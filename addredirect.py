"""
Python script for adding a new wiki redirect
"""
import re
import json
import os
from urllib.parse import urlparse, urlunparse, urljoin, ParseResult as UrlParseResult
import requests
import lxml.html
import unicodedata
from io import BytesIO
from typing import Optional
from PIL import Image


class LanguageMismatchError(Exception):
    def __init__(self, origin_lang, destination_lang):
        self.message = f"{origin_lang} and {destination_lang} are different languages"


class DuplicateIDError(Exception):
    def __init__(self, entry_id):
        self.message = f"id {entry_id} is already used"


def extract_hostname(url: str) -> str:
    """
    Extract the hostname (full domain name) of the specified URL.

    :param url: URL
    :return: Domain name
    """
    parsed_url = urlparse(url)
    return parsed_url.hostname


def normalize_url(url: str, default_protocol="https") -> str:
    """
    Enforces that the URL specifies a protocol.

    :param url: Unnormalized URL
    :param default_protocol: Protocol to use to access the URL, if one is not specified
    :return: URL with a scheme, or // prefix to indicate the lack of one
    """
    if url.startswith(("http://", "https://")):
        return url
    elif url.startswith("//"):
        return f"{default_protocol}:{url}"
    else:
        return f"{default_protocol}://{url}"


def normalize_wikia_url(original_url: str) -> str:
    """
    Old Wikia URLs included the language as a subdomain for non-English wikis, but these URLs no longer work.
    Non-English Wikia URLs need to be modified to move the language to the path of the URL.

    :param original_url: Wikia URL
    :return: Modified URL with language moved to the path, if necessary
    """
    # Ignore non-Wikia URLs
    if "wikia.com" not in original_url:
        return original_url

    # Parse the URL
    parsed_url = urlparse(normalize_url(original_url))

    # Extract the URL components
    lang_match = re.match(r"([a-z]+)\.(.*)\.wikia\.com", parsed_url.hostname)
    if not lang_match:
        return original_url
    lang, subdomain = lang_match.groups()

    # Construct the new URL
    new_domain = f"{subdomain}.fandom.com"  # Always use "fandom.com" when restructuring Wikia URLs
    new_path = urljoin(lang, parsed_url.path)
    parsed_url = parsed_url._replace(netloc=new_domain, path=new_path)

    return urlunparse(parsed_url)


def normalize_api_url(parsed_api_url: UrlParseResult, parsed_response_url: UrlParseResult) -> str:
    """
    Ensures that the API URL includes the scheme and netloc, and does not include a query.
    For example, if the API URL is retrieved as "/w/api.php?action=rsd", adds the scheme and domain name to the URL,
    and deletes action=rsd.

    :param parsed_api_url: Pre-parsed API URL
    :param parsed_response_url: Pre-parsed site URL
    :return: Normalized API URL
    """
    if parsed_api_url.netloc == "":
        parsed_api_url = parsed_api_url._replace(netloc=parsed_response_url.netloc)
    if parsed_api_url.scheme == "":
        parsed_api_url = parsed_api_url._replace(scheme=parsed_response_url.scheme)
    if parsed_api_url.query != "":
        parsed_api_url = parsed_api_url._replace(query="")

    return urlunparse(parsed_api_url)


