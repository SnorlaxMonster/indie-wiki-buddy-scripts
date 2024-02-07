"""
Python script for scraping metadata from wikis
"""
import lxml.html
import re
import warnings
import time
import requests
from requests.exceptions import SSLError
from io import BytesIO
from typing import Optional, Generator, Iterable
from urllib.parse import urlparse, urlunparse, urljoin, ParseResult as UrlParseResult
from enum import Enum


class WikiSoftware(Enum):
    MEDIAWIKI = 1
    FEXTRALIFE = 2


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

    return str(urlunparse(parsed_url))


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


def extract_mediawiki_version(generator_string: str) -> str:
    match = re.match(r"MediaWiki (\d+\.\d+\.\d+)(?:\+.*)?", generator_string)
    return match.group(1)


def determine_wiki_software(parsed_html: lxml.html.etree) -> Optional[WikiSoftware]:
    """
    Determines what software the specified wiki is running

    :param parsed_html: Parsed HTML for a wiki page
    :return: Software the wiki runs on
    """
    # Check the generator meta element
    generator = extract_xpath_property(parsed_html, '//meta[@name="generator"]', "content")
    if generator is not None:
        if generator.startswith("MediaWiki"):
            return WikiSoftware.MEDIAWIKI

    # Check the wiki's URL via URL meta element
    meta_url = extract_xpath_property(parsed_html, '//meta[@property="og:url"]', "content")
    if meta_url is not None:
        parsed_url = urlparse(meta_url)
        if parsed_url.hostname.endswith("fextralife.com"):
            return WikiSoftware.FEXTRALIFE

    # Check the class on the body element (necessary for BreezeWiki)
    body_class = extract_xpath_property(parsed_html, 'body', "class")
    if body_class is not None:
        if 'mediawiki' in body_class.split():
            return WikiSoftware.MEDIAWIKI

    # Check the content element (necessary for Neoseeker's AdBird skin)
    content_elem = parsed_html.find('//div[@id="mw-content-text"]/div[@class="mw-parser-output"]')
    if content_elem is not None:
        return WikiSoftware.MEDIAWIKI

    # Unable to determine the wiki's software
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


