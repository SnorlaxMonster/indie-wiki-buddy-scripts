import lxml.html
import pandas as pd
import re
import requests
import feedparser
import warnings
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from io import BytesIO
from requests.exceptions import ConnectionError
from typing import Optional
from urllib.parse import urlparse

from utils import resolve_wiki_page, extract_base_url, ensure_absolute_url, retrieve_sitemap


def determine_wikidot_search_path(base_url: str, session: Optional[requests.Session] = None, **kwargs) -> str:
    """
    Determines the search path of a wikidot wiki.

    :param base_url: Wikidot wiki domain (including protocol, excluding a path)
    :param session: requests Session to use for resolving the URL
    :param kwargs: kwargs to use for the HTTP requests
    :return: search path of the Wikidot wiki
    """

    # Try search:main, the only Wikidot search endpoint known to be functional and have a URL-based API
    candidate_search_url = base_url.removesuffix("/") + "/search:main/fullname"
    try:
        response = session.get(candidate_search_url, **kwargs)
    # If the connection failed, start a new session (this is mostly relevant for HTTP connections)
    except ConnectionError:
        session.close()
        response = session.get(candidate_search_url, **kwargs)
    if response:
        return "/search:main/fullname/"

    # Wikidot's default search (search:site) has been non-functional since May 2022.
    # Wikidot search issue announcement: http://blog.wikidot.com/blog:back-online
    # return "/search:site/q/"

    # While Wikidot search is unavailable, just use direct links and hope that the page title matches exactly.
    # search:crom (used by SCP wikis) and Google CSE are functional, but they seemingly don't have public URL-based APIs
    warnings.warn("Wikidot's default search 'search:site' has been non-functional since May 2022.\n"
                  "This wiki does not appear to support the alternate search extension 'search:main', "
                  "so there is no URL-based search API available for this wiki.\n"
                  "The content path will be used as the search path instead, which will result in 404 errors "
                  "upon redirection if the title does not match exactly.")
    return "/"


def extract_user_from_wikidot_recentchanges_summary(summary_html):
    soup = BeautifulSoup(summary_html, features="lxml")
    ip_element = soup.select_one("span.ip")
    if ip_element is not None:
        ip_element.decompose()
    username = soup.select_one("span.printuser").text.strip()

    return username


def parse_wikidot_page_id(page_id):
    page_id_parts = page_id.split(':', maxsplit=1)
    if len(page_id_parts) > 1:
        return tuple(page_id_parts)
    else:
        return None, page_id


def retrieve_pages_from_wikidot_sitemap(base_url: str, session: Optional[requests.Session] = None,
                                        **kwargs) -> Optional[pd.DataFrame]:
    """
    Retrieves a list of all pages on the specified Wikidot wiki (and their namespaces) from its sitemap.

    :param base_url: Wikidot wiki domain (including protocol, excluding a path)
    :param session: requests Session to use for resolving the URL
    :param kwargs: kwargs to use for the HTTP requests
    :return: DataFrame of all pages on the wiki and their namespaces. If the sitemap cannot be retrieved, returns None.
    """
    # Construct sitemap URL
    sitemap_url = base_url.removesuffix("/") + "/sitemap.xml"

    # Retrieve the sitemap
    sitemap_df = retrieve_sitemap(sitemap_url, session=session, **kwargs)

    # Drop duplicate URLs (these can often be caused by paginated sitemaps)
    sitemap_df = sitemap_df.drop_duplicates("loc", keep="first")

    # Exclude forum URLs
    sitemap_paths = sitemap_df["loc"].apply(lambda url: urlparse(url).path.removeprefix("/"))
    sitemap_root_paths = sitemap_paths.str.split("/", n=1).apply(lambda x: x[0] if len(x) > 1 else None)
    assert all(sitemap_root_paths.isin(["forum", None]))  # The only handled cases are wiki pages and forum pages
    sitemap_paths = sitemap_paths[sitemap_root_paths != "forum"]

    # Extract namespaces and page IDs from the URL paths
    pages_df = pd.DataFrame(sitemap_paths.apply(parse_wikidot_page_id).tolist(), columns=["namespace", "base_page_id"])

    return pages_df


