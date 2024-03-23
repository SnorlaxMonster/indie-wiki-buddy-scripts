import json
import lxml.html
import pandas as pd
import requests
import warnings
from io import BytesIO
from urllib.parse import urlparse
from typing import Optional

from utils import resolve_wiki_page, extract_base_url, ensure_absolute_url


def retrieve_pages_from_minmax(base_url: str, session: Optional[requests.Session] = None,
                               **kwargs) -> Optional[pd.DataFrame]:
    """
    Retrieves a list of all pages on the specified MinMax wiki from its "Pages" page.

    :param base_url: MinMax wiki base URL
    :param session: requests Session to use for resolving the URL
    :param kwargs: kwargs to use for the HTTP requests
    :return: DataFrame of all pages on the wiki and their namespaces. If the sitemap cannot be retrieved, returns None.
    """
    # Create a new session if one was not provided
    if session is None:
        session = requests.Session()

    # Retrieve the "Pages" page
    all_pages_url = base_url + "/pages"
    response = session.get(all_pages_url, **kwargs)

    # Select the list container
    html_tree = lxml.html.parse(BytesIO(response.content))
    xpath_result = html_tree.xpath("//div[@id='app-container']//div[contains(@class, 'chakra-container')]//div[div/li]")
    list_container = xpath_result[0]

    # Get all link elements in the list
    page_entries = list_container.findall("./div/li/a")

    # Extract page information from the link elements
    all_pages_list = [(list_item.get("href"), list_item.find(".//h4").text) for list_item in page_entries]
    pages_df = pd.DataFrame(all_pages_list)

    return pages_df


def profile_minmax_wiki(wiki_page: str | requests.Response, full_profile: bool = True,
                        session: Optional[requests.Session] = None,
                        **kwargs) -> dict:
    """
    Given a URL or HTTP request response for a page of a MinMax wiki, retrieves key information about the wiki,
    including page count.

    :param wiki_page: MinMax wiki page URL or HTTP request response
    :param full_profile: Whether to include activity and content metrics
    :param session: requests Session to use for resolving the URL
    :param kwargs: kwargs to use for the HTTP requests
    :return: JSON-serializable dict of wiki metadata in standardized format
    """
    # Create a new session if one was not provided
    if session is None:
        session = requests.Session()

    # If provided a URL, run an HTTP request
    response = resolve_wiki_page(wiki_page, session=session, **kwargs)

    # Parse the URL
    parsed_url = urlparse(response.url)
    base_url = str(parsed_url.hostname) + "/" + str(parsed_url.path).split("/")[1]
    protocol = parsed_url.scheme

    # Parse the HTML
    html_tree = lxml.html.parse(BytesIO(response.content))

    licence_element = html_tree.xpath("//footer/div/div[not(@id='top')]/p[contains(@class, 'chakra-text')]/a")[0]
    icon_path = html_tree.find("//link[@rel='icon']").get("href")

    # Parse the JSON data
    page_data = json.loads(html_tree.find("//script[@id='__NEXT_DATA__']").text)

    # Get the wiki element of the JSON data
    page_props = page_data["props"]["pageProps"]
    wiki_json_data = page_props.get("wiki")
    if wiki_json_data is None:
        page_props_page = page_props.get("page")
        if page_props_page is not None:
            wiki_json_data = page_props_page["wiki"]

    # Build wiki_metadata result
    wiki_metadata = {
        # Basic information
        "name": wiki_json_data.get("name"),
        "base_url": base_url,
        "full_language": html_tree.getroot().get("lang"),
        "language": html_tree.getroot().get("lang"),

        # Technical data
        "wiki_id": wiki_json_data.get("id"),
        "wikifarm": "MinMax".lower(),
        "platform": "MinMax".lower(),
        "software_version": None,  # page_data["buildId"] is close, but not that meaningful

        # Paths
        "protocol": protocol,
        "main_page": "/",  # MinMax wiki Main Pages are located at the root URL
        "content_path": "/",
        "search_path": None,  # Search results pop out from the search bar, and do not have their own page
        "icon_path": ensure_absolute_url(icon_path, extract_base_url(response.url)),

        # Licensing
        "licence_name": licence_element.text,
        "licence_page": licence_element.get("href"),
    }

    if not full_profile:
        return wiki_metadata

    warnings.warn("MinMax does not make editor names publicly available, \n"
                  "so there is no way to determine the number of active users. \n"
                  "MinMax also does not have a Recent Changes, so there is no way to check recent edits, \n"
                  "other than simply counting the number of revisions on every single page.")

    # Retrieve list of pages
    base_url_with_protocol = protocol + "://" + base_url
    all_pages_df = retrieve_pages_from_minmax(base_url_with_protocol, session=session, **kwargs)

    # Extract data
    wiki_metadata.update({
        # Count all pages listed on the "Pages" page
        "content_pages": len(all_pages_df),

        # Editors are not visible on MinMax wikis
        "active_users": None,

        # There is no Recent Changes on MinMax wikis
        "recent_edit_count": None,
        "latest_edit_timestamp": None,
    })
    return wiki_metadata
