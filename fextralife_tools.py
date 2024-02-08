import datetime
import lxml.etree
import lxml.html
import pandas as pd
import re
import requests
from io import BytesIO
from typing import Optional, Callable
from urllib.parse import urlparse, urlunparse, urljoin, quote as urllib_quote

from utils import extract_xpath_property


def extract_metadata_from_fextralife_page(response: requests.Response) -> dict:
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


def retrieve_segmented_recentchanges(base_url: str, window_end: datetime.datetime,
                                     url_constructor: Callable[[str, int], str],
                                     rc_parser: Callable[[requests.Response], pd.DataFrame],
                                     offset_increment: int = 1, timestamp_col: str = "timestamp",
                                     headers: Optional[dict] = None) -> pd.DataFrame:
    """
    Retrieve paginated Recent Changes.

    Results outside the window will typically be included at the end of the table.
    They are not filtered out in order to allow checking the most recent edit's timestamp, even if it is outside the
    window.

    :param base_url: Base URL for the url_constructor function
    :param window_end: Date of the earliest Recent Changes entry to include
    :param url_constructor: Function that takes the base_url and offset, and outputs the corresponding RC page's URL
    :param rc_parser: Function that takes the RC HTTP response and returns the RC as a DataFrame
    :param offset_increment: Value to increase the offset by each iteration
    :param timestamp_col: Column containing the timestamp
    :param headers: Headers to include in HTTP requests (e.g. user-agent)
    :return: DataFrame of Recent Changes
    """
    rc_fragments = []
    offset = 0
    earliest_timestamp = datetime.datetime.now()
    while earliest_timestamp >= window_end:
        # Request next page of Recent Changes
        rc_page_url = url_constructor(base_url, offset)
        response = requests.get(rc_page_url, headers=headers)
        if not response:
            response.raise_for_status()

        # Parse response
        rc_fragment_df = rc_parser(response)
        rc_fragments.append(rc_fragment_df)

        # Update loop variables
        earliest_timestamp = rc_fragment_df[timestamp_col].min()
        offset += offset_increment

    rc_df = pd.concat(rc_fragments)

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
    def parse_fextralife_recentchanges(response: requests.Response) -> pd.DataFrame:
        rc_fragment_df = pd.DataFrame(response.json()).set_index('id')
        rc_fragment_df["date"] = rc_fragment_df["date"].astype('datetime64[ms]')
        return rc_fragment_df

    def compose_fextralife_recentchanges_url(rc_base_url: str, offset: int) -> str:
        """
        This function builds all the API parameters, despite only ever varying the offset, mostly just to document the
        RC API URL structure in case other parameters need to be varied in the future.
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
        url = urljoin(rc_base_url, url_path)
        return url

    rc_df = retrieve_segmented_recentchanges(base_url, window_end, compose_fextralife_recentchanges_url,
                                             parse_fextralife_recentchanges, timestamp_col="date", headers=headers)

    # Drop duplicated RC entries (duplicates can occur if edits are made between GET requests)
    rc_df = rc_df[~rc_df.index.duplicated(keep='first')]

    return rc_df


def profile_fextralife_wiki(wiki_page: str | requests.Response, full_profile: bool = True,
                            rc_days_limit: int = 30, headers: Optional[dict] = None) -> dict:
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
    if isinstance(wiki_page, str):
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