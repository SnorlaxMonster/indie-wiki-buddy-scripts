"""
Python script for scraping metadata from wikis
"""
import lxml.html
import re
import warnings
import requests
from requests.exceptions import SSLError
from io import BytesIO
from typing import Optional, Generator
from urllib.parse import urlparse, urlunparse, urljoin, ParseResult as UrlParseResult


class MediaWikiAPIError(Exception):
    pass


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

    return urlunparse(parsed_new_url)


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


def normalize_wikia_url(original_url: str) -> str:
    """
    Old Wikia URLs included the language as a subdomain for non-English wikis, but these URLs no longer work.
    Non-English Wikia URLs need to be modified to move the language to the path of the URL.

    :param original_url: Wikia URL
    :return: Modified URL with language moved to the path, if necessary
    """
    # Ignore non-Wikia URLs
    if "wikia.com" not in original_url:
        return original_url

    # Parse the URL
    parsed_url = urlparse(normalize_url_protocol(original_url))

    # Extract the URL components
    lang_match = re.match(r"([a-z]+)\.(.*)\.wikia\.com", parsed_url.hostname)
    if not lang_match:
        return original_url
    lang, subdomain = lang_match.groups()

    # Construct the new URL
    new_domain = f"{subdomain}.fandom.com"  # Always use "fandom.com" when restructuring Wikia URLs
    new_path = urljoin(lang, parsed_url.path)
    parsed_url = parsed_url._replace(netloc=new_domain, path=new_path)

    return urlunparse(parsed_url)


def extract_hostname(url: str) -> str:
    """
    Extract the hostname (full domain name) of the specified URL.

    :param url: URL
    :return: Domain name
    """
    parsed_url = urlparse(url)
    return parsed_url.hostname


