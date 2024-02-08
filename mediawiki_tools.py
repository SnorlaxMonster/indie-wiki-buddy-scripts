"""
Python script for generating metadata about wikis
"""
import datetime
import lxml.etree
import lxml.html
import pandas as pd
import re
import requests
import time
import warnings
from io import BytesIO
from typing import Optional, Generator
from urllib.parse import urlparse, urlunparse, urljoin

from scrapewiki import (extract_xpath_property, ensure_absolute_url, normalize_url_protocol,
                        request_with_http_fallback, detect_wikifarm)


class MediaWikiAPIError(Exception):
    """
    Errors returned by the MediaWiki API
    """
    pass


def normalize_wikia_url(original_url: str) -> str:
    """
    Old Wikia URLs included the language as a subdomain for non-English wikis, but these URLs no longer work.
    Non-English Wikia URLs need to be modified in order to move the language to the path of the URL.

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


def extract_mediawiki_version(generator_string: str) -> str:
    match = re.match(r"MediaWiki (\d+\.\d+\.\d+)(?:\+.*)?", generator_string)
    return match.group(1)


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

    return ensure_absolute_url(icon_url, page_url)


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

        response = request_with_http_fallback(url, headers=headers)
        if not response:
            response.raise_for_status()
    else:
        response = wiki_page

    # Parse the HTML
    parsed_html = lxml.html.parse(BytesIO(response.content))

    # Retrieve the API URL via EditURI element
    api_url = extract_xpath_property(parsed_html, '/head/link[@rel="EditURI"]', "href")
    if api_url is not None:
        # Delete the query from the API URL (usually this element's API URL includes '?action=rsd')
        api_url = urlparse(api_url)._replace(query="").geturl()
        return ensure_absolute_url(api_url, response.url)

    # If EditURI is missing, try to find the searchform element and determine the API URL from that
    searchform_node_url = extract_xpath_property(parsed_html, '//form[@id="searchform"]', "action")
    if searchform_node_url is not None:
        if searchform_node_url.endswith('index.php'):
            print(f"ℹ Retrieved API URL for {response.url} via searchform node")
            api_url = searchform_node_url.replace('index.php', 'api.php')

            return ensure_absolute_url(api_url, response.url)

    # If EditURI is missing, try to find the permalink URL and determine the API URL from that
    permalink_url = extract_xpath_property(parsed_html, '//li[@id="t-permalink"]/a', "href")
    if permalink_url is not None:
        if permalink_url.endswith('index.php'):
            print(f"ℹ Retrieved API URL for {response.url} via permalink node")
            api_url = permalink_url.replace('index.php', 'api.php')
            api_url = urlparse(api_url)._replace(query="").geturl()

            return ensure_absolute_url(api_url, response.url)

    # If the page is a BreezeWiki page, identify the original Fandom URL and retrieve the API URL from Fandom
    if ".fandom.com" not in response.url:
        fandom_url = get_fandom_url_from_breezewiki(response)
        if fandom_url is not None:
            print(f"ℹ {response.url} is a BreezeWiki site. Retrieving API URL from Fandom.")
            return get_mediawiki_api_url(fandom_url, headers=headers)

    # Otherwise, the API URL retrieval has failed
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


def retrieve_mediawiki_recentchanges(api_url: str, window_end: datetime.datetime, extra_params: Optional[dict] = None,
                                     headers: Optional[dict] = None) -> pd.DataFrame:
    """
    Returns the full set of Recent Changes back to a specific date.
    By default, it excludes bots and only includes page edits, page creations, and category additions.

    :param api_url: MediaWiki API URL
    :param window_end: Date of the earliest Recent Changes entry to include
    :param extra_params: Parameters to include in the Recent Changes query, beyond the default values
    :param headers: Headers to include in HTTP requests (e.g. user-agent)
    :return: DataFrame of Recent Changes
    """
    # Prepare query params
    # NOTE: The MediaWiki API is very particular about date formats. Timezone must be written in Z format.
    assert window_end.tzinfo == datetime.timezone.utc  # TODO: Handle wikis using a timezone other than UTC
    rcend = window_end.strftime('%Y-%m-%dT%H:%M:%SZ')
    query_params = {'list': 'recentchanges', 'rcshow': '!bot', 'rclimit': 'max', 'rcend': rcend,
                    'rctype': 'edit|new|categorize'}
    if extra_params is not None:
        query_params.update(extra_params)

    # Execute query, iterating over each continuation (MediaWiki typically returns up to 500 results per query)
    rc_contents = []
    for result in query_mediawiki_api_with_continue(api_url, params=query_params, headers=headers):
        rc_contents += result['recentchanges']

    rc_df = pd.DataFrame(rc_contents)
    if not rc_df.empty:
        rc_df["timestamp"] = pd.to_datetime(rc_df["timestamp"])
    return rc_df


def profile_mediawiki_recentchanges(api_url: str, rc_days_limit: int, siteinfo: dict,
                                    headers: Optional[dict] = None) -> tuple[int, Optional[datetime.datetime]]:
    """
    Determines the number of content-namespace edits by humans to the wiki within the last X days,
    and the date of the most recent content-namespace edit by a human.

    :param api_url: MediaWiki API URL
    :param rc_days_limit: The number of days to look back when retrieving Recent Changes
    :param siteinfo: Result of a previous MediaWiki siteinfo query
    :param headers: Headers to include in the request (e.g. user-agent)
    :return: number of edits in the time window, and timestamp of the last edit
    """
    # Calculate window_end
    #current_timestamp = datetime.datetime.fromisoformat(siteinfo["general"]["time"])  # Not supported until Python 3.11
    current_timestamp = datetime.datetime.strptime(siteinfo["general"]["time"], '%Y-%m-%dT%H:%M:%S%z')
    window_end = current_timestamp - datetime.timedelta(rc_days_limit)

    # Determine content namespaces
    content_namespaces = [entry.get('id') for entry in siteinfo["namespaces"].values()
                          if entry.get('content') is not None]

    # Retrieve Recent Changes
    extra_params = {
        "rcnamespace": '|'.join(str(ns) for ns in content_namespaces),
        "rcend": window_end.strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    rc_df = retrieve_mediawiki_recentchanges(api_url, window_end, extra_params=extra_params, headers=headers)

    # Count edits in the time window
    edit_count = len(rc_df)

    # Find latest edit
    if len(rc_df) > 0:
        latest_edit_timestamp = rc_df["timestamp"].max()

    # If there were no edits in the time window, request Recent Changes without a time restriction
    else:
        query_params = {'list': 'recentchanges', 'rcshow': '!bot', 'rclimit': 1, 'rctype': 'edit|new|categorize',
                        "rcnamespace": '|'.join(str(ns) for ns in content_namespaces)}
        rc_extended = query_mediawiki_api(api_url, query_params, headers=headers)

        # Get the most recent edit from the Recent Changes result
        rc_contents = rc_extended["recentchanges"]
        if len(rc_contents) > 0:
            latest_edit_timestamp = rc_df["timestamp"].max()
        else:
            latest_edit_timestamp = None

    return edit_count, latest_edit_timestamp


def profile_mediawiki_wiki(wiki_url: str, full_profile: bool = True,
                           rc_days_limit: int = 30, headers: Optional[dict] = None) -> dict:
    """
    Uses the MediaWiki API to retrieve key information about the specified MediaWiki site,
    including content and activity metrics.

    :param wiki_url: URL for a page on the MediaWiki wiki, or the API URL of the MediaWiki wiki
    :param full_profile: Whether to include activity and content metrics
    :param rc_days_limit: The number of days to look back when retrieving Recent Changes
    :param headers: Headers to include in HTTP requests (e.g. user-agent)
    :return: JSON-serializable dict of wiki metadata in standardized format
    """
    # If the provided URL is not an API URL, determine the MediaWiki API URL
    if wiki_url.endswith("/api.php"):
        api_url = wiki_url
    else:
        api_url = get_mediawiki_api_url(wiki_url, headers=headers)
        if api_url is None:
            raise MediaWikiAPIError("Unable to determine MediaWiki API URL")

    # Request siteinfo data
    siteinfo_params = {'format': 'json', 'action': 'query', 'meta': 'siteinfo',
                       'siprop': 'general|namespaces|statistics|rightsinfo'}
    siteinfo = query_mediawiki_api(api_url, params=siteinfo_params, headers=headers)

    if not full_profile:
        return siteinfo

    # Request recentchanges data
    recent_edit_count, latest_edit_timestamp = profile_mediawiki_recentchanges(api_url, rc_days_limit, siteinfo,
                                                                               headers=headers)

    # Extract data
    wiki_metadata = extract_metadata_from_siteinfo(siteinfo)
    wiki_metadata.update({
        # Activity & content metrics
        "content_pages": siteinfo["statistics"]["articles"],
        "active_users": siteinfo["statistics"]["activeusers"],
        "recent_edit_count": recent_edit_count,
        "latest_edit_timestamp": str(latest_edit_timestamp) if latest_edit_timestamp is not None else None,
    })

    return wiki_metadata
