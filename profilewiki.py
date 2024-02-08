import json
import lxml.html
from io import BytesIO
from requests.exceptions import HTTPError, SSLError
from typing import Optional
from urllib.parse import urlparse, urlunparse

from scrapewiki import normalize_url_protocol, request_with_http_fallback, extract_xpath_property, WikiSoftware
from mediawiki_tools import get_mediawiki_api_url, profile_mediawiki_wiki, normalize_wikia_url, MediaWikiAPIError
from fextralife_tools import profile_fextralife_wiki


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
    wiki_url = normalize_url_protocol(wiki_url)
    response = request_with_http_fallback(wiki_url, headers=headers)
    if not response:
        response.raise_for_status()
    parsed_html = lxml.html.parse(BytesIO(response.content))

    # Detect wiki software
    wiki_software = determine_wiki_software(parsed_html)

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
    wiki_url = normalize_wikia_url(normalize_url_protocol(wiki_url))

    # Detect wiki software
    print(f"🕑 Resolving {wiki_url}")
    try:
        response = request_with_http_fallback(wiki_url, headers=headers)
    except (HTTPError, ConnectionError, SSLError) as e:
        print(e)
        return

    html_tree = lxml.html.parse(BytesIO(response.content))
    wiki_software = determine_wiki_software(html_tree)

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