def retrieve_wikidot_recentchanges(base_url: str, session: Optional[requests.Session] = None, **kwargs):
    """
    Retrieve Recent Changes from a Wikidot wiki from the RSS feed.

    Unfortunately, Wikidot's RSS feed only includes 30 entries, with no option to request older changes.
    Additional changes can be viewed on the regular user-facing Recent Changes HTML page, but it only displays 20
    results per page, and requesting additional pages requires a session token. It should theoretically be possible to
    request these pages and parse the Recent Changes from the HTML, but especially due to the token requirement it is a
    non-trivial process. Additionally, most Wikidot wikis are not active enough that they have more than 30 edits in
    the past 30 days, so this limitation is unlikely to be relevant.

    :param base_url: Wikidot wiki domain (including protocol, excluding a path)
    :param session: requests Session to use for resolving the URL
    :param kwargs: kwargs to use for the HTTP requests
    :return: DataFrame of Recent Changes
    """
    # Create a new session if one was not provided
    if session is None:
        session = requests.Session()

    # Request the Recent Changes feed
    feed_url = base_url.removesuffix("/") + "/feed/site-changes.xml"

    try:
        response = session.get(feed_url, **kwargs)
    # If the connection failed, start a new session (this is mostly relevant for HTTP connections)
    except requests.exceptions.ConnectionError:
        session.close()
        response = session.get(feed_url, **kwargs)
    if not response:
        response.raise_for_status()

    # Parse the raw feed contents into a DataFrame
    rc_feed = feedparser.parse(response.content)
    raw_rc_df = pd.DataFrame(rc_feed["entries"])

    # Parse feed as a Recent Changes table
    extracted_url = raw_rc_df["id"].str.extract(r"https?:\/\/[\w\-]+\.wikidot\.com\/(.*)#revision-(\d+)")
    extracted_title = raw_rc_df["title"].str.extract(r'\"(.+)\"\s+-\s*(.*)')

    rc_df = pd.concat([extracted_url, extracted_title], axis=1)
    rc_df.columns = ["page_id", "revision_id", "title", "action"]
    rc_df["namespace"] = rc_df["page_id"].apply(lambda page_id: parse_wikidot_page_id(page_id)[0])
    rc_df["published"] = pd.to_datetime(raw_rc_df["published"])
    rc_df["author"] = raw_rc_df["summary"].apply(extract_user_from_wikidot_recentchanges_summary)
    rc_df = rc_df.set_index("revision_id")

    return rc_df


