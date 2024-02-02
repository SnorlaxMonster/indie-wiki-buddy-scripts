"""
Python script for scraping metadata from wikis
"""
import requests
import lxml.html
import re
from io import BytesIO
from typing import Optional
from urllib.parse import urlparse, urlunparse, urljoin, ParseResult as UrlParseResult


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


def request_with_error_handling(raw_url: str, ignorable_errors: Optional[list[int]] = None,
                                **kwargs) -> Optional[requests.Response]:
    """
    Given a base URL, attempts to resolve the URL as HTTPS, then falls back to HTTP if that isn't possible.

    :param raw_url: URL to resolve
    :param ignorable_errors: Error codes that should be ignored as long as the response is not null
    :param kwargs: kwargs to use for the HTTP/HTTPS requests
    :return: GET request response
    """
    ignorable_errors = [] if (ignorable_errors is None) else ignorable_errors
    url = normalize_url_protocol(raw_url)

    # GET request the URL
    try:
        response = requests.get(url, **kwargs)

    # If using HTTPS results in an SSLError, try HTTP instead
    except requests.exceptions.SSLError:
        parsed_url = urlparse(url)
        if parsed_url.scheme != "http":
            print(f"⚠ SSLError. Defaulting to HTTP connection for {raw_url}")
            url = urlunparse(parsed_url._replace(scheme="http"))
            return request_with_error_handling(url, ignorable_errors=ignorable_errors, **kwargs)
        else:
            print(f"⚠ SSLError: Unable to connect to {raw_url} , even via HTTP")
            return None

    # If unable to connect to the URL at all, return None
    except requests.exceptions.ConnectionError:
        print(f"⚠ ConnectionError: Unable to connect to {url}")
        return None

    # If the response is an error, handle the error
    if not response:
        # If the error is in the set of acceptable errors and the response included any content, ignore the error
        if response.status_code in ignorable_errors and response.content:
            pass

        # For Error 404 and 410, the wiki presumably does not exist, so abort
        # This could mean that only the Main Page does not exist (while the wiki does), but that case is ignored
        if response.status_code in (404, 410):
            print(f"⚠ Error {response.status_code} returned by {url} . Page does not exist. Aborting.")
            return None

        # For other errors, they can usually be fixed by adding a user-agent
        else:
            print(f"⚠ Error {response.status_code} returned by {url} . Aborting.")
            return None

    # Otherwise, return the response
    return response


def is_mediawiki(parsed_html: lxml.html.etree) -> bool:
    """
    Checks if the page is a MediaWiki page.
    MediaWiki pages can be identified by containing "mediawiki" as a class on the <body> element.

    :param parsed_html: LXML etree representation of the page's HTML
    :return: Whether the page is a page from a MediaWiki site
    """
    body_elem = parsed_html.find('body')
    if body_elem is None:
        return False

    body_class = body_elem.get('class')
    if body_class is None:
        return False

    if 'mediawiki' in body_class.split():
        return True
    else:
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
    Given a URL or HTTP/HTTPS response for a wiki page, determines the wiki's API URL.

    :param wiki_page: URL or HTTP/HTTPS response for a wiki page
    :param headers: Headers to include in the request (e.g. user-agent) if provided a URL
    :return: Wiki's API URL
    """
    # If provided a URL, run an HTTP request
    if type(wiki_page) is str:
        url = wiki_page
        response = request_with_error_handling(url, headers=headers)
        if wiki_page is None:
            return None

    else:
        response = wiki_page
        assert headers is None

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

    # If EditURI is missing, try to find the permalink URL and determine the API URL from that
    permalink_node = parsed_html.find('//li[@id="t-permalink"]/a')
    if permalink_node is not None:
        print(f"ℹ Retrieved API URL for {response.url} via permalink node")
        permalink_url = permalink_node.get("href")
        parsed_permalink_url = urlparse(permalink_url)
        parsed_api_url = parsed_permalink_url._replace(path=parsed_permalink_url.path.replace('index.php', 'api.php'))

        return normalize_relative_url(parsed_api_url, response.url)

    # Otherwise, the API URL retrieval has failed
    return None


def query_mediawiki_api(api_url: str, query_params: dict, headers: Optional[dict] = None) -> Optional[dict]:
    """
    Runs a MediaWiki API query with the specified parameters.

    :param api_url:
    :param query_params: Query parameters to use
    :param headers: Headers to include in the request (e.g. user-agent)
    :return: API query result
    """
    # GET request API query
    response = request_with_error_handling(api_url, params=query_params, headers=headers)

    # If the response is an error, do not attempt to parse it as JSON
    if not response:
        print(f"Error {response.status_code} from {api_url}")
        return None

    # Parse as JSON
    query_json = response.json()

    # If the response contains an error, do not attempt the retrieve the content
    if query_json.get("error"):
        print(f"Error {query_json['error'].get('code')} from {api_url}")
        return None

    return query_json["query"]


def request_siteinfo(api_url: str, siprop: str = 'general', headers: Optional[dict] = None) -> Optional[dict]:
    """
    Runs a "siteinfo" query using the MediaWiki API of the specified wiki.

    :param api_url: MediaWiki API URL
    :param siprop: Value to use for the siprop parameter (defaults to 'general')
    :param headers: Headers to include in the request (e.g. user-agent).
    :return: siteinfo query result
    """
    siteinfo_params = {'action': 'query', 'meta': 'siteinfo', 'siprop': siprop, 'format': 'json'}
    siteinfo = query_mediawiki_api(api_url, siteinfo_params, headers=headers)

    return siteinfo
