"""
Python script for generating metadata about wikis
"""
import datetime
import pandas as pd
import requests
import json
import lxml.etree
import lxml.html
from requests.exceptions import HTTPError, SSLError
from urllib.parse import urlparse, urlunparse, urljoin, quote as urllib_quote
from typing import Optional

from scrapewiki import (get_mediawiki_api_url, query_mediawiki_api, query_mediawiki_api_with_continue,
                        extract_metadata_from_siteinfo, extract_metadata_from_fextralife_page, determine_wiki_software,
                        normalize_url_protocol, WikiSoftware, MediaWikiAPIError)


def retrieve_fextralife_sitemap(base_url: str, headers: Optional[dict] = None) -> lxml.etree:
    """
    Retrieves and parses the sitemap for a specified Fextralife wiki.

    :param base_url: Fextralife wiki domain (including protocol, excluding a path)
    :param headers: Headers to include in the HTTP request (e.g. user-agent)
    :return: Parsed sitemap
    """
    # Retrieve sitemap
    url = urljoin(base_url, 'sitemap.xml')
    response = requests.get(url, headers=headers)
    if not response:
        response.raise_for_status()

    # Parse sitemap
    parsed_sitemap = lxml.etree.fromstring(response.content)

    return parsed_sitemap


def compose_fextralife_recentchanges_url(base_url: str, offset: int) -> str:
    """
    Builds a Fextralife Recent Changes API URL for a specified offset.

    This function builds all the API parameters, despite only ever varying the offset.
    This is mostly just to document the RC API URL structure in case other parameters need to be varied in the future.

    :param base_url: Fextralife wiki domain (including protocol, excluding a path)
    :param offset: Recent Changes offset
    :return: Fextralife Recent Changes API URL
    """

    # Prepare other arguments
    author_filter = urllib_quote("{none}")
    date_filter = urllib_quote("{none}")

    # Prepare param flags
    param_flags = [
        False,  # (always False; named 'isIP')
        True,  # Include actions on Pages
        False,  # (always False)
        True,  # Include actions on Templates
        False,  # Include forum activity
        True,  # Include actions on Files
        False,  # (always False)
        False,  # Include unregistered users (defaults to all users if neither flag is True)
        True,  # Include registered users (defaults to all users if neither flag is True)
    ]
    param_flags_string = '|'.join([str(int(flag)) for flag in param_flags])

    # Construct URL
    url_path = f"/ws/wikichangemanager/wiki/changes/{offset}/{author_filter}/{date_filter}/{param_flags_string}"
    url = urljoin(base_url, url_path)
    return url


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


def retrieve_fextralife_recentchanges(base_url: str, window_end: datetime.datetime,
                                      headers: Optional[dict] = None) -> pd.DataFrame:
    """
    Retrieve Recent Changes from a Fextralife wiki within the specified time window.

    Results outside the window will typically be included at the end of the table.
    They are not filtered out in order to allow checking the most recent edit's timestamp, even if it is outside the
    window.

    :param base_url: Fextralife wiki domain (including protocol, excluding a path)
    :param window_end: Date of the earliest Recent Changes entry to include
    :param headers: Headers to include in HTTP requests (e.g. user-agent)
    :return: DataFrame of Recent Changes
    """
    rc_fragments = []
    offset = 0
    earliest_timestamp = datetime.datetime.now()
    while earliest_timestamp >= window_end:
        # API request
        fextralife_rc_url = compose_fextralife_recentchanges_url(base_url, offset)
        response = requests.get(fextralife_rc_url, headers=headers)
        if not response:
            response.raise_for_status()

        # Parse response
        rc_fragment_df = pd.DataFrame(response.json()).set_index('id')
        rc_fragment_df["date"] = rc_fragment_df["date"].astype('datetime64[ms]')
        rc_fragments.append(rc_fragment_df)

        # Update loop variables
        earliest_timestamp = rc_fragment_df["date"].min()
        offset += 1

    rc_df = pd.concat(rc_fragments)

    # Drop duplicated RC entries (duplicates can occur if edits are made between GET requests)
    rc_df = rc_df[~rc_df.index.duplicated(keep='first')]

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


def profile_mediawiki_wiki(api_url: str, full_profile: bool = True,
                           rc_days_limit: int = 30, headers: Optional[dict] = None) -> dict:
    """
    Uses the MediaWiki API to retrieve key information about the specified MediaWiki site,
    including content and activity metrics.

    :param api_url: MediaWiki API URL
    :param full_profile: Whether to include activity and content metrics
    :param rc_days_limit: The number of days to look back when retrieving Recent Changes
    :param headers: Headers to include in HTTP requests (e.g. user-agent)
    :return: JSON-serializable dict of wiki metadata in standardized format
    """
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