def request_with_error_handling(raw_url: str, ignorable_errors: Optional[list[int]] = None,
                                **kwargs) -> Optional[requests.Response]:
    """
    Given a base URL, attempts to resolve the URL as HTTPS, then falls back to HTTP if that isn't possible.

    :param raw_url: URL to resolve
    :param ignorable_errors: Error codes that should be ignored as long as the response is not null
    :param kwargs: kwargs to use for the HTTP/HTTPS requests
    :return: GET request response
    """
    ignorable_errors = [] if (ignorable_errors is None) else ignorable_errors
    url = normalize_url(raw_url)

    # GET request the URL
    try:
        response = requests.get(url, **kwargs)

    # If using HTTPS results in an SSLError, try HTTP instead
    except requests.exceptions.SSLError:
        parsed_url = urlparse(url)
        if parsed_url.scheme != "http":
            print(f"⚠ SSLError. Defaulting to HTTP connection for {raw_url}")
            url = urlunparse(parsed_url._replace(scheme="http"))
            return request_with_error_handling(url, ignorable_errors=ignorable_errors, **kwargs)
        else:
            print(f"⚠ SSLError: Unable to connect to {raw_url} , even via HTTP")
            return None

    # If unable to connect to the URL at all, return None
    except requests.exceptions.ConnectionError:
        print(f"⚠ ConnectionError: Unable to connect to {url}")
        return None

    # If the response is an error, handle the error
    if not response:
        # If the error is in the set of acceptable errors and the response included any content, ignore the error
        if response.status_code in ignorable_errors and response.content:
            pass

        # For Error 404 and 410, the wiki presumably does not exist, so abort
        # This could mean that only the Main Page does not exist (while the wiki does), but that case is ignored
        if response.status_code in (404, 410):
            print(f"Error {response.status_code} returned by {url} . Page does not exist. Aborting.")
            return None

        # For other errors, they can usually be fixed by adding a user-agent
        else:
            print(f"Error {response.status_code} returned by {url} . Aborting.")
            return None

    # Otherwise, return the response
    return response


def is_mediawiki(parsed_html: lxml.html.etree) -> bool:
    """
    Checks if the page is a MediaWiki page.
    MediaWiki pages can be identified by containing "mediawiki" as a class on the <body> element.

    :param parsed_html: LXML etree representation of the page's HTML
    :return: Whether the page is a page from a MediaWiki site
    """
    body_elem = parsed_html.find('body')
    if body_elem is None:
        return False

    body_class = body_elem.get('class')
    if body_class is None:
        return False

    if 'mediawiki' in body_class.split():
        return True
    else:
        return False


def get_api_url(response: requests.Response) -> Optional[str]:
    """
    Given an HTTP/HTTPS response for a wiki page, determines the wiki's API URL.

    :param response: HTTP/HTTPS response for a wiki page
    :return: Wiki's API URL
    """
    # Parse the HTML
    parsed_html = lxml.html.parse(BytesIO(response.content))

    # If the site is not a MediaWiki wiki, abort trying to determine the API URL
    if not is_mediawiki(parsed_html):
        print(f"{response.url} is not a MediaWiki page")
        return None

    # Retrieve the API URL via EditURI element
    edit_uri_node = parsed_html.find('/head/link[@rel="EditURI"]')
    if edit_uri_node is not None:
        parsed_api_url = urlparse(edit_uri_node.get('href'))
        parsed_response_url = urlparse(response.url)

        return normalize_api_url(parsed_api_url, parsed_response_url)

    # If EditURI is missing, try to find the permalink URL and determine the API URL from that
    permalink_node = parsed_html.find('//li[@id="t-permalink"]/a')
    if permalink_node is not None:
        print(f"WARNING: Retrieved API URL for {response.url} via permalink node")
        permalink_url = permalink_node.get("href")
        parsed_permalink_url = urlparse(permalink_url)
        parsed_api_url = parsed_permalink_url._replace(path=parsed_permalink_url.path.replace('index.php', 'api.php'))
        parsed_response_url = urlparse(response.url)

        return normalize_api_url(parsed_api_url, parsed_response_url)

    # Otherwise, the API URL retrieval has failed
    print(f"Unable to determine API URL for {response.url}")
    return None


def get_api_url_via_request(wiki_url: str, headers: Optional[dict] = None) -> Optional[str]:
    """
    Given a wiki's URL, determines what its API URL is.

    :param wiki_url: Wiki's URL
    :param headers: Headers to include in the request (e.g. user-agent)
    :return: Wiki's API URL
    """
    response = request_with_error_handling(wiki_url, headers=headers)
    if response is None:
        return None

    return get_api_url(response)


