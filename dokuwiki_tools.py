import datetime
import pandas as pd
import requests
import lxml.etree
import lxml.html
import re
import feedparser
import gzip
from io import BytesIO
from urllib.parse import urlparse, urljoin, parse_qsl
from typing import Optional

from utils import extract_base_url, ensure_absolute_url, resolve_wiki_page


def parse_dokuwiki_page_id(page_id: str) -> tuple[Optional[str], Optional[str], str]:
    """
    Separate a DokuWiki page ID into the namespace and base name

    :param page_id: A DokuWiki page ID, including the namespace
    :return: The top-level namespace, the full namespace hierarchy, and the base name of the page
    """
    page_id_parts = page_id.split(':')
    if len(page_id_parts) > 1:
        root_namespace = page_id_parts[0]
        full_namespace = ":".join(page_id_parts[:-1])
        base_title = page_id_parts[-1]
        return root_namespace, full_namespace, base_title
    else:
        return None, None, page_id


def retrieve_dokuwiki_sitemap(entry_url: str, session: Optional[requests.Session] = None,
                              **kwargs) -> Optional[pd.DataFrame]:
    """
    Retrieves a list of all pages on the specified wiki (and their namespaces) from its sitemap.

    :param entry_url: URL path to content-level PHP files (including doku.php)
    :param session: requests Session to use for resolving the URL
    :param kwargs: kwargs to use for the HTTP requests
    :return: DataFrame of all pages on the wiki and their namespaces. If the sitemap cannot be retrieved, returns None.
    """
    # Create a new session if one was not provided
    if session is None:
        session = requests.Session()

    # Request sitemap
    if not entry_url.endswith("/"):
        entry_url += "/"
    php_url = urljoin(entry_url, 'doku.php')
    response = session.get(php_url, params={"do": "sitemap"}, **kwargs)

    # If the wiki does not have a sitemap configured (which is not uncommon), return None
    if not response:
        return None

    # Unpack the sitemap
    sitemap_contenttype = response.headers["Content-Type"]
    if sitemap_contenttype == 'application/x-gzip':
        sitemap_raw = gzip.decompress(response.content)
    else:
        print(f"🗙 Unknown sitemap content-type {sitemap_contenttype}")
        return None

    # Parse sitemap XML
    sitemap_xml = lxml.etree.fromstring(sitemap_raw)
    sitemap_urls = [loc.text for loc in sitemap_xml.xpath("//*[local-name() = 'loc']")]
    sitemap_titles = [parse_dokuwiki_page_id(url.removeprefix(entry_url).replace('/', ':')) for url in sitemap_urls]

    sitemap_df = pd.DataFrame(sitemap_titles, columns=["root_namespace", "full_namespace", "base_page_id"])

    return sitemap_df


def retrieve_dokuwiki_index(entry_url, session: Optional[requests.Session] = None, **kwargs) -> pd.DataFrame:
    """
    Retrieves a list of all pages on the specified wiki (and their namespaces) from its index page.

    :param entry_url: URL path to content-level PHP files (including doku.php)
    :param session: requests Session to use for resolving the URL
    :param kwargs: kwargs to use for the HTTP requests
    :return:
    """
    # Create a new session if one was not provided
    if session is None:
        session = requests.Session()

    def parse_dokuwiki_index_directory(directory_elem):
        # Retrieve all listed elements
        page_list = directory_elem.xpath('.//a[@class="wikilink1"]')
        parsed_entries = [parse_dokuwiki_page_id(list_entry.get("data-wiki-id")) for list_entry in page_list]

        # If the subdirectory elements are not loaded, follow the expansion URL to retrieve them
        subdirectory_list = directory_elem.xpath('./ul/li[./div/a[@class="idx_dir"]]')
        for subdirectory_elem in subdirectory_list:
            if subdirectory_elem.find('./ul') is None:
                # Identify the subdirectory expansion link
                subdirectory_link = subdirectory_elem.find('./div/a[@class="idx_dir"]')
                subdirectory_title = subdirectory_link.get('title')
                subdirectory_url_path = subdirectory_link.get('href')

                # Request the expanded directory
                subdirectory_url = urljoin(entry_url, subdirectory_url_path)
                subdirectory_response = session.get(subdirectory_url, **kwargs)

                # Select just the expanded directory, and parse its contents
                subdirectory_tree = lxml.html.parse(BytesIO(subdirectory_response.content))
                expanded_subdirectory_elem = subdirectory_tree.xpath(f'//li[./div/a[@title="{subdirectory_title}"]]')[0]
                subdirectory_pages = parse_dokuwiki_index_directory(expanded_subdirectory_elem)

                parsed_entries += subdirectory_pages
        return parsed_entries

    # Request index page
    php_url = urljoin(entry_url, "doku.php")
    response = session.get(php_url, params={"do": "index"}, **kwargs)
    if not response:
        response.raise_for_status()
    html_tree = lxml.html.parse(BytesIO(response.content))

    # Parse the directory tree recursively
    root_directory = html_tree.find('//div[@id="index__tree"]')
    df = pd.DataFrame(parse_dokuwiki_index_directory(root_directory),
                      columns=["root_namespace", "full_namespace", "base_page_id"])

    return df