def profile_fextralife_wiki(wiki_page: str | requests.Response, full_profile: bool = True,
                            rc_days_limit: int = 30, headers: Optional[dict] = None):
    """
    Given a URL or HTTP request response for a page of a Fextralife wiki, retrieves key information about the wiki,
    including content and activity metrics.

    :param wiki_page: Fextralife wiki page URL or HTTP request response
    :param full_profile: Whether to include activity and content metrics
    :param rc_days_limit: The number of days to look back when retrieving Recent Changes
    :param headers: Headers to include in HTTP requests (e.g. user-agent)
    :return: JSON-serializable dict of wiki metadata in standardized format
    """

    # If provided a URL, run an HTTP request
    if type(wiki_page) is str:
        url = wiki_page
        response = requests.get(url, headers=headers)
        if not response:
            response.raise_for_status()
    else:
        response = wiki_page
        url = response.url

    base_url = str(urlunparse(urlparse(url)._replace(path="")))

    # Extract metadata from the main page
    wiki_metadata = extract_metadata_from_fextralife_page(response)

    if not full_profile:
        return wiki_metadata

    # Request the sitemap and Recent Changes
    window_end = datetime.datetime.now() - datetime.timedelta(rc_days_limit)
    sitemap = retrieve_fextralife_sitemap(base_url, headers=headers)
    rc_df = retrieve_fextralife_recentchanges(base_url, window_end=window_end, headers=headers)
    recent_rc_df = rc_df[rc_df["date"] > window_end]

    # Content edits are Page edits, Page creations, and Page reversions
    content_edit_actions = ["Page_Edited", "Page_Created", "Page_Version_Restored"]

    # Extract data
    wiki_metadata.update({
        # Fextralife wiki sitemaps appear to be a definitive listing of exclusively mainspace pages
        "content_pages": len(sitemap),
        # Active users are registered users who have performed any action in the past 30 days
        "active_users": len(recent_rc_df["author"].unique()),
        # Number of content edits made in the past 30 days
        "recent_edit_count": len(recent_rc_df[recent_rc_df["code"].isin(content_edit_actions)]),
        # Timestamp of the most recent content edit
        "latest_edit_timestamp": str(rc_df[rc_df["code"].isin(content_edit_actions)]["date"].max()),
    })
    return wiki_metadata


def profile_wiki(wiki_url: str, full_profile: bool = True, headers: Optional[dict] = None) -> Optional[dict]:
    """
    Given a URL of any type of wiki, retrieves key information about the wiki,
    including content and activity metrics.

    :param wiki_url: Wiki URL
    :param full_profile: Whether to include activity and content metrics
    :param headers: Headers to include in HTTP requests (e.g. user-agent)
    :return: JSON-serializable dict of wiki metadata in standardized format
    """

    # GET request input URL
    response = requests.get(normalize_url_protocol(wiki_url), headers=headers)
    if not response:
        response.raise_for_status()

    # Detect wiki software
    wiki_software = determine_wiki_software(response)

    # Select profiler based on software
    if wiki_software == WikiSoftware.MEDIAWIKI:
        api_url = get_mediawiki_api_url(wiki_url, headers=headers)
        if api_url is None:
            return None
        wiki_metadata = profile_mediawiki_wiki(api_url, full_profile=full_profile, headers=headers)
        return wiki_metadata

    elif wiki_software == WikiSoftware.FEXTRALIFE:
        wiki_metadata = profile_fextralife_wiki(wiki_url, full_profile=full_profile, headers=headers)
        return wiki_metadata

    else:
        return None


def main():
    headers = {'User-Agent': 'Mozilla/5.0'}

    # Take site URL as input
    wiki_url = ""
    while wiki_url.strip() == "":
        wiki_url = input(f"📥 Enter wiki URL: ")

    # Detect wiki software
    print(f"🕑 Resolving input URL...")
    try:
        response = requests.get(normalize_url_protocol(wiki_url), headers=headers)
    except (HTTPError, ConnectionError, SSLError) as e:
        print(e)
        return

    wiki_software = determine_wiki_software(response)

    if wiki_software == WikiSoftware.MEDIAWIKI:
        print(f"ℹ Detected MediaWiki software")

        # Get API URL
        api_url = get_mediawiki_api_url(response, headers=headers)
        if api_url is None:
            print(f"🗙 Unable to retrieve API from {response.url}")
            return

        # Retrieve wiki metadata
        print(f"🕑 Submitting queries to {api_url}")
        try:
            wiki_metadata = profile_mediawiki_wiki(api_url, full_profile=True, rc_days_limit=30, headers=headers)
        except (HTTPError, ConnectionError, SSLError, MediaWikiAPIError) as e:
            print(e)
            return

    elif wiki_software == WikiSoftware.FEXTRALIFE:
        print(f"ℹ Detected Fextralife software")

        # Retrieve wiki metadata
        base_url = urlunparse(urlparse(wiki_url)._replace(path=""))
        print(f"🕑 Submitting queries to {base_url}")
        try:
            wiki_metadata = profile_fextralife_wiki(response, full_profile=True, rc_days_limit=30, headers=headers)
        except (HTTPError, ConnectionError, SSLError, MediaWikiAPIError) as e:
            print(e)
            return

    else:
        print(f"🗙 Unsupported wiki software")
        return

    # Print retrieved metadata
    print(json.dumps(wiki_metadata, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