def query_wiki_api(api_url: str, query_params: dict, headers: Optional[dict] = None) -> Optional[dict]:
    """
    Runs a MediaWiki API query with the specified parameters.

    :param api_url:
    :param query_params: Query parameters to use
    :param headers: Headers to include in the request (e.g. user-agent)
    :return: API query result
    """
    # GET request API query
    response = request_with_error_handling(api_url, params=query_params, headers=headers)

    # If the response is an error, do not attempt to parse it as JSON
    if not response:
        print(f"Error {response.status_code} from {api_url}")
        return None

    # Parse as JSON
    query_json = response.json()

    # If the response contains an error, do not attempt the retrieve the content
    if query_json.get("error"):
        print(f"Error {query_json['error'].get('code')} from {api_url}")
        return None

    return query_json["query"]


def request_siteinfo(api_url: str, headers: Optional[dict] = None) -> Optional[dict]:
    """
    Runs a "siteinfo" query using the MediaWiki API of the specified wiki, for 'general' data.

    :param api_url: MediaWiki API URL
    :param headers: Headers to include in the request (e.g. user-agent).
    :return: siteinfo query result
    """
    siteinfo_params = {'action': 'query', 'meta': 'siteinfo', 'siprop': 'general', 'format': 'json'}
    siteinfo = query_wiki_api(api_url, siteinfo_params, headers=headers)

    return siteinfo


def extract_site_metadata_from_siteinfo(siteinfo: dict) -> dict:
    """
    Extracts the important data from a siteinfo result, and transforms it into a standardized format

    :param siteinfo: MediaWiki API response for a "siteinfo" query including siprop=general
    :return: Standardized site properties
    """
    siteinfo_general = siteinfo["general"]

    base_url = extract_hostname(siteinfo_general["base"])
    wiki_name = siteinfo_general["sitename"]
    language = siteinfo_general["lang"]  # NOTE: The language retrieved this way will include the dialect
    main_page = siteinfo_general["mainpage"].replace(" ", "_")
    content_path = siteinfo_general["articlepath"].replace("$1", "")
    search_path = siteinfo_general["script"]
    icon_path = siteinfo_general.get("favicon")  # Not guaranteed to be present
    logo_path = siteinfo_general.get("logo")  # Not guaranteed to be present

    # Apply standard wiki name changes
    if ".fandom.com" in base_url:
        wiki_name = wiki_name.replace(" Wiki", " Fandom Wiki")

    # For Fandom wikis, ensure the language path is part of the base_url instead of the content_path
    if ".fandom.com" in base_url and language != "en":
        full_path_parts = (base_url + content_path).split("/")
        if full_path_parts[1] == language:
            base_url = "/".join(full_path_parts[0:2])
            content_path = "/" + "/".join(full_path_parts[2:])

    # Return extracted properties
    site_properties = {
        "name": wiki_name,
        "base url": base_url,
        "language": language,
        "main page": main_page,
        "content path": content_path,
        "search path": search_path,
        "icon path": icon_path,
        "logo path": logo_path,
        "platform": "mediawiki",  # This function requires a MediaWiki API result as input
    }
    return site_properties


def extract_topic_from_url(wiki_url: str) -> str:
    # Retrieve the topic
    normalized_url = normalize_url(wiki_url)
    hostname = extract_hostname(normalized_url)
    domain_parts = hostname.split('.')
    topic = domain_parts[0].replace("-", "")

    return topic


def generate_entry_id(language: str, origin_url: str) -> str:
    """
    Auto-generate an entry ID, based on the language and subdomain of the origin wiki.

    :param language: Normalized language code
    :param origin_url: URL of the origin wiki
    :return: entry ID string
    """
    # Retrieve the topic
    topic = extract_topic_from_url(origin_url)

    entry_id = language + "-" + topic
    return entry_id