def parse_dokuwiki_diff_url(diff_url: str) -> dict[str, str]:
    """
    Extract key properties from a DokuWiki diff URL.

    :param diff_url: DokuWiki diff URL
    :return: Dict of details extracted from the URL
    """
    parsed_url = urlparse(diff_url)
    parsed_url_params = dict(parse_qsl(parsed_url.query))
    if "image" in parsed_url_params:
        page_details = {
            "page_type": parsed_url_params.get("do"),
            "full_title": parsed_url_params["image"],
            "namespace": parsed_url_params.get("ns"),
            "diff_id": parsed_url_params.get("rev"),
        }
    else:
        page_id = parsed_url_params.get("id", parsed_url.path.removeprefix("/"))
        root_namespace, full_namespace, base_page_id = parse_dokuwiki_page_id(page_id)
        page_details = {
            "page_type": "page",
            "full_title": page_id,
            "namespace": full_namespace,
            "diff_id": parsed_url_params.get("rev"),
        }

    return page_details


def get_action_from_dokuwiki_summary(summary):
    if summary == "removed":
        return "delete"
    if summary == "created":
        return "create"
    else:
        return "edit"


def retrieve_dokuwiki_recentchanges(entry_url: str, request_size: int = 1000,
                                    session: Optional[requests.Session] = None, **kwargs) -> Optional[pd.DataFrame]:
    """
    Retrieve Recent Changes from a DokuWiki wiki within the specified time window.

    Results outside the window will typically be included at the end of the table.
    They are not filtered out in order to allow checking the most recent edit's timestamp, even if it is outside the
    window.

    :param entry_url: URL path to content-level PHP files (including feed.php)
    :param request_size: The number of entries to request
    :param session: requests Session to use for resolving the URL
    :param kwargs: kwargs to use for the HTTP requests
    :return: DataFrame of Recent Changes
    """
    # Create a new session if one was not provided
    if session is None:
        session = requests.Session()

    # Request the Recent Changes feed
    feed_url = urljoin(entry_url, "feed.php")
    rc_params = {"num": request_size, "minor": str(int(True)), "mode": "recent"}
    response = session.get(feed_url, params=rc_params, **kwargs)
    if not response:
        response.raise_for_status()

    # Parse the raw feed contents into a DataFrame
    rc_feed = feedparser.parse(response.content)
    rc_feed_df = pd.DataFrame(rc_feed["entries"])
    if rc_feed_df.empty:
        return None

    # Determine date column
    if "updated" in rc_feed_df.columns:
        date_col = "updated"
    else:
        date_col = "published"

    # Parse feed as a Recent Changes table
    parsed_link_df = pd.DataFrame(rc_feed_df["link"].apply(parse_dokuwiki_diff_url).tolist())
    parsed_title_df = rc_feed_df["title"].str.split(" - ", n=1, expand=True, regex=False).rename(
        columns={0: "base_title", 1: "summary"})
    parsed_title_df["summary"] = parsed_title_df.get("summary")  # Ensure the "summary" column always exists

    parsed_rc_df = pd.concat([parsed_link_df, parsed_title_df], axis=1)
    parsed_rc_df["author"] = rc_feed_df["author_detail"].apply(lambda d: d.get("name"))
    parsed_rc_df["published"] = pd.to_datetime(rc_feed_df[date_col])
    parsed_rc_df["action"] = rc_feed_df["summary"].apply(get_action_from_dokuwiki_summary)

    return parsed_rc_df


