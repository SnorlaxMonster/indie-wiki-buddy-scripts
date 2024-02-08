"""
Python script for scraping metadata from wikis
"""
import lxml.html
import requests
from enum import Enum
from requests.exceptions import SSLError
from typing import Optional, Iterable
from urllib.parse import urlparse, urlunparse, ParseResult as UrlParseResult


class WikiSoftware(Enum):
    MEDIAWIKI = 1
    FEXTRALIFE = 2


def extract_base_url(input_url):
    parsed_input_url = urlparse(input_url)
    return urlunparse((parsed_input_url.scheme, parsed_input_url.netloc, '', '', '', ''))


def normalize_relative_url(relative_url: str | UrlParseResult, absolute_url: str | UrlParseResult) -> str:
    """
    Ensures that a URL includes the protocol and domain name, and does not include a query.
    For example, if the input URL is "/w/api.php?action=rsd", adds the protocol and domain name to the URL,
    and deletes action=rsd.

    :param relative_url: URL to be normalized
    :param absolute_url: URL to use to fill in gaps in the first URL
    :return: Normalized API URL
    """
    # Parse URLs, if not already parsed
    if type(relative_url) is str:
        parsed_relative_url = urlparse(relative_url)
    else:
        parsed_relative_url = relative_url

    if type(absolute_url) is str:
        parsed_absolute_url = urlparse(absolute_url)
    else:
        parsed_absolute_url = absolute_url

    # Construct a new URL
    parsed_new_url = parsed_relative_url
    if parsed_new_url.netloc == "":
        parsed_new_url = parsed_new_url._replace(netloc=parsed_absolute_url.netloc)
    if parsed_new_url.scheme == "":
        parsed_new_url = parsed_new_url._replace(scheme=parsed_absolute_url.scheme)
    if parsed_new_url.query != "":
        parsed_new_url = parsed_new_url._replace(query="")

    return str(urlunparse(parsed_new_url))


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


def request_with_http_fallback(raw_url: str, **kwargs) -> requests.Response:
    """
    Attempts to resolve the URL, then falls back to HTTP if an SSLError occurred.

    :param raw_url: URL to resolve
    :param kwargs: kwargs to use for the HTTP requests
    :return: GET request response
    """
    url = normalize_url_protocol(raw_url)
    parsed_url = urlparse(url)

    # GET request the URL
    try:
        response = requests.get(url, **kwargs)

    # If using HTTPS results in an SSLError, try HTTP instead
    except SSLError and parsed_url.scheme != "http":
        print(f"⚠ SSLError for {raw_url} . Defaulting to HTTP connection.")
        url = urlunparse(parsed_url._replace(scheme="http"))
        response = requests.get(url, **kwargs)

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
    If the site URL or logo URL contains the name of a wikifarm, assume the wiki is hosted on that wikifarm
    Checking the logo URL should catch any wikis hosted on a wikifarm that use a custom URL

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

