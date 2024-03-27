"""
Python script for generating metadata about wikis
"""
import lxml.html
import pandas as pd
import re
import requests
import time
import warnings
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Optional, Generator
from urllib.parse import urlparse, urlunparse

from utils import (extract_xpath_property, ensure_absolute_url, normalize_url_protocol, resolve_wiki_page,
                   detect_wikifarm)


class MediaWikiAPIError(Exception):
    """
    Errors returned by the MediaWiki API
    """
    pass


class MediaWikiAPIWarning(Warning):
    """
    Warnings returned by the MediaWiki API
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
    new_path = lang + "/" + parsed_url.path.removeprefix("/")
    parsed_url = parsed_url._replace(netloc=new_domain, path=new_path)

    return str(urlunparse(parsed_url))


def extract_mediawiki_version(generator_string: str) -> str:
    match = re.match(r"MediaWiki (\d+\.\d+\.\d+)(?:\+.*)?", generator_string)
    return match.group(1)


def get_mediawiki_favicon_url(wiki_page: str | requests.Response, session: Optional[requests.Session] = None,
                              **kwargs) -> Optional[str]:
    """
    Given an HTTP response for a MediaWiki page, determines the wiki's favicon's URL.

    :param wiki_page: MediaWiki wiki page URL or HTTP request response
    :param session: requests Session to use for resolving the URL
    :param kwargs: kwargs to use for the HTTP requests
    :return: Favicon URL
    """
    # Create a new session if one was not provided
    if session is None:
        session = requests.Session()

    # Parse the page HTML
    response = resolve_wiki_page(wiki_page, session=session, **kwargs)
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

    # Retrieve the page's URL
    return ensure_absolute_url(icon_url, response.url)


def get_mediawiki_api_url(wiki_page: str | requests.Response, session: Optional[requests.Session] = None,
                          **kwargs) -> Optional[str]:
    """
    Given a URL or HTTP response for a wiki page, determines the wiki's API URL.

    :param wiki_page: URL or HTTP response for a wiki page
    :param session: requests Session to use for resolving the URL
    :param kwargs: kwargs to use for the HTTP requests
    :return: Wiki's API URL
    """
    # If provided an API URL or a response from the API page, just return that URL
    if isinstance(wiki_page, str) and wiki_page.endswith("/api.php"):
        api_url = wiki_page
        return api_url
    if isinstance(wiki_page, requests.Response) and wiki_page.url.endswith("/api.php"):
        api_url = wiki_page.url
        return api_url

    # If provided a URL, run an HTTP request
    response = resolve_wiki_page(wiki_page, session=session, **kwargs)

    # Parse the HTML
    parsed_html = lxml.html.parse(BytesIO(response.content))

    # Retrieve the API URL via EditURI element
    api_url = extract_xpath_property(parsed_html, '//link[@rel="EditURI"]', "href")
    if api_url is not None:
        # Delete the query from the API URL (usually this element's API URL includes '?action=rsd')
        api_url = str(urlparse(api_url)._replace(query="").geturl())
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
            api_url = str(urlparse(api_url)._replace(query="").geturl())

            return ensure_absolute_url(api_url, response.url)

    # If the page is a BreezeWiki page, identify the original Fandom URL and retrieve the API URL from Fandom
    if ".fandom.com" not in response.url:
        fandom_url = get_fandom_url_from_breezewiki(response)
        if fandom_url is not None:
            print(f"ℹ {response.url} is a BreezeWiki site. Retrieving API URL from Fandom.")
            return get_mediawiki_api_url(fandom_url, session=session, **kwargs)

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


def print_api_warnings(api_warnings: dict):
    for warning_group_name, warning_group in api_warnings.items():
        for warning_entry in warning_group.values():
            warnings.warn(f"{warning_group_name}: {warning_entry}", MediaWikiAPIWarning)


def query_mediawiki_api(api_url: str, params: dict, session: Optional[requests.Session] = None, **kwargs) -> dict:
    """
    Runs a MediaWiki API query with the specified parameters.

    :param api_url: MediaWiki API URL
    :param params: params to use for HTTP requests
    :param session: requests Session to use for HTTP requests
    :param kwargs: kwargs to use for HTTP requests
    :return: API query result
    :raises: HTTPError: If the API request returns an HTTP error code
    :raises: MediaWikiAPIError: If the API query returns an error
    """
    # Create a new session if one was not provided
    if session is None:
        session = requests.Session()

    # GET request API query
    response = session.get(api_url, params=params, **kwargs)

    # If the response is Error 429 (Too Many Requests), sleep for 30 seconds then try again (once only)
    if response.status_code == 429:
        print(f"🕑 Error 429 {response.reason}. Sleeping for 30 seconds...")
        time.sleep(30)
        response = session.get(api_url, params=params, **kwargs)

    # If the response is an error, raise an HTTPError
    if not response:
        response.raise_for_status()

    # Parse as JSON
    result = response.json()

    # Check for errors and warnings
    if 'error' in result:
        raise MediaWikiAPIError(result['error'])
    if 'warnings' in result:
        print_api_warnings(result['warnings'])

    return result['query']


def query_mediawiki_api_with_continue(api_url: str, params: dict, session: Optional[requests.Session] = None,
                                      **kwargs) -> Generator[dict, None, None]:
    """
    Runs a MediaWiki API query with the specified parameters.

    Based on https://www.mediawiki.org/wiki/API:Continue#Example_3:_Python_code_for_iterating_through_all_results

    :param api_url: MediaWiki API URL
    :param params: params to use for HTTP requests
    :param session: requests Session to use for HTTP requests
    :param kwargs: kwargs to use for the HTTP requests
    :return: Generator of API query results
    :raises: HTTPError: If the API request returns an HTTP error code
    :raises: MediaWikiAPIError: If the API query returns an error
    """
    # Create a new session if one was not provided
    if session is None:
        session = requests.Session()

    params['action'] = 'query'
    params['format'] = 'json'
    last_continue = {}
    while True:
        # Clone original request
        request_params = params.copy()
        # Modify it with the values returned in the 'continue' section of the last result.
        request_params.update(last_continue)
        # Call API
        response = session.get(api_url, params=request_params, **kwargs)

        # If the response is an error, raise an HTTPError
        if not response:
            response.raise_for_status()

        # Process result
        result = response.json()

        if 'error' in result:
            raise MediaWikiAPIError(result['error'])
        if 'warnings' in result:
            print_api_warnings(result['warnings'])
        if 'query' in result:
            yield result['query']
        if 'continue' not in result:
            break
        last_continue = result['continue']


def retrieve_all_mediawiki_content_pages(api_url: str, content_namespaces: Optional[list[int | str]] = None,
                                         session: Optional[requests.Session] = None, **kwargs) -> pd.DataFrame:
    """
    Generates a DataFrame of all content pages (articles) on a specific MediaWiki.

    This can be used to manually count the number of content namespace pages on a MediaWiki wiki when the number
    returned by the API is inaccurate.

    :param api_url: MediaWiki API URL
    :param content_namespaces: List of namespace IDs for all content namespaces on the wiki
    :param session: requests Session to use for HTTP requests
    :param kwargs: kwargs to use for the HTTP requests
    :return: DataFrame of all content pages on the wiki
    """
    # Create a new session if one was not provided
    if session is None:
        session = requests.Session()

    # If the list of content namespaces were not provided, request them
    if content_namespaces is None:
        siteinfo_params = {'format': 'json', 'action': 'query', 'meta': 'siteinfo', 'siprop': 'namespaces'}
        siteinfo = query_mediawiki_api(api_url, params=siteinfo_params, session=session, **kwargs)

        content_namespaces = [entry.get('id') for entry in siteinfo["namespaces"].values()
                              if entry.get('content') is not None]

    # Request all articles in each of the content namespaces
    all_articles = []
    for ns in content_namespaces:
        query_params = {'action': 'query', 'list': 'allpages', 'apnamespace': ns, 'aplimit': 'max', 'format': 'json'}
        # Execute query, iterating over each continuation (MediaWiki typically returns up to 500 results per query)
        for result in query_mediawiki_api_with_continue(api_url, params=query_params, session=session, **kwargs):
            all_articles += result['allpages']

    articles_df = pd.DataFrame(all_articles)
    return articles_df


def extract_metadata_from_siteinfo(siteinfo: dict) -> dict:
    """
    Extracts the important data from a siteinfo result, and transforms it into a standardized format

    :param siteinfo: MediaWiki API response for a "siteinfo" query including siprop=general
    :return: Standardized site properties
    """
    base_url = str(urlparse(siteinfo["general"]["base"]).hostname)

    # Retrieve normalized language
    full_language = siteinfo["general"]["lang"]  # NOTE: The language retrieved this way may include the dialect
    normalized_language = full_language.split('-')[0]

    # Retrieve path properties
    search_path = siteinfo["general"].get("script")
    content_path = siteinfo["general"].get("articlepath")
    if content_path is not None:
        content_path = content_path.replace("$1", "")
    # In ancient versions of MediaWiki, the content_path is not returned by the API
    else:
        encoded_main_page = siteinfo["general"]["mainpage"].replace(" ", "_")
        main_page_path = urlparse(siteinfo["general"]["base"]).path
        assert main_page_path.endswith(encoded_main_page)
        content_path = main_page_path.removesuffix(encoded_main_page)

    # For non-English Fandom and wiki.gg wikis, place the language path in the base_url instead of path properties
    script_path = siteinfo["general"].get("scriptpath")
    if (".fandom.com" in base_url or ".wiki.gg" in base_url) and script_path != "":
        base_url += script_path
        content_path = content_path.removeprefix(script_path)
        search_path = search_path.removeprefix(script_path)

    # Apply standard wiki name changes
    wiki_name = siteinfo["general"]["sitename"]
    if ".fandom.com" in base_url:
        wiki_name = wiki_name.replace(" Wiki", " Fandom Wiki")

    # Detect if the wiki is on a wikifarm
    logo_path = siteinfo["general"].get("logo", "")  # Not guaranteed to be present
    wikifarm = detect_wikifarm([base_url, logo_path])

    # Get favicon path
    favicon_path = siteinfo["general"].get("favicon")  # Not guaranteed to be present
    if favicon_path is not None:
        # On Fandom, the API's favicon URL path starts with $wgUploadPath. For now, just ignore these kinds of paths.
        if favicon_path.startswith("$"):
            favicon_path = None
        else:
            favicon_path = ensure_absolute_url(favicon_path, siteinfo["general"]["base"])

    # Return extracted properties
    wiki_metadata = {
        # Basic information
        "name": wiki_name,
        "base_url": base_url,
        "full_language": full_language,
        "language": normalized_language,

        # Technical data
        "wiki_id": siteinfo["general"].get("wikiid"),
        "wikifarm": wikifarm,
        "platform": "MediaWiki".lower(),
        "software_version": extract_mediawiki_version(siteinfo["general"]["generator"]),

        # Paths
        "protocol": urlparse(siteinfo["general"]["base"]).scheme,
        "main_page": siteinfo["general"]["mainpage"].replace(" ", "_"),
        "content_path": content_path,
        "search_path": search_path,
        "icon_path": favicon_path,

        # Licensing
        "licence_name": siteinfo["rightsinfo"]["text"] if "rightsinfo" in siteinfo else None,
        "licence_page": siteinfo["rightsinfo"]["url"] if "rightsinfo" in siteinfo else None,
    }

    return wiki_metadata


def retrieve_mediawiki_recentchanges(api_url: str, window_end: datetime, extra_params: Optional[dict] = None,
                                     session: Optional[requests.Session] = None, **kwargs) -> pd.DataFrame:
    """
    Returns the full set of Recent Changes back to a specific date.
    By default, it excludes bots and only includes page edits, page creations, and category additions.

    :param api_url: MediaWiki API URL
    :param window_end: Date of the earliest Recent Changes entry to include
    :param extra_params: Parameters to include in the Recent Changes query, beyond the default values
    :param session: requests Session to use for HTTP requests
    :param kwargs: kwargs to use for the HTTP requests
    :return: DataFrame of Recent Changes
    """
    # Prepare query params
    # NOTE: The MediaWiki API is very particular about date formats. Timezone must be written in Z format.
    window_end = window_end.astimezone(timezone.utc)  # Force UTC
    rcend = window_end.strftime('%Y-%m-%dT%H:%M:%SZ')
    query_params = {'action': 'query', 'list': 'recentchanges', 'rcshow': '!bot', 'rclimit': 'max', 'rcend': rcend,
                    'rctype': 'edit|new|categorize', 'format': 'json'}
    if extra_params is not None:
        query_params.update(extra_params)

    # Execute query, iterating over each continuation (MediaWiki typically returns up to 500 results per query)
    rc_contents = []
    for result in query_mediawiki_api_with_continue(api_url, params=query_params, session=session, **kwargs):
        rc_contents += result['recentchanges']

    rc_df = pd.DataFrame(rc_contents)
    if not rc_df.empty:
        rc_df["timestamp"] = pd.to_datetime(rc_df["timestamp"])
    return rc_df


def profile_mediawiki_recentchanges(api_url: str, rc_days_limit: int, siteinfo: dict,
                                    session: Optional[requests.Session] = None,
                                    **kwargs) -> tuple[int, Optional[datetime]]:
    """
    Determines the number of content-namespace edits by humans to the wiki within the last X days,
    and the date of the most recent content-namespace edit by a human.

    :param api_url: MediaWiki API URL
    :param rc_days_limit: The number of days to look back when retrieving Recent Changes
    :param siteinfo: Result of a previous MediaWiki siteinfo query
    :param session: requests Session to use for HTTP requests
    :param kwargs: kwargs to use for the HTTP requests
    :return: number of edits in the time window, and timestamp of the last edit
    """
    # Calculate window_end
    siteinfo_timestamp = siteinfo["general"].get("time")
    if siteinfo_timestamp is not None:
        #current_timestamp = datetime.fromisoformat(siteinfo["general"]["time"])  # Not supported until Python 3.11
        current_timestamp = datetime.strptime(siteinfo["general"]["time"], '%Y-%m-%dT%H:%M:%S%z')
        current_timestamp = current_timestamp.astimezone(timezone.utc) # Force UTC
    else:
        current_timestamp = datetime.now(timezone.utc)
    window_end = current_timestamp - timedelta(rc_days_limit)

    # Determine content namespaces
    content_namespaces = [entry.get('id') for entry in siteinfo["namespaces"].values()
                          if entry.get('content') is not None]

    # Retrieve Recent Changes
    extra_params = {
        "rcnamespace": '|'.join(str(ns) for ns in content_namespaces),
        "rcend": window_end.strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    rc_df = retrieve_mediawiki_recentchanges(api_url, window_end, extra_params=extra_params, **kwargs)

    # Count edits in the time window
    edit_count = len(rc_df)

    # Find latest edit
    if not rc_df.empty:
        latest_edit_timestamp = rc_df["timestamp"].max()

    # If there were no edits in the time window, request Recent Changes without a time restriction
    else:
        query_params = {'action': 'query', 'list': 'recentchanges', 'rcshow': '!bot', 'rclimit': 1,
                        'rctype': 'edit|new|categorize', "rcnamespace": '|'.join(str(ns) for ns in content_namespaces),
                        'format': 'json'}
        rc_extended_result = query_mediawiki_api(api_url, query_params, session=session, **kwargs)

        # Get the most recent edit from the Recent Changes result
        rc_extended_df = pd.DataFrame(rc_extended_result["recentchanges"])
        if not rc_extended_df.empty:
            latest_edit_timestamp = pd.to_datetime(rc_extended_df["timestamp"]).max()
        else:
            latest_edit_timestamp = None

    return edit_count, latest_edit_timestamp


def profile_mediawiki_wiki(wiki_page: str | requests.Response, full_profile: bool = True, rc_days_limit: int = 30,
                           session: Optional[requests.Session] = None, **kwargs) -> dict:
    """
    Uses the MediaWiki API to retrieve key information about the specified MediaWiki site,
    including content and activity metrics.

    :param wiki_page: MediaWiki wiki page URL or HTTP request response
    :param full_profile: Whether to include activity and content metrics
    :param rc_days_limit: The number of days to look back when retrieving Recent Changes
    :param session: requests Session to use for resolving the URL
    :param kwargs: kwargs to use for the HTTP requests
    :return: JSON-serializable dict of wiki metadata in standardized format
    """
    # Create a new session if one was not provided
    if session is None:
        session = requests.Session()

    # If not provided an API URL, use the wiki page to find the API URL
    if isinstance(wiki_page, str) and wiki_page.endswith("/api.php"):
        api_url = wiki_page
    else:
        api_url = get_mediawiki_api_url(wiki_page, **kwargs)
        if api_url is None:
            raise MediaWikiAPIError("Unable to determine MediaWiki API URL")

    # Request siteinfo data
    siteinfo_params = {'format': 'json', 'action': 'query', 'meta': 'siteinfo',
                       'siprop': 'general|namespaces|statistics|rightsinfo'}
    siteinfo = query_mediawiki_api(api_url, params=siteinfo_params, session=session, **kwargs)
    wiki_metadata = extract_metadata_from_siteinfo(siteinfo)

    # If the search path was not retrieved, manually derive it from the API URL
    if wiki_metadata.get("search_path") is None:
        wiki_metadata["search_path"] = urlparse(api_url).path.replace("/api.php", "/index.php")

    # If the icon URL was not found via the standard method, try to find it from the HTML
    if wiki_metadata.get("icon_path") is None:
        if wiki_page != api_url:
            wiki_metadata["icon_path"] = get_mediawiki_favicon_url(wiki_page)
        else:
            full_content_path = str(urlunparse((wiki_metadata["protocol"], wiki_metadata["base_url"],
                                                wiki_metadata["content_path"], '', '', '')))
            wiki_metadata["icon_path"] = get_mediawiki_favicon_url(full_content_path)

    if not full_profile:
        return wiki_metadata

    # Retrieve Recent Changes
    recent_edit_count, latest_edit_timestamp = profile_mediawiki_recentchanges(api_url, rc_days_limit, siteinfo,
                                                                               session=session, **kwargs)

    # Extract data
    wiki_metadata.update({
        # Activity & content metrics
        "content_pages": siteinfo["statistics"]["articles"],
        "active_users": siteinfo["statistics"]["activeusers"],
        "recent_edit_count": recent_edit_count,
        "latest_edit_timestamp": str(latest_edit_timestamp) if latest_edit_timestamp is not None else None,
    })

    return wiki_metadata