def get_wiki_language(origin_lang_code: str, destination_lang_code: str) -> str:
    """
    Given a pair of language codes, returns the base language of the pair if they are the same (stripping dialect info).
    If the pair are different languages, raise LanguageMismatchError.
    If the pair are different dialects of the same language, print a warning, but still return the base language.

    :param origin_lang_code: Origin wiki's language code
    :param destination_lang_code: Destination wiki's language code
    :return: Base language code
    """
    origin_lang_parts = origin_lang_code.split('-')
    destination_lang_parts = destination_lang_code.split('-')

    origin_lang_base = origin_lang_parts[0]
    destination_lang_base = destination_lang_parts[0]

    if origin_lang_base != destination_lang_base:
        raise LanguageMismatchError(origin_lang_code, destination_lang_code)

    # If the languages are the same but dialects differ, print a warning
    if origin_lang_code != destination_lang_code:
        print(f"WARNING: Cross-dialect redirection from {origin_lang_code} to {destination_lang_code}")

    return origin_lang_base


def generate_redirect_entry(origin_site_metadata: dict, destination_site_metadata: dict,
                            entry_id: Optional[str] = None) -> dict:
    """
    Using the pre-processed metadata of the origin and destination wikis, generates an IWB redirection entry that can
    be added to the sites JSON file.
    The icon file is not downloaded at this time, so "destination_icon" is set to null.

    :param origin_site_metadata: Origin's pre-processed metadata
    :param destination_site_metadata: Destination's pre-processed metadata
    :param entry_id: ID to use for the new entry. Determined automatically if not specified.
    :return: IWB redirection entry for a sites JSON file
    """
    # Generate an entry id if it was not provided
    if entry_id is None:
        language = get_wiki_language(origin_site_metadata["language"], destination_site_metadata["language"])
        entry_id = generate_entry_id(language, origin_site_metadata["base url"])

    # Generate origin entry
    origin_entry = {
        "origin": origin_site_metadata["name"],
        "origin_base_url": origin_site_metadata["base url"],
        "origin_content_path": origin_site_metadata["content path"],
        "origin_main_page": origin_site_metadata["main page"],
    }

    # Generate redirect entry
    entry = {
        "id": entry_id,
        "origins_label": origin_site_metadata["name"],
        "origins": [origin_entry],
        "destination": destination_site_metadata["name"],
        "destination_base_url": destination_site_metadata["base url"],
        "destination_platform": "mediawiki",
        "destination_icon": None,  # Filename cannot be determined at this time. Populate it when the icon is added.
        "destination_main_page": destination_site_metadata["main page"],
        "destination_search_path": destination_site_metadata["search path"],
    }

    # Generate tags
    tags = []

    # If the site URL or logo URL contains the name of a wikifarm, assume the wiki is hosted on that wikifarm
    # Checking the logo URL should catch any wikis hosted on a wikifarm that use a custom URL
    destination_url = destination_site_metadata["base url"]
    destination_logo_url = destination_site_metadata.get("logo path", "")
    for wikifarm in ("shoutwiki", "wiki.gg", "miraheze", "wikitide"):
        if wikifarm in destination_url or wikifarm in destination_logo_url:
            tags.append(wikifarm)
            break

    if len(tags) > 0:
        entry["tags"] = tags

    return entry


def generate_icon_filename(wiki_name: str) -> str:
    """
    Auto-generates a filename for the icon file, by stripping all non-alphabetic characters from the sitename.
    File will be saved as .png

    :param wiki_name: Name of the wiki
    :return: Filename of the icon file
    """
    # Unicode decomposition ensures characters have their diacritics separated so that they can be removed (e.g. é → e)
    normalized_wiki_name = unicodedata.normalize("NFKD", wiki_name).lower()

    # Remove .gg from wiki.gg wikis
    normalized_wiki_name = normalized_wiki_name.replace('wiki.gg', 'wiki')

    # Remove characters other than ASCII alphanumerics
    filename_stem = re.sub('[^A-Za-z0-9]+', '', normalized_wiki_name)
    assert filename_stem != ""

    return filename_stem + ".png"