def get_mediawiki_favicon_url(parsed_html: lxml.html.etree) -> Optional[str]:
    """
    Given an HTTP response for a MediaWiki page, determines the wiki's favicon's URL.

    :param parsed_html: Parsed HTML for a wiki page
    :return: Favicon URL
    """
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

    # Retrieve the page's URL
    page_url = extract_xpath_property(parsed_html, '//meta[@property="og:url"]', "content")

    return normalize_relative_url(icon_url, page_url)


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

        # Check that the input URL isn't already an API URL
        if url.endswith("/api.php"):
            return url

        url = normalize_wikia_url(url)  # Normalize defunct Wikia URLs
        response = request_with_http_fallback(url, headers=headers)
        if not response:
            response.raise_for_status()
    else:
        response = wiki_page

    # Parse the HTML
    parsed_html = lxml.html.parse(BytesIO(response.content))

    # If the site is not a MediaWiki wiki, abort trying to determine the API URL
    wiki_software = determine_wiki_software(parsed_html)
    if wiki_software != WikiSoftware.MEDIAWIKI:
        print(f"⚠ {response.url} is not a MediaWiki page")
        return None

    # Retrieve the API URL via EditURI element
    api_url = extract_xpath_property(parsed_html, '/head/link[@rel="EditURI"]', "href")
    if api_url is not None:
        return normalize_relative_url(api_url, response.url)

    # If EditURI is missing, try to find the searchform element and determine the API URL from that
    searchform_node_url = extract_xpath_property(parsed_html, '//form[@id="searchform"]', "action")
    if searchform_node_url is not None:
        if searchform_node_url.endswith('index.php'):
            print(f"ℹ Retrieved API URL for {response.url} via searchform node")
            api_url = searchform_node_url.replace('index.php', 'api.php')

            return normalize_relative_url(api_url, response.url)

    # If EditURI is missing, try to find the permalink URL and determine the API URL from that
    permalink_url = extract_xpath_property(parsed_html, '//li[@id="t-permalink"]/a', "href")
    if permalink_url is not None:
        if permalink_url.endswith('index.php'):
            print(f"ℹ Retrieved API URL for {response.url} via permalink node")
            api_url = permalink_url.replace('index.php', 'api.php')

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
    :param params: params to use for the HTTP requests
    :param kwargs: kwargs to use for the HTTP requests
    :return: API query result
    :raises: HTTPError: If the API request returns an HTTP error code
    :raises: MediaWikiAPIError: If the API query returns an error
    """
    # GET request API query
    response = requests.get(api_url, params=params, **kwargs)

    # If the response is Error 429 (Too Many Requests), sleep for 30 seconds then try again (once only)
    if response.status_code == 429:
        print(f"🕑 Error 429 Too Many Requests. Sleeping for 30 seconds...")
        time.sleep(30)
        response = requests.get(api_url, params=params, **kwargs)

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


def extract_metadata_from_siteinfo(siteinfo: dict) -> dict:
    """
    Extracts the important data from a siteinfo result, and transforms it into a standardized format

    :param siteinfo: MediaWiki API response for a "siteinfo" query including siprop=general
    :return: Standardized site properties
    """
    base_url = urlparse(siteinfo["general"]["base"]).hostname

    # Retrieve normalized language
    full_language = siteinfo["general"]["lang"]  # NOTE: The language retrieved this way may include the dialect
    normalized_language = full_language.split('-')[0]

    # For Fandom wikis, ensure the language is part of the base_url instead of the content_path
    content_path = siteinfo["general"]["articlepath"].replace("$1", "")
    if ".fandom.com" in base_url and normalized_language != "en":
        full_path_parts = (base_url + content_path).split("/")
        if full_path_parts[1] == normalized_language:
            base_url = "/".join(full_path_parts[0:2])
            content_path = "/" + "/".join(full_path_parts[2:])

    # Apply standard wiki name changes
    wiki_name = siteinfo["general"]["sitename"]
    if ".fandom.com" in base_url:
        wiki_name = wiki_name.replace(" Wiki", " Fandom Wiki")

    # Detect if the wiki is on a wikifarm
    logo_path = siteinfo["general"].get("logo", "")  # Not guaranteed to be present
    wikifarm = detect_wikifarm([base_url, logo_path])

    # Get favicon path
    favicon_path = siteinfo["general"].get("favicon")  # Not guaranteed to be present
    if favicon_path is not None and favicon_path.startswith("$"):
        # On Fandom, the API's favicon URL path starts with $wgUploadPath. For now, just ignore these kinds of paths.
        favicon_path = None

    # Return extracted properties
    wiki_metadata = {
        # Basic information
        "name": wiki_name,
        "base_url": base_url,
        "full_language": full_language,
        "language": normalized_language,

        # Technical data
        "wiki_id": siteinfo["general"]["wikiid"],
        "wikifarm": wikifarm,
        "platform": "MediaWiki".lower(),
        "software_version": extract_mediawiki_version(siteinfo["general"]["generator"]),

        # Paths
        "protocol": urlparse(siteinfo["general"]["base"]).scheme,
        "main_page": siteinfo["general"]["mainpage"].replace(" ", "_"),
        "content_path": content_path,
        "search_path": siteinfo["general"]["script"],
        "icon_path": favicon_path,

        # Licensing
        "licence_name": siteinfo["rightsinfo"]["text"] if "rightsinfo" in siteinfo else None,
        "licence_page": siteinfo["rightsinfo"]["url"] if "rightsinfo" in siteinfo else None,
    }

    return wiki_metadata


def extract_metadata_from_fextralife_page(response: requests.Response):
    """
    Extracts the important data from a Fextralife page, and transforms it into a standardized format

    :param response: HTTP response for a Fextralife page
    :return: Standardized site properties
    """
    page_html = lxml.html.parse(BytesIO(response.content))

    # Extract language
    language = page_html.getroot().get('lang')

    # Extract the favicon URL
    favicon_path = extract_xpath_property(page_html, '//link[@type="logos/x-icon"]', "href")

    # Extract the wiki ID
    wiki_id = None
    pagex_script_matches = page_html.xpath("//script[contains(., 'pagex')]")
    if len(pagex_script_matches) > 0:
        pagex_script = pagex_script_matches[0].text
        match = re.search(r"pagex\['wikiId'\] = '(.*)';", pagex_script)
        if match:
            wiki_id = match.group(1)

    # Return extracted properties
    wiki_metadata = {
        # Basic information
        "name": page_html.find('//title').text.split(" | ")[-1],
        "base_url": urlparse(response.url).hostname,
        "full_language": language,
        "language": language,

        # Technical data
        "wiki_id": wiki_id,
        "wikifarm": "Fextralife",
        "platform": "Fextralife".lower(),
        "software_version": None,  # N/A

        # Paths
        "protocol": urlparse(response.url).scheme,
        "main_page": page_html.find('//a[@class="WikiLogo WikiElement"]').get("href").removeprefix('/'),
        "content_path": "/",
        "search_path": None,  # Irrelevant
        "icon_path": favicon_path,

        # Licensing
        "licence_name": "Fextralife Wiki Custom License",
        "licence_page": "https://fextralife.com/wiki-license/",
    }
    return wiki_metadata