def get_dokuwiki_page_id(html_tree: lxml.html.etree) -> str:
    """
    Finds the page ID of a DokuWiki page.
    Finds the JSINFO JSON in a <script> tag's CDATA, then extracts the 'id' value from the JSINFO JSON.

    :param html_tree: Parsed HTML of a DokuWiki page
    :return: Page ID of the DokuWiki page
    """
    jsinfo_tag = html_tree.xpath('//script[contains(., "JSINFO")]')[0]
    jsinfo_match = re.search(r'\"id\":\s*\"(.*?)\"', jsinfo_tag.text)
    page_id = jsinfo_match.group(1)

    return page_id


def get_dokuwiki_content_path_from_url(page_url: Optional[str], page_id: str) -> Optional[str]:
    """
    Given a page URL and page ID, attempt to determine the content path.

    :param page_url: URL of a DokuWiki page
    :param page_id: Page ID of the DokuWiki page
    :return: Content path of the DokuWiki
    """
    if page_url is None:
        return None

    # Normalize case
    page_url = page_url.lower()
    page_id = page_id.lower()

    # Some DokuWikis use / instead of : for namespace delimiters in URLs
    slashed_page_id = page_id.replace(":", "/")

    # If the page URL ends with the page_id, it can be used to determine the content path
    if page_url.endswith(page_id):
        full_content_path = page_url.removesuffix(page_id)
    elif page_url.endswith(slashed_page_id):
        full_content_path = page_url.removesuffix(slashed_page_id)
    else:
        return None

    # Remove the scheme and netloc from the content_path
    base_url = extract_base_url(page_url)
    content_path = full_content_path.removeprefix(base_url)

    return content_path


def get_dokuwiki_content_path(parsed_html: lxml.html.etree, response_url: str) -> Optional[str]:
    """
    Given a page URL and page ID, attempt to determine the content path.

    :param parsed_html: Parsed HTML of a DokuWiki page
    :param response_url: URL used as the final URL of the HTTP response
    :return: Content path of the DokuWiki
    """
    # Determine the page ID
    page_id = get_dokuwiki_page_id(parsed_html)

    # Try the response URL
    content_path = get_dokuwiki_content_path_from_url(response_url, page_id)
    if content_path is not None:
        return content_path

    # Try the og:url URL
    link_elem_matches = parsed_html.xpath('//meta[@name="og:url" or @property="og:url"]')
    for link_elem in link_elem_matches:
        og_url = link_elem.get("content")
        content_path = get_dokuwiki_content_path_from_url(og_url, page_id)
        if content_path is not None:
            return content_path

    # Try the canonical URL
    link_elem = parsed_html.find('//link[@rel="canonical"]')
    if link_elem is not None:
        canonical_url = link_elem.get("href")
        content_path = get_dokuwiki_content_path_from_url(canonical_url, page_id)
        if content_path is not None:
            return content_path

    # Otherwise, content_path cannot be determined
    return None