def download_icon(icon_url: str, icon_filename: str, language: str, headers: Optional[dict] = None,
                  iwb_filepath: str = ".") -> Image:
    # Download favicon
    try:
        icon_file_response = requests.get(normalize_url(icon_url), headers=headers)
    except requests.exceptions.ConnectionError:
        icon_file_response = None

    if not icon_file_response:
        print(f"Failed to download icon from {icon_url}")
        return None

    # Determine filepath
    icon_folderpath = os.path.join(iwb_filepath, "favicons", language)
    if not os.path.isdir(icon_folderpath):  # If the folder doesn't already exist, create it
        os.mkdir(icon_folderpath)
    icon_filepath = os.path.join(icon_folderpath, icon_filename)

    # Write to file
    image_file = Image.open(BytesIO(icon_file_response.content))
    image_file = image_file.resize((16, 16))
    image_file.save(icon_filepath)  # PIL ensures that conversion from ICO to PNG is safe

    # Return the file
    assert image_file is not None
    return image_file


def add_redirect_entry(new_entry: dict, language_code: str, icon_url: Optional[str] = None,
                       headers: Optional[dict] = None, iwb_filepath: str = "."):
    """
    Adds a provided IWB redirection entry to the appropriate sites JSON.
    Also downloads the destination wiki's favicon to the appropriate folder, if necessary.

    :param new_entry: Entry to insert
    :param language_code: Language of the new entry (as 2-letter language code)
    :param entry_id: ID to use for the new entry (including language). Determined automatically if not specified.
    :param icon_url: URL for the wiki's icon.
    :param headers: Headers to use for the favicon download (e.g. user-agent).
    :param iwb_filepath: Filepath to IWB code, if it differs from the directory the script is being run from.
    :return: ID of the newly added entry, or None if it was not added
    """

    # Open relevant sites JSON file
    assert os.path.isdir(os.path.join(iwb_filepath, "data"))
    sites_json_filepath = os.path.join(iwb_filepath, "data", f"sites{language_code.upper()}.json")
    try:
        with open(sites_json_filepath, "r", encoding='utf-8') as sites_json_file:
            sites_json = json.load(sites_json_file)
    except OSError:  # If there is not currently a sites JSON for this language, start from an empty list
        sites_json = []

    # Check whether either wiki is already present
    new_origin_entry = new_entry["origins"][0]
    target_entry = None
    for json_entry in sites_json:
        # Check that the origin is distinct
        for json_origin_entry in json_entry["origins"]:
            if json_origin_entry["origin_base_url"] == new_origin_entry["origin_base_url"]:
                print(f"⚠ Redirect addition failed: {json_origin_entry['origin_base_url']} already redirects to "
                      f"{json_entry['destination_base_url']}")
                return None  # If the origin is already present, do not attempt to add it again

        # Check if the destination is distinct
        if new_entry["destination_base_url"] == json_entry["destination_base_url"]:
            target_entry = json_entry
            break

        # If the destinations are distinct, ensure that the new ID is unique
        elif new_entry["id"] == json_entry["id"]:
            raise DuplicateIDError(new_entry["id"])

    # If the destination is already present, add the origin as a new entry to the existing entry
    if target_entry is not None:
        destination_is_new = False
        target_entry["origins"].append(new_origin_entry)

    # If the destination is not already present, add a new entry for it
    else:
        destination_is_new = True

        # Download the wiki's icon
        if icon_url is not None:
            icon_filename = generate_icon_filename(new_entry["destination"])
            image_file = download_icon(icon_url, icon_filename, language_code,
                                       iwb_filepath=iwb_filepath, headers=headers)
            if image_file is not None:  # If the download was successful
                new_entry["destination_icon"] = icon_filename
            else:
                print(f"⚠ Unable to download icon for {new_entry['destination']}")
        else:
            print(f"⚠ No icon URL provided for {new_entry['destination']}")

        # Add the entry and sort the list
        sites_json.append(new_entry)
        target_entry = new_entry
        sites_json.sort(key=lambda entry: entry["id"])

    print(f"ℹ️️ Added redirect from {new_origin_entry['origin']} ({new_origin_entry['origin_base_url']}) to "
          f"{'existing entry for ' if not destination_is_new else ''}"
          f"{target_entry['destination']} ({target_entry['destination_base_url']})"
          f" at ID {target_entry['id']}.")

    # Save the updated sites JSON file
    with open(sites_json_filepath, "w", encoding="utf-8") as sites_json_file:
        json.dump(sites_json, sites_json_file, indent=2, ensure_ascii=False)

    return new_entry["id"]