def profile_wikidot_wiki(wiki_page: str | requests.Response, full_profile: bool = True, rc_days_limit: int = 30,
                            session: Optional[requests.Session] = None, **kwargs) -> dict:
    """
    Given a URL or HTTP request response for a page of a Fextralife wiki, retrieves key information about the wiki,
    including content and activity metrics.

    :param wiki_page: Fextralife wiki page URL or HTTP request response
    :param full_profile: Whether to include activity and content metrics
    :param rc_days_limit: The number of days to look back when retrieving Recent Changes
    :param session: requests Session to use for resolving the URL
    :param kwargs: kwargs to use for the HTTP requests
    :return: JSON-serializable dict of wiki metadata in standardized format
    """
    # Create a new session if one was not provided
    if session is None:
        session = requests.Session()

    # If provided a URL, run an HTTP request
    input_page_response = resolve_wiki_page(wiki_page, session=session, **kwargs)

    # Parse HTML
    input_page_html = lxml.html.parse(BytesIO(input_page_response.content))
    base_url_with_protocol = extract_base_url(input_page_response.url)

    # Extract properties from input page
    language = input_page_html.getroot().get('lang')
    icon_path = input_page_html.find('//link[@rel="shortcut icon"]').get("href")
    licence_elem = input_page_html.find('//div[@id="license-area"]/a[@rel="license"]')
    wikirequest_info_script = input_page_html.xpath('//script[contains(text(), "WIKIREQUEST.info")]')[0].text

    # Request the Main Page and extract its ID
    main_page_response = session.get(base_url_with_protocol, **kwargs)  # Assume the root URL is always the Main Page
    main_page_html = lxml.html.parse(BytesIO(main_page_response.content))
    main_page_wikirequest_info_script = main_page_html.xpath('//script[contains(text(), "WIKIREQUEST.info")]')[0].text
    main_page_id = re.search(r'WIKIREQUEST\.info\.requestPageName = \"(.+)\";',
                             main_page_wikirequest_info_script).group(1)

    # Determine the search path
    search_path = determine_wikidot_search_path(base_url_with_protocol, session=session, **kwargs)

    # Determine licence
    if licence_elem is not None:
        licence_name = licence_elem.text
        licence_page = licence_elem.get("href")
    else:
        licence_name = None
        licence_page = None

    # Extract metadata from the input page
    wiki_metadata = {
        # Basic information
        "name": input_page_html.find('//div[@id="header"]/h1/a/span').text,
        "base_url": urlparse(input_page_response.url).hostname,
        "full_language": language,
        "language": language,

        # Technical data
        "wiki_id": int(re.search(r"WIKIREQUEST\.info\.siteId = (\d+);", wikirequest_info_script).group(1)),
        "wikifarm": "Wikidot".lower(),
        "platform": "Wikidot".lower(),
        "software_version": None,  # N/A

        # Paths
        "protocol": urlparse(input_page_response.url).scheme,
        "main_page": main_page_id,
        "content_path": "/",
        "search_path": search_path,
        "icon_path": ensure_absolute_url(icon_path, base_url_with_protocol),

        # Licensing
        "licence_name": licence_name,
        "licence_page": licence_page,
    }

    if not full_profile:
        return wiki_metadata

    # Request the sitemap
    pages_df = retrieve_pages_from_wikidot_sitemap(base_url_with_protocol, session=session, **kwargs)
    non_content_namespaces = ["system", "poll", "forum", "nav", "search", "admin", "info", "deleted", "random",
                              "more-by", "workbench", "component", "fragment", "theme", "attribution"]

    content_pages_df = pages_df[~pages_df["namespace"].isin(non_content_namespaces)]  # Ignore all non-mainspace pages

    # Request Recent Changes
    window_end = datetime.now(timezone.utc) - timedelta(rc_days_limit)
    rc_df = retrieve_wikidot_recentchanges(base_url_with_protocol, session=session, **kwargs)

    # Actions that do not count as content namespace actions
    noncontent_edit_actions = ["page move/rename", "file action"]

    # If there could be relevant Recent Changes outside the returned window, print a warning instead
    if not rc_df.empty and rc_df["published"].min() > window_end:
        wiki_title = wiki_metadata.get('name')
        warnings.warn(f"Wikidot's Recent Changes RSS can only return the most recent 30 results.\n"
                      f"{wiki_title} has more than 30 edits (to any namespace) in the last {rc_days_limit} days, "
                      f"making it impossible to accurately count the number of recent edits or active users.")
        recent_edit_count = None
        active_user_count = None
    else:
        recent_rc_df = rc_df[rc_df["published"] > window_end]

        recent_edit_count = len(recent_rc_df[~recent_rc_df["action"].isin(noncontent_edit_actions)])
        active_user_count = len([author for author in recent_rc_df["author"].unique()
                                 if author not in ["Anonymous", "(account deleted)"]])

    # Extract data
    wiki_metadata.update({
        # Assume all pages in the sitemap are mainspace pages
        "content_pages": len(content_pages_df),
        # Active users are existing registered users who have performed any action in the past 30 days (including bots)
        "active_users": active_user_count,
        # Number of content edits made in the past 30 days
        "recent_edit_count": recent_edit_count,
        # Timestamp of the most recent content edit
        "latest_edit_timestamp": str(rc_df[~rc_df["action"].isin(noncontent_edit_actions)]["published"].max()),
    })
    return wiki_metadata
