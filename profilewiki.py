"""
Python script for generating metadata about wikis
"""
import datetime
import re
from requests.exceptions import HTTPError, SSLError
from urllib.parse import urlparse
from typing import Optional

from scrapewiki import query_mediawiki_api, query_mediawiki_api_with_continue, MediaWikiAPIError, extract_hostname


def extract_mediawiki_version(generator_string):
    match = re.match(r"MediaWiki (\d+\.\d+\.\d+)(?:\+.*)?", generator_string)
    return match.group(1)


def profile_mediawiki_recentchanges(api_url: str, rc_days_limit: int, siteinfo: dict,
                                    headers: Optional[dict] = None) -> tuple[int, str]:
    """
    Determines the number of content-namespace edits by humans to the wiki within the last X days,
    and the date of the most recent content-namespace edit by a human.

    :param api_url: MediaWiki API URL
    :param rc_days_limit: The number of days to lookback when retrieving Recent Changes
    :param siteinfo: Result of a previous MediaWiki siteinfo query
    :param headers: Headers to include in the request (e.g. user-agent)
    :return: number of edits in the time window, and timestamp of the last edit
    """
    # Determine content namespaces
    content_namespaces = [entry.get('id') for entry in siteinfo["namespaces"].values() if
                          entry.get('content') is not None]

    # Calculate rcend
    # NOTE: The MediaWiki API is very particular about date formats. Timezone must be written in Z format.
    #current_timestamp = datetime.datetime.fromisoformat(siteinfo["general"]["time"])  # Not supported until Python 3.11
    current_timestamp = datetime.datetime.strptime(siteinfo["general"]["time"], '%Y-%m-%dT%H:%M:%S%z')
    window_end = current_timestamp - datetime.timedelta(rc_days_limit)
    assert window_end.tzinfo == datetime.timezone.utc  # TODO: Handle wikis using a timezone other than UTC
    rcend = datetime.datetime.strftime(window_end, '%Y-%m-%dT%H:%M:%SZ')

    # Prepare query parameters
    rcnamespace = '|'.join(str(ns) for ns in content_namespaces)

    recentchanges_params = {'list': 'recentchanges', 'rcshow': '!bot', 'rclimit': 'max',
                            'rctype': 'edit|new|categorize', 'rcnamespace': rcnamespace, 'rcend': rcend}

    # Retrieve all content namespace edits (incl. page creations) in the last [days_lookback] days performed by humans
    rc_contents = []
    for result in query_mediawiki_api_with_continue(api_url, recentchanges_params, headers=headers):
        rc_contents += result['recentchanges']

    # Count edits in the time window
    edit_count = len(rc_contents)

    # Find latest edit
    if len(rc_contents) > 0:
        latest_edit_timestamp = rc_contents[0].get("timestamp")

    # If there were no edits in the specified time window, redo the request without specifying a time restriction
    else:
        recentchanges_params.pop('rcend')
        recentchanges_params['rclimit'] = 1
        rc_extended = next(query_mediawiki_api_with_continue(api_url, recentchanges_params, headers=headers))
        rc_contents = rc_extended["recentchanges"]
        if len(rc_contents) > 0:
            latest_edit_timestamp = rc_contents[0].get("timestamp")
        else:
            latest_edit_timestamp = None

    return edit_count, latest_edit_timestamp


def profile_mediawiki_site(api_url, rc_days_limit=30, headers: Optional[dict] = None):
    # Request siteinfo data
    siteinfo_params = {'format': 'json', 'action': 'query', 'meta': 'siteinfo',
                       'siprop': 'general|namespaces|statistics|rightsinfo'}
    siteinfo = query_mediawiki_api(api_url, params=siteinfo_params, headers=headers)

    # Request recentchanges data
    recent_edit_count, latest_edit = profile_mediawiki_recentchanges(api_url, rc_days_limit, siteinfo, headers=headers)

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
        "latest_edit_timestamp": latest_edit,

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

    # Get API URL
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
