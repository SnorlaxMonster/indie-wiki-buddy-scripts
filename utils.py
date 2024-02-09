"""
Python script for scraping metadata from wikis
"""
import json
import lxml.html
import os
import requests
from enum import Enum
from requests.exceptions import SSLError
from typing import Optional, Iterable
from urllib.parse import urlparse, urlunparse, ParseResult as UrlParseResult

USER_CONFIG_PATH = "user_config.json"


class WikiSoftware(Enum):
    MEDIAWIKI = 1
    FEXTRALIFE = 2
    DOKUWIKI = 3


def extract_base_url(input_url):
    parsed_input_url = urlparse(input_url)
    return urlunparse((parsed_input_url.scheme, parsed_input_url.netloc, '', '', '', ''))


def ensure_absolute_url(subject_url: str | UrlParseResult, donor_url: str | UrlParseResult) -> str:
    """
    Ensures that a URL includes the protocol and domain name, copying them from the donor URL if missing from the
    subject URL. If they are already provided, leaves those properties unmodified.

    :param subject_url: URL to be filled in
    :param donor_url: URL to use to fill in gaps in the subject URL
    :return: Absolute version of the subject URL
    """
    # Parse URLs, if not already parsed
    if isinstance(subject_url, str):
        parsed_relative_url = urlparse(subject_url)
    else:
        parsed_relative_url = subject_url

    if isinstance(donor_url, str):
        parsed_absolute_url = urlparse(donor_url)
    else:
        parsed_absolute_url = donor_url

    # Construct a new URL
    parsed_new_url = parsed_relative_url
    if parsed_new_url.netloc == "":
        parsed_new_url = parsed_new_url._replace(netloc=parsed_absolute_url.netloc)
    if parsed_new_url.scheme == "":
        parsed_new_url = parsed_new_url._replace(scheme=parsed_absolute_url.scheme)

    return parsed_new_url.geturl()


def normalize_url_protocol(url: str, default_protocol="https") -> str:
    """
    Enforces that the URL specifies a protocol.

    :param url: Unnormalized URL
    :param default_protocol: Protocol to use to access the URL, if one is not specified
    :return: URL with a protocol
    """
    if url.startswith(("http://", "https://")):
        return url
    elif url.startswith("//"):
        return f"{default_protocol}:{url}"
    else:
        return f"{default_protocol}://{url}"


def request_with_http_fallback(raw_url: str, session: Optional[requests.Session] = None, **kwargs) -> requests.Response:
    """
    Attempts to resolve the URL, then falls back to HTTP if an SSLError occurred.

    :param raw_url: URL to resolve
    :param session: requests Session to use for resolving the URL
    :param kwargs: kwargs to use for the HTTP requests
    :return: GET request response
    """
    # Create a new session if one was not provided
    if session is None:
        session = requests.Session()

    # Prepare the URL
    url = normalize_url_protocol(raw_url)

    # GET request the URL
    try:
        response = session.get(url, **kwargs)

    # If using HTTPS results in an SSLError, try HTTP instead
    except SSLError as e:
        parsed_url = urlparse(url)
        if parsed_url.scheme != "http":
            print(f"⚠ SSLError for {raw_url} . Defaulting to HTTP connection.")
            url = urlunparse(parsed_url._replace(scheme="http"))
            response = session.get(url, **kwargs)
        else:
            raise e

    return response


def extract_xpath_property(parsed_html: lxml.html.etree, xpath: str, property_name: str):
    """
    Returns the value of a specific property of an element selected via XPath from an HTML document.
    Returns None if the element does not exist, or if the element does not have the specified property

    :param parsed_html: Parsed HTML
    :param xpath: XPath uniquely identifying the HTML element to extract the property from
    :param property_name: Name of the property to extract
    :return: Software the wiki runs on
    """
    url_elem = parsed_html.find(xpath)
    if url_elem is not None:
        return url_elem.get(property_name)
    else:
        return None


def detect_wikifarm(url_list: Iterable[str]) -> Optional[str]:
    """
    If the site URL or logo URL contains the name of a wikifarm, assume the wiki is hosted on that wikifarm.
    Checking the logo URL should catch any wikis hosted on a wikifarm that use a custom URL.

    :param url_list: List of URLs to inspect for wikifarms
    :return: Name of the site's wikifarm, if it is hosted by one
    """
    # This is only relevant for destinations, so "fandom" is not checked for (and it would likely give false positives)
    known_wikifarms = {"shoutwiki", "wiki.gg", "miraheze", "wikitide"}

    for wikifarm in known_wikifarms:
        for url in url_list:
            if wikifarm in url:
                return wikifarm
    return None


def resolve_wiki_page(wiki_page: str | requests.Response,
                      session: Optional[requests.Session] = None, headers: Optional[dict] = None) -> requests.Response:
    """
    Given a URL, returns the corresponding Response object.
    If given a response object, just returns the response unmodified.

    This function is used to allow functions to take either a wiki page or response object as input.

    :param wiki_page: URL of a wiki page, or Response object for a wiki page URL
    :param session: requests Session to use for resolving the URL
    :param headers: Headers to include in HTTP requests (e.g. user-agent)
    :return: Response object for a wiki page URL
    """
    # If provided a response, return it unmodified
    if isinstance(wiki_page, requests.Response):
        response = wiki_page
        return response

    # If no Session was provided, create one
    if session is None:
        session = requests.Session()

    # If provided a URL, run an HTTP request
    assert isinstance(wiki_page, str)
    url = wiki_page
    response = request_with_http_fallback(url, session=session, headers=headers)

    # If the request returned an error, raise an exception
    if not response:
        response.raise_for_status()

    return response


def read_user_config(key, default=None):
    if os.path.isfile(USER_CONFIG_PATH):
        with open(USER_CONFIG_PATH, "r", encoding="utf-8") as user_config_file:
            user_config = json.load(user_config_file)
        return user_config.get(key, default)
    else:
        return default


def update_user_config(key, value):
    user_config_exists = os.path.isfile(USER_CONFIG_PATH)
    with open(USER_CONFIG_PATH, "w+", encoding="utf-8") as user_config_file:
        if user_config_exists:
            user_config = json.load(user_config_file)
        else:
            user_config = dict()
        user_config[key] = value
        json.dump(user_config, user_config_file, indent=2, ensure_ascii=False)