def request_with_http_fallback(raw_url: str, **kwargs) -> requests.Response:
    """
    Attempts to resolve the URL, then falls back to HTTP if an SSLError occurred.

    :param raw_url: URL to resolve
    :param ignorable_errors: Error codes that should be ignored as long as the response is not null
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


def is_mediawiki(parsed_html: lxml.html.etree) -> bool:
    """
    Checks if the page is a MediaWiki page.

    :param parsed_html: LXML etree representation of the page's HTML
    :return: Whether the page is a page from a MediaWiki site
    """
    # Most MediaWiki wikis include 'mediawiki' as a class on the <body> element
    body_elem = parsed_html.find('body')
    if body_elem is not None:
        body_class = body_elem.get('class')
        if body_class is not None:
            if 'mediawiki' in body_class.split():
                return True

    # For wikis that lack this (e.g. Neoseeker's AdBird skin), they can still be identified by the content element
    content_elem = parsed_html.find('//div[@id="mw-content-text"]/div[@class="mw-parser-output"]')
    if content_elem is not None:
        return True

    # Otherwise, assume the site is not MediaWiki
    return False


def determine_wiki_software(response: Optional[requests.Response]) -> Optional[str]:
    if not response:
        return None

    # Parse the HTML
    parsed_html = lxml.html.parse(BytesIO(response.content))

    # Check the wiki's software
    if is_mediawiki(parsed_html):
        return "mediawiki"
    else:
        return None  # unable to determine the wiki's software


def get_fandom_url_from_breezewiki(response: Optional[requests.Response]) -> Optional[str]:
    """
    If the input page is a BreezeWiki site, returns the original Fandom URL.
    Otherwise returns None.

    :param response: HTTP response for a wiki page request
    :return: URL of the same page on Fandom if the page is a BreezeWiki page, otherwise None
    """
    # Parse the HTML
    parsed_html = lxml.html.parse(BytesIO(response.content))

    # Assume all BreezeWiki instances include a link to the sourcecode in the footer
    bw_footer_signature_xpath = '//footer[@class="custom-footer"]//a[@href="https://gitdab.com/cadence/breezewiki"]'
    if parsed_html.find(bw_footer_signature_xpath) is None:
        return None

    # Retrieve the Fandom URL from the page footer
    # NOTE: This is very fragile, and could break on alternate BreezeWiki hosts or in future BreezeWiki updates
    fandom_link_node = parsed_html.find('//footer[@class="custom-footer"]/div/div[2]/p/a[1]')
    fandom_url = fandom_link_node.get('href')
    if ".fandom.com" in fandom_url and fandom_url != "https://www.fandom.com/licensing":
        return fandom_url
    else:
        return None


def get_favicon_url(response: Optional[requests.Response]) -> Optional[str]:
    if not response:
        return None

    parsed_html = lxml.html.parse(BytesIO(response.content))

    # Find the icon element in the HTML
    icon_link_element = parsed_html.find('//link[@rel="shortcut icon"]')
    if icon_link_element is None:
        icon_link_element = parsed_html.find('//link[@rel="icon"]')
    if icon_link_element is None:
        return None

    # Retrieve the URL from the icon element
    icon_url = icon_link_element.get("href")
    if icon_url is None:
        return None

    return normalize_relative_url(icon_url, response.url)


def get_mediawiki_api_url(wiki_page: str | requests.Response, headers: Optional[dict] = None) -> Optional[str]:
    """
    Given a URL or HTTP response for a wiki page, determines the wiki's API URL.

    :param wiki_page: URL or HTTP response for a wiki page
    :param headers: Headers to include in the request (e.g. user-agent) if provided a URL
    :return: Wiki's API URL
    """
    # If provided a URL, run an HTTP request
    if type(wiki_page) is str:
        url = wiki_page
        url = normalize_wikia_url(url)  # Normalize defunct Wikia URLs
        response = request_with_http_fallback(url, headers=headers)
        if not response:
            response.raise_for_status()
    else:
        response = wiki_page

    # Parse the HTML
    parsed_html = lxml.html.parse(BytesIO(response.content))

    # If the site is not a MediaWiki wiki, abort trying to determine the API URL
    if not is_mediawiki(parsed_html):
        print(f"⚠ {response.url} is not a MediaWiki page")
        return None

    # Retrieve the API URL via EditURI element
    edit_uri_node = parsed_html.find('/head/link[@rel="EditURI"]')
    if edit_uri_node is not None:
        api_url = edit_uri_node.get('href')

        return normalize_relative_url(api_url, response.url)

    # If EditURI is missing, try to find the searchform element and determine the API URL from that
    searchform_node = parsed_html.find('//form[@id="searchform"]')
    if searchform_node is not None:
        print(f"ℹ Retrieved API URL for {response.url} via searchform node")
        api_url = searchform_node.get("action").replace('index.php', 'api.php')

        return normalize_relative_url(api_url, response.url)

    # If EditURI is missing, try to find the permalink URL and determine the API URL from that
    permalink_node = parsed_html.find('//li[@id="t-permalink"]/a')
    if permalink_node is not None:
        print(f"ℹ Retrieved API URL for {response.url} via permalink node")
        api_url = permalink_node.get("href").replace('index.php', 'api.php')

        return normalize_relative_url(api_url, response.url)

    # If the page is a BreezeWiki page, identify the original Fandom URL and retrieve the API URL from Fandom
    if ".fandom.com" not in response.url:
        fandom_url = get_fandom_url_from_breezewiki(response)
        if fandom_url is not None:
            print(f"ℹ {response.url} is a BreezeWiki site. Retrieving API URL from Fandom.")
            return get_mediawiki_api_url(fandom_url, headers=headers)

    # Otherwise, the API URL retrieval has failed
    return None


def query_mediawiki_api(api_url: str, params: dict, **kwargs) -> dict:
    """
    Runs a MediaWiki API query with the specified parameters.

    :param api_url: MediaWiki API URL
    :param kwargs: kwargs to use for the HTTP requests
    :return: API query result
    :raises: HTTPError: If the API request returns an HTTP error code
    :raises: MediaWikiAPIError: If the API query returns an error
    """
    # GET request API query
    response = request_with_http_fallback(api_url, params=params, **kwargs)

    # If the response is an error, raise an HTTPError
    if not response:
        response.raise_for_status()

    # Parse as JSON
    result = response.json()

    # Check for errors and warnings
    if 'error' in result:
        raise MediaWikiAPIError(result['error'])
    if 'warnings' in result:
        warnings.warn(result['warnings'])

    return result['query']


def query_mediawiki_api_with_continue(api_url: str, params: dict, headers: Optional[dict] = None) \
        -> Generator[dict, None, None]:
    """
    Runs a MediaWiki API query with the specified parameters.

    :param api_url: MediaWiki API URL
    :param params: Query parameters
    :param headers: Headers to include in the request (e.g. user-agent)
    :return: Generator of API query results
    :raises: HTTPError: If the API request returns an HTTP error code
    :raises: MediaWikiAPIError: If the API query returns an error
    """

    # Based on https://www.mediawiki.org/wiki/API:Continue#Example_3:_Python_code_for_iterating_through_all_results
    params['action'] = 'query'
    params['format'] = 'json'
    last_continue = {}
    while True:
        # Clone original request
        request_params = params.copy()
        # Modify it with the values returned in the 'continue' section of the last result.
        request_params.update(last_continue)
        # Call API
        response = requests.get(api_url, params=request_params, headers=headers)

        # If the response is an error, raise an HTTPError
        if not response:
            response.raise_for_status()

        # Process result
        result = response.json()
        if 'error' in result:
            raise MediaWikiAPIError(result['error'])
        if 'warnings' in result:
            warnings.warn(result['warnings'])
        if 'query' in result:
            yield result['query']
        if 'continue' not in result:
            break
        last_continue = result['continue']
