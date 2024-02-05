"""
Python script for generating metadata about wikis
"""
import datetime
import re
import pandas as pd
from requests.exceptions import HTTPError, SSLError
from urllib.parse import urlparse
from typing import Optional

from scrapewiki import query_mediawiki_api, query_mediawiki_api_with_continue, MediaWikiAPIError, extract_hostname


def extract_mediawiki_version(generator_string):
    match = re.match(r"MediaWiki (\d+\.\d+\.\d+)(?:\+.*)?", generator_string)
    return match.group(1)


def retrieve_mediawiki_recentchanges(api_url: str, window_end: datetime.datetime, extra_params: Optional[dict] = None,
                                     headers: Optional[dict] = None) -> pd.DataFrame:
    """
    Returns the full set of Recent Changes back to a specific date.
    By default, it excludes bots and only includes page edits, page creations, and category additions.

    :param api_url: MediaWiki API URL
    :param window_end: Date of the earliest Recent Changes entry to include
    :param extra_params: Parameters to include in the Recent Changes query, beyond the default values
    :param headers: Headers to include in the request (e.g. user-agent)
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
    rc_df["timestamp"] = pd.to_datetime(rc_df["timestamp"])
    return rc_df


def profile_mediawiki_recentchanges(api_url: str, rc_days_limit: int, siteinfo: dict,
                                    headers: Optional[dict] = None) -> tuple[int, Optional[datetime.datetime]]:
    """
    Determines the number of content-namespace edits by humans to the wiki within the last X days,
    and the date of the most recent content-namespace edit by a human.

    :param api_url: MediaWiki API URL
    :param rc_days_limit: The number of days to lookback when retrieving Recent Changes
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


def profile_mediawiki_site(api_url: str, rc_days_limit: int = 30, headers: Optional[dict] = None):
    # Request siteinfo data
    siteinfo_params = {'format': 'json', 'action': 'query', 'meta': 'siteinfo',
                       'siprop': 'general|namespaces|statistics|rightsinfo'}
    siteinfo = query_mediawiki_api(api_url, params=siteinfo_params, headers=headers)

    # Request recentchanges data
    recent_edit_count, latest_edit_timestamp = profile_mediawiki_recentchanges(api_url, rc_days_limit, siteinfo,
                                                                               headers=headers)

    # Extract data
    site_metadata = {
        # Basic information
        "title": siteinfo["general"]["sitename"],
        "base_url": extract_hostname(siteinfo["general"]["server"]),
        "language": siteinfo["general"]["lang"],

        # Technical data
        "wikiid": siteinfo["general"]["wikiid"],
        "software": "MediaWiki",
        "software_version": extract_mediawiki_version(siteinfo["general"]["generator"]),
        "protocol": urlparse(siteinfo["general"]["base"]).scheme,

        # Activity & content metrics
        "content_pages": siteinfo["statistics"]["articles"],
        "active_users": siteinfo["statistics"]["activeusers"],
        "recent_edit_count": recent_edit_count,
        "latest_edit_timestamp": str(latest_edit_timestamp) if latest_edit_timestamp is not None else None,

        # Licensing
        "licence_name": siteinfo["rightsinfo"]["text"],
        "licence_page": siteinfo["rightsinfo"]["url"],

        # Properties useful for constructing IWB redirect entries
        "content_path": siteinfo["general"]["articlepath"].rstrip('$1'),
        "main_page": siteinfo["general"]["mainpage"],
        "search_path": siteinfo["general"]["script"],
        "logo": siteinfo["general"].get("logo"),
        "favicon": siteinfo["general"].get("favicon"),
    }

    return site_metadata


def main():
    import json
    from scrapewiki import get_mediawiki_api_url

    headers = {'User-Agent': 'Mozilla/5.0'}

    # Take site URL as input
    wiki_url = ""
    while wiki_url.strip() == "":
        wiki_url = input(f"📥 Enter wiki URL: ")

    if wiki_url.endswith("/api.php"):
        api_url = wiki_url
    # Get API URL
    else:
        print(f"🕑 Retrieving wiki's API URL")
        try:
            api_url = get_mediawiki_api_url(wiki_url, headers=headers)
        except (HTTPError, ConnectionError, SSLError) as e:
            print(e)
            return
        if api_url is None:
            print(f"🗙 Unable to retrieve wiki's API URL")
            return

    # Retrieve wiki metadata
    print(f"🕑 Submitting queries to {api_url}")
    try:
        site_metadata = profile_mediawiki_site(api_url, rc_days_limit=30, headers=headers)
    except (HTTPError, ConnectionError, SSLError, MediaWikiAPIError) as e:
        print(e)
        return

    # Print retrieved metadata
    print(json.dumps(site_metadata, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