def add_redirect_entry_via_request(origin_wiki_url: str, destination_wiki_url: str, entry_id: Optional[str] = None,
                                   headers: Optional[dict] = None, iwb_filepath: str = ".") -> Optional[str]:
    """
    Given the URLs for the origin and destination wikis, determines the MediaWiki API URL, then uses the API for each
    wiki to generate the IWB entry and adds it to the sites JSON and downloads the favicon to the appropriate folder.
    Also downloads the destination wiki's favicon to the appropriate folder, if necessary.

    Both wikis must be MediaWiki wikis for this to be successful.

    :param origin_wiki_url: URL of the origin wiki
    :param destination_wiki_url: URL of the destination wiki
    :param entry_id: ID to use for the new entry. Determined automatically if not specified.
    :param headers: Headers to use for the favicon download (e.g. user-agent).
    :param iwb_filepath: Filepath to IWB code, if it differs from the directory the script is being run from.
    :return: ID of the newly added entry, or None if it was not added
    """
    # Check the IWB filepath is correct
    if not os.path.isdir(os.path.join(iwb_filepath, "data")):
        raise Exception('⚠ Cannot find the "data" folder. Ensure that you specified the correct "iwb_filepath".')

    # Normalize defunct Wikia URLs
    origin_wiki_url = normalize_wikia_url(origin_wiki_url)

    # Determine the API URLs
    origin_api_url = get_api_url_via_request(origin_wiki_url, headers=headers)
    destination_api_url = get_api_url_via_request(destination_wiki_url, headers=headers)

    # Validate the API URLs
    if origin_api_url is None or destination_api_url is None:
        print(f"Unable to add redirection from {origin_wiki_url} to {destination_wiki_url}.")
        return None

    # Request siteinfo data
    origin_siteinfo = request_siteinfo(origin_api_url, headers=headers)
    destination_siteinfo = request_siteinfo(destination_api_url, headers=headers)

    # Validate the siteinfo data
    if origin_siteinfo is None or destination_siteinfo is None:
        print(f"Unable to add redirection from {origin_wiki_url} to {destination_wiki_url}.")
        return None

    # Extract relevant metadata from the siteinfo data
    origin_site_metadata = extract_site_metadata_from_siteinfo(origin_siteinfo)
    destination_site_metadata = extract_site_metadata_from_siteinfo(destination_siteinfo)

    # Get properties from site metadata
    language = get_wiki_language(origin_site_metadata["language"], destination_site_metadata["language"])
    icon_url = destination_site_metadata["icon path"]

    # Generate entry
    new_entry = generate_redirect_entry(origin_site_metadata, destination_site_metadata, entry_id)

    # Add the entry
    entry_id = add_redirect_entry(new_entry, language, icon_url=icon_url, headers=headers, iwb_filepath=iwb_filepath)
    return entry_id