def profile_dokuwiki_wiki(wiki_page: str | requests.Response, full_profile: bool = True, rc_days_limit: int = 30,
                          session: Optional[requests.Session] = None, **kwargs) -> dict:
    """
    Given a URL or HTTP request response for a page of a DokuWiki wiki, retrieves key information about the wiki,
    including content and activity metrics.

    :param wiki_page: DokuWiki wiki page URL or HTTP request response
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

    # Get the manifest
    manifest_url_path = input_page_html.find('//link[@rel="manifest"]').get("href")
    manifest_url = urljoin(base_url_with_protocol, manifest_url_path)
    manifest_response = session.get(manifest_url, **kwargs)
    if not manifest_response:
        manifest_response.raise_for_status()
    manifest = manifest_response.json()

    # Request the Main Page
    entry_url = urljoin(base_url_with_protocol, manifest.get("start_url"))

    main_page_response = session.get(entry_url, **kwargs)
    if not main_page_response:
        main_page_response.raise_for_status()
    main_page_html = lxml.html.parse(BytesIO(main_page_response.content))
    main_page_id = get_dokuwiki_page_id(main_page_html)

    # Try to find the content_path from the Main Page (to avoid any potential noise on the input URL)
    content_path = get_dokuwiki_content_path(main_page_html, main_page_response.url)
    # If that fails, fallback to the input URL
    if content_path is None:
        content_path = get_dokuwiki_content_path(input_page_html, input_page_response.url)

    # Other properties
    language_full = input_page_html.getroot().get("lang")
    language_base = language_full.split("-")[0]

    licence_elem = input_page_html.find('//div[@class="license"]/*/a[@rel="license"]')
    icon_url = input_page_html.find('//link[@rel="shortcut icon"]').get("href")

    wiki_metadata = {
        # Basic information
        "name": manifest.get("name"),
        "base_url": urlparse(input_page_response.url).hostname,
        "full_language": language_full,
        "language": language_base,

        # Technical data
        "wiki_id": None,           # DokuWiki does not appear to have an equivalent to wiki_id
        "wikifarm": None,          # Unaware of any examples of DokuWiki sites hosted on wikifarms
        "platform": "DokuWiki".lower(),
        "software_version": None,  # Software version does not appear to be publicly available

        # Paths
        "protocol": urlparse(input_page_response.url).scheme,
        "main_page": main_page_id,
        "content_path": content_path,
        "search_path": urljoin(entry_url, "doku.php"),
        "icon_path": ensure_absolute_url(icon_url, base_url_with_protocol),

        # Licensing
        "licence_name": licence_elem.text if licence_elem is not None else None,
        "licence_page": licence_elem.get("href") if licence_elem is not None else None,
    }

    if not full_profile:
        return wiki_metadata

    # Namespaces commonly used as non-content namespaces
    # (Non-content namespaces are purely a semantic distinction; DokuWiki does not make a technical distinction)
    non_content_namespaces = ["user", "editors", "test", "sandbox", "playground", "wiki"]

    # Request page list
    # The sitemap is strongly preferred as the source, as it only requires a single request;
    # the index requires a separate request for every namespace and subnamespace on the wiki,
    # so is substantially slower, especially for wikis with lots of namespaces.
    # However, many DokuWikis do not configure a sitemap, so the index fallback is still required.
    page_df = retrieve_dokuwiki_sitemap(entry_url, session=session, **kwargs)
    if page_df is None:
        page_df = retrieve_dokuwiki_index(entry_url, session=session, **kwargs)

    # Count content pages
    content_pages = len(page_df[~page_df["root_namespace"].isin(non_content_namespaces)])
    wiki_metadata.update({"content_pages": content_pages})

    # Request Recent Changes
    window_end = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(rc_days_limit)
    rc_df = retrieve_dokuwiki_recentchanges(entry_url, session=session, **kwargs)
    if rc_df is None:
        # If the Recent Changes request failed, fill the corresponding values with nulls
        wiki_metadata.update({"active_users": None, "recent_edit_count": None, "latest_edit_timestamp": None})
        return wiki_metadata

    # Content edits exclude deletions and edits to Media pages
    content_edits_df = rc_df[(rc_df["action"] != "delete") & (rc_df["page_type"] == "page") &
                             (~rc_df["namespace"].isin(non_content_namespaces))]

    active_users = rc_df["author"][(rc_df["published"] > window_end) & (rc_df["author"] != "Anonymous")].unique()

    # Extract data
    wiki_metadata.update({
        # Active users are registered users who have performed any action in the past 30 days
        "active_users": len(active_users),
        # Number of content edits made in the past 30 days
        "recent_edit_count": len(content_edits_df[content_edits_df["published"] > window_end]),
        # Timestamp of the most recent content edit
        "latest_edit_timestamp": str(content_edits_df["published"].max()),
    })

    return wiki_metadata
