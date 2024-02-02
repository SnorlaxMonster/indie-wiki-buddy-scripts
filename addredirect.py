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

NO_VALUES = {"", "n", "no"}
YES_VALUES = {"y", "yes"}

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
    :return: URL with a protocol
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


def normalize_relative_url(parsed_relative_url: UrlParseResult, parsed_absolute_url: UrlParseResult) -> str:
    """
    Ensures that a URL includes the protocol and domain name, and does not include a query.
    For example, if the input URL is "/w/api.php?action=rsd", adds the protocol and domain name to the URL,
    and deletes action=rsd.

    :param parsed_relative_url: Pre-parsed URL to be normalized
    :param parsed_absolute_url: Pre-parsed URL to use to fill in gaps in the first URL
    :return: Normalized API URL
    """
    parsed_new_url = parsed_relative_url
    if parsed_new_url.netloc == "":
        parsed_new_url = parsed_new_url._replace(netloc=parsed_absolute_url.netloc)
    if parsed_new_url.scheme == "":
        parsed_new_url = parsed_new_url._replace(scheme=parsed_absolute_url.scheme)
    if parsed_new_url.query != "":
        parsed_new_url = parsed_new_url._replace(query="")

    return urlunparse(parsed_new_url)


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
            print(f"⚠ Error {response.status_code} returned by {url} . Page does not exist. Aborting.")
            return None

        # For other errors, they can usually be fixed by adding a user-agent
        else:
            print(f"⚠ Error {response.status_code} returned by {url} . Aborting.")
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


def determine_wiki_software(response: Optional[requests.Response]) -> Optional[str]:
    if not response:
        return None

    # Parse the HTML
    parsed_html = lxml.html.parse(BytesIO(response.content))

    # Check the wiki's software
    if is_mediawiki(parsed_html):
        return "mediawiki"
    else:
        return None  # unable to determine the wiki's software


def get_favicon_url(response: Optional[requests.Response]) -> Optional[str]:
    if not response:
        return None

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

    return normalize_relative_url(urlparse(icon_url), urlparse(response.url))


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
        print(f"⚠ {response.url} is not a MediaWiki page")
        return None

    # Retrieve the API URL via EditURI element
    edit_uri_node = parsed_html.find('/head/link[@rel="EditURI"]')
    if edit_uri_node is not None:
        parsed_api_url = urlparse(edit_uri_node.get('href'))
        parsed_response_url = urlparse(response.url)

        return normalize_relative_url(parsed_api_url, parsed_response_url)

    # If EditURI is missing, try to find the permalink URL and determine the API URL from that
    permalink_node = parsed_html.find('//li[@id="t-permalink"]/a')
    if permalink_node is not None:
        print(f"ℹ Retrieved API URL for {response.url} via permalink node")
        permalink_url = permalink_node.get("href")
        parsed_permalink_url = urlparse(permalink_url)
        parsed_api_url = parsed_permalink_url._replace(path=parsed_permalink_url.path.replace('index.php', 'api.php'))
        parsed_response_url = urlparse(response.url)

        return normalize_relative_url(parsed_api_url, parsed_response_url)

    # Otherwise, the API URL retrieval has failed
    print(f"⚠ Unable to determine API URL for {response.url}")
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
    full_language = siteinfo_general["lang"]  # NOTE: The language retrieved this way will include the dialect
    normalized_language = full_language.split('-')[0]
    main_page = siteinfo_general["mainpage"].replace(" ", "_")
    content_path = siteinfo_general["articlepath"].replace("$1", "")
    search_path = siteinfo_general["script"]
    icon_path = siteinfo_general.get("favicon")  # Not guaranteed to be present

    # Detect if the wiki is on a wikifarm
    logo_path = siteinfo_general.get("logo", "")  # Not guaranteed to be present
    wikifarm = get_wikifarm(base_url, logo_path)

    # Apply standard wiki name changes
    if ".fandom.com" in base_url:
        wiki_name = wiki_name.replace(" Wiki", " Fandom Wiki")

    # For Fandom wikis, ensure the language path is part of the base_url instead of the content_path
    if ".fandom.com" in base_url and normalized_language != "en":
        full_path_parts = (base_url + content_path).split("/")
        if full_path_parts[1] == normalized_language:
            base_url = "/".join(full_path_parts[0:2])
            content_path = "/" + "/".join(full_path_parts[2:])

    # Return extracted properties
    site_properties = {
        "name": wiki_name,
        "base url": base_url,
        "full language": full_language,
        "language": normalized_language,
        "main page": main_page,
        "content path": content_path,
        "search path": search_path,
        "icon path": icon_path,
        "wikifarm": wikifarm,
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


def validate_wiki_languages(origin_site_metadata: dict, destination_site_metadata: dict) -> bool:
    """
    Compares the language codes of a pair of wikis. Returns True if they are the same, False otherwise.
    If the pair are different dialects of the same language, print a warning.

    :param origin_site_metadata: Origin wiki's metadata
    :param destination_site_metadata: Destination wiki's metadata
    :return: Base language code
    """
    # Compare base languages
    origin_base_language = origin_site_metadata["language"]
    destination_base_language = destination_site_metadata["language"]
    if origin_base_language != destination_base_language:
        return False

    # Compare full languages (i.e. including dialect)
    origin_full_language = origin_site_metadata.get("full language", origin_base_language)
    destination_full_language = destination_site_metadata.get("full language", destination_base_language)

    # If the languages are the same but dialects differ, print a warning
    if origin_full_language != destination_full_language:
        print(f"⚠ WARNING: Cross-dialect redirection from '{origin_full_language}' to '{destination_full_language}'")

    return True


def get_wikifarm(base_url: str, logo_url: str = "") -> Optional[str]:
    # If the site URL or logo URL contains the name of a wikifarm, assume the wiki is hosted on that wikifarm
    # Checking the logo URL should catch any wikis hosted on a wikifarm that use a custom URL

    # This is only relevant for destinations, so "fandom" is not checked for (and it would likely give false positives)
    known_wikifarms = {"shoutwiki", "wiki.gg", "miraheze", "wikitide"}

    for wikifarm in known_wikifarms:
        if wikifarm in base_url or wikifarm in logo_url:
            return wikifarm
    return None


def generate_origin_entry(origin_site_metadata: dict) -> dict:
    origin_entry = {
        "origin": origin_site_metadata["name"],
        "origin_base_url": origin_site_metadata["base url"],
        "origin_content_path": origin_site_metadata["content path"],
        "origin_main_page": origin_site_metadata["main page"],
    }
    return origin_entry


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
    # Generate origin entry
    origin_entry = generate_origin_entry(origin_site_metadata)

    # Generate an entry id if it was not provided
    if entry_id is None:
        language = destination_site_metadata["language"]
        entry_id = generate_entry_id(language, origin_entry["base_url"])

    # Generate redirect entry
    entry = {
        "id": entry_id,
        "origins_label": origin_entry["origin"],
        "origins": [origin_entry],
        "destination": destination_site_metadata["name"],
        "destination_base_url": destination_site_metadata["base url"],
        "destination_platform": "mediawiki",
        "destination_icon": None,  # Filename cannot be determined at this time. Populate it when the icon is added.
        "destination_main_page": destination_site_metadata["main page"],
        "destination_search_path": destination_site_metadata["search path"],
    }

    # Generate tags
    wikifarm = destination_site_metadata.get("wikifarm")
    if wikifarm is not None:
        entry["tags"] = [wikifarm]

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


def download_wiki_icon(icon_url: str, wiki_name: str, language_code: str,
                       headers: Optional[dict] = None, iwb_filepath: str | os.PathLike = ".") -> Optional[str]:
    """
    Downloads the wiki icon from the specified URL and adds it to the appropriate JSON file.

    :param icon_url: URL for the wiki's icon
    :param wiki_name: Name of the wiki the icon is for
    :param language_code: Language of the new entry (as 2-letter language code)
    :param headers: Headers to use for the favicon download (e.g. user-agent).
    :param iwb_filepath: Filepath to IWB code, if it differs from the directory the script is being run from.
    :return: Filename the downloaded icon was saved to
    """
    # Download icon file
    try:
        icon_file_response = requests.get(normalize_url(icon_url), headers=headers)
    except requests.exceptions.ConnectionError:
        icon_file_response = None

    if not icon_file_response:
        return None

    image_file = Image.open(BytesIO(icon_file_response.content))

    # Determine filepath
    icon_filename = generate_icon_filename(wiki_name)
    icon_folderpath = os.path.join(iwb_filepath, "favicons", language_code)
    if not os.path.isdir(icon_folderpath):  # If the folder doesn't already exist, create it
        os.mkdir(icon_folderpath)
        print(f"✏ Created new {language_code} icon folder")
    icon_filepath = os.path.join(icon_folderpath, icon_filename)

    # Write to file
    image_file = image_file.resize((16, 16))
    image_file.save(icon_filepath)  # PIL ensures that conversion from ICO to PNG is safe

    return icon_filename


def validate_origin_uniqueness(sites_json: list[dict], new_origin_base_url: str) -> Optional[dict]:
    """
    Identify any existing redirect from the proposed new origin URL.

    :param new_origin_base_url: URL of the origin entry planned to be inserted
    :param sites_json: Sites JSON file for the origin wiki's language
    :return: The entry that contains the origin wiki, if it already exists in the sites JSON file
    """
    for json_entry in sites_json:
        for json_origin_entry in json_entry["origins"]:
            if json_origin_entry["origin_base_url"] == new_origin_base_url:
                return json_entry

    return None


def validate_destination_uniqueness(sites_json: list[dict], new_destination_url: str) -> Optional[dict]:
    """
    Identify any existing redirect to the proposed new destination URL.

    :param sites_json: Sites JSON file for the origin wiki's language
    :param new_destination_url: Destination URL of the planned new redirect entry
    :return: The entry that contains the origin wiki, if it already exists in the sites JSON file
    """
    for json_entry in sites_json:
        if json_entry["destination_base_url"] == new_destination_url:
            return json_entry

    return None


def entry_id_is_unique(sites_json: list[dict], new_entry_id: str):
    """
    Validate that the ID is not already in use.

    :param sites_json: Sites JSON file for the origin wiki's language
    :param new_entry_id: ID of the planned new redirect entry
    :return: The entry that contains the origin wiki, if it already exists in the sites JSON file
    """
    for json_entry in sites_json:
        # If the destinations are distinct, ensure that the new ID is unique
        if json_entry["id"] == new_entry_id:
            return False
    return True


def get_sites_json_filepath(language_code, iwb_filepath: str | os.PathLike = "."):
    # Open relevant sites JSON file
    assert os.path.isdir(os.path.join(iwb_filepath, "data"))
    return os.path.join(iwb_filepath, "data", f"sites{language_code.upper()}.json")


def read_sites_json(sites_json_filepath: str | os.PathLike) -> list[dict]:
    try:
        with open(sites_json_filepath, "r", encoding='utf-8') as sites_json_file:
            return json.load(sites_json_file)
    # If there is not currently a sites JSON for this language, start from an empty list
    except OSError:
        return []


def add_redirect_entry(new_entry: dict, language_code: str, icon_url: Optional[str] = None,
                       headers: Optional[dict] = None, iwb_filepath: str | os.PathLike = ".") -> Optional[str]:
    """
    Adds a provided IWB redirection entry to the appropriate sites JSON, and returns the entry's ID.
    Also downloads the destination wiki's favicon to the appropriate folder, if necessary.

    :param new_entry: Entry to insert
    :param language_code: Language of the new entry (as 2-letter language code)
    :param icon_url: URL for the wiki's icon
    :param headers: Headers to use for the favicon download (e.g. user-agent).
    :param iwb_filepath: Filepath to IWB code, if it differs from the directory the script is being run from.
    :return: ID of the newly added entry, or None if it was not added
    """
    # Open relevant sites JSON file
    sites_json_filepath = get_sites_json_filepath(language_code, iwb_filepath=iwb_filepath)
    redirects_list = read_sites_json(sites_json_filepath)

    # Check whether origin wiki is already present
    new_origin_entry = new_entry["origins"][0]
    new_origin_entry_url = new_origin_entry['origin_base_url']
    existing_origin_redirect = validate_origin_uniqueness(redirects_list, new_origin_entry_url)
    if existing_origin_redirect is not None:
        print(f"🗙 Redirect addition failed: {new_origin_entry['origin_base_url']} already redirects to "
              f"{existing_origin_redirect['destination_base_url']}")
        return None

    # If the destination is already present, append the new origin to the existing entry
    existing_destination_redirect = validate_destination_uniqueness(redirects_list, new_entry['destination_base_url'])
    if existing_destination_redirect is not None:
        existing_destination_redirect["origins"].append(new_origin_entry)
        inserted_entry = existing_destination_redirect
        destination_is_new = False

    # If the destination is not already present, add the new entry to the list
    else:
        assert entry_id_is_unique(redirects_list, new_entry['id'])

        redirects_list.append(new_entry)
        inserted_entry = new_entry
        redirects_list.sort(key=lambda entry: entry["id"])
        destination_is_new = True

        # Download the icon file
        destination_wiki_name = inserted_entry['destination']
        if icon_url is None:
            print(f"⚠ No icon URL provided for {destination_wiki_name}")
        else:
            icon_filename = download_wiki_icon(icon_url, destination_wiki_name, language_code,
                                               headers=headers, iwb_filepath=iwb_filepath)
            if icon_filename is not None:
                inserted_entry["destination_icon"] = icon_filename
            else:
                print(f"⚠ Unable to download icon from {icon_url}")

    # Log new entry
    new_origin_entry = new_entry["origins"][0]
    print(f"ℹ Added redirect from {new_origin_entry['origin']} ({new_origin_entry['origin_base_url']}) to "
          f"{'existing entry for ' if not destination_is_new else ''}"
          f"{inserted_entry['destination']} ({inserted_entry['destination_base_url']})"
          f" at ID {inserted_entry['id']}.")

    # Save the updated sites JSON file
    with open(sites_json_filepath, "w", encoding="utf-8") as sites_json_file:
        json.dump(redirects_list, sites_json_file, indent=2, ensure_ascii=False)

    return inserted_entry["id"]


def add_redirect_entry_from_url(origin_wiki_url: str, destination_wiki_url: str, entry_id: Optional[str] = None,
                                headers: Optional[dict] = None, iwb_filepath: str | os.PathLike = ".") -> Optional[str]:
    """
    Creates a new redirect entry between the input URLs. Metadata is generated by retrieving it from the URLs.
    Also downloads the destination wiki's favicon to the appropriate folder, if necessary.

    Since this process relies on the MediaWiki API, both wikis must be MediaWiki wikis for this to be successful.

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
        print(f"⚠ Unable to add redirection from {origin_wiki_url} to {destination_wiki_url}.")
        return None

    # Request siteinfo data
    origin_siteinfo = request_siteinfo(origin_api_url, headers=headers)
    destination_siteinfo = request_siteinfo(destination_api_url, headers=headers)

    # Validate the siteinfo data
    if origin_siteinfo is None or destination_siteinfo is None:
        print(f"⚠ Unable to add redirection from {origin_wiki_url} to {destination_wiki_url}.")
        return None

    # Extract relevant metadata from the siteinfo data
    origin_site_metadata = extract_site_metadata_from_siteinfo(origin_siteinfo)
    destination_site_metadata = extract_site_metadata_from_siteinfo(destination_siteinfo)

    # Check if languages are the same
    if not validate_wiki_languages(origin_site_metadata, destination_site_metadata):
        print(f"🗙 ERROR: The wikis have different languages. "
              f"{origin_site_metadata['name']} is {origin_site_metadata['language']} "
              f"while {destination_site_metadata['name']} is {destination_site_metadata['language']}.")
        return None

    # Get properties from site metadata
    language = origin_site_metadata["language"]
    icon_url = destination_site_metadata["icon path"]

    # Generate entry
    new_entry = generate_redirect_entry(origin_site_metadata, destination_site_metadata, entry_id)

    # Add the entry
    entry_id = add_redirect_entry(new_entry, language, icon_url=icon_url, headers=headers, iwb_filepath=iwb_filepath)
    return entry_id


def confirm_property_value_cli(prop: str, expected_type: str, current_value, new_value) -> bool:
    user_input = None

    # Loop until the user confirms or rejects the change
    while user_input not in (YES_VALUES | NO_VALUES):
        user_input = input(f'⚠ {new_value} does not look like a {expected_type}. Is this value correct? (Y/N): ')
        user_input = user_input.lower().strip()

    # If the user rejects the change, cancel the edit
    if user_input in NO_VALUES:
        print(f'ℹ {prop} not changed from "{current_value}"): ')
        return False
    else:
        return True


def get_wiki_metadata_cli(site_class, key_properties, headers: Optional[dict] = None):
    """
    CLI for preparing the data for a new origin or destination site.
    Retrieves the base_url, as well as any properties specified in key_properties.

    :param site_class: Whether the site is the origin or destination site
    :param key_properties: The properties that need to be collected for this site
    :param headers: Headers to use for HTTP/HTTPS requests (e.g. user-agent)
    :return: Dict of the required properties
    """

    # Resolve input URL
    wiki_url = ""
    while wiki_url.strip() == "":
        wiki_url = input(f"📥 Enter {site_class} wiki URL: ")
    response = request_with_error_handling(wiki_url, headers=headers)

    wiki_data = {}
    auto_properties = []  # Properties that have been added automatically (i.e. that the user may want to edit)

    if not response:
        print(f"⚠ Unable to connect to {wiki_url}")

    # Check wiki software
    wiki_software = determine_wiki_software(response)

    # For MediaWiki wikis, retrieve details via the API
    if wiki_software == "mediawiki":
        print(f"🕑 Getting {site_class} site info...")
        api_url = get_api_url(response)
        if api_url is None:
            print(f"⚠ Unable to automatically retrieve API URL for {wiki_url}.")
            api_url = input(f"📥 Enter {site_class} wiki API URL: ")
            print(f"🕑 Getting {site_class} site info...")

        siteinfo = request_siteinfo(api_url, headers=headers)
        if siteinfo is not None:
            wiki_data = extract_site_metadata_from_siteinfo(siteinfo)
        else:
            print(f"⚠ Unable to retrieve metadata via MediaWiki API")

    # If the icon URL was not found via the API, try to find it from the HTML
    if "icon path" in key_properties:
        icon_path = wiki_data.get("icon path")
        if icon_path is None:
            wiki_data["icon path"] = get_favicon_url(response)

    # Check all key properties. Print retrieved ones, require manual entry of missing ones.
    for prop in key_properties:
        current_value = wiki_data.get(prop)
        if current_value is not None:
            print(f"ℹ Retrieved {site_class} wiki {prop}: {current_value}")
            auto_properties.append(prop)
        else:
            input_value = input(f"📥 Enter {site_class} wiki {prop}: ")
            if input_value.strip() != "":
                wiki_data[prop] = input_value.strip()

    # Display the wiki farm, if detected
    wikifarm = wiki_data.get("wikifarm")
    if wikifarm is not None:
        print(f"ℹ Detected as a {wikifarm} wiki")

    # Check if the user wants to edit the retrieved metadata
    if len(auto_properties) > 0:
        user_input = None
        while user_input not in (YES_VALUES | NO_VALUES):
            user_input = input(f"❔ Edit auto-generated metadata (Y/N)?: ")
            user_input = user_input.lower().strip()

            if user_input in NO_VALUES:
                return wiki_data
            elif user_input in YES_VALUES:
                break
            print("⚠ Unrecognized input. Please enter Y or N, or leave blank to skip.")

        # Allow the user to edit the properties
        print(f"ℹ Enter new values for {site_class} wiki properties. Leave blank to retain the current value.")
        for prop in auto_properties:
            current_value = wiki_data.get(prop, "null")
            new_value = input(f'📥 Enter {site_class} wiki {prop} (current value: "{current_value}"): ')
            new_value = new_value.strip()
            if new_value != "":
                # Basic sanity checks for common input errors
                if "url" in prop and (" " in new_value or "." not in new_value):
                    if not confirm_property_value_cli(prop, "URL", current_value, new_value):
                        continue
                elif "path" in prop and "/" not in new_value:
                    if not confirm_property_value_cli(prop, "path", current_value, new_value):
                        continue

                # Update the property
                wiki_data[prop] = new_value
                print(f'ℹ Updated {prop} to "{new_value}"')

    return wiki_data


def main():
    """
    Interactive CLI for adding new wikis one at a time
    """
    headers = {'User-Agent': 'Mozilla/5.0'}
    iwb_filepath = ".."

    # Get origin wiki data
    origin_key_properties = ["base url", "name", "language", "main page", "content path"]
    origin_site_metadata = get_wiki_metadata_cli("origin", origin_key_properties, headers=headers)

    # Determine sites JSON filepath
    language = origin_site_metadata["language"]
    sites_json_filepath = get_sites_json_filepath(language, iwb_filepath)

    # Check whether origin wiki is already present
    redirects_list = read_sites_json(sites_json_filepath)
    origin_base_url = origin_site_metadata["base url"]

    existing_origin_redirect = validate_origin_uniqueness(redirects_list, origin_base_url)
    if existing_origin_redirect is not None:
        print(f"🗙 Redirect addition failed: {origin_base_url} already redirects to "
              f"{existing_origin_redirect['destination_base_url']}")
        return
    else:
        print(f"✅ {origin_base_url} is a new entry!")

    # Get destination wiki data
    destination_key_properties = ["base url", "name", "language", "main page", "search path", "platform", "icon path"]
    destination_site_metadata = get_wiki_metadata_cli("destination", destination_key_properties, headers=headers)

    # Validate wiki language
    if not validate_wiki_languages(origin_site_metadata, destination_site_metadata):
        origin_language = origin_site_metadata["language"]
        destination_language = destination_site_metadata["language"]
        print(f"🗙 Redirect addition failed: The wikis are in different languages "
              f"('{origin_language}' and '{destination_language}')!")
        return

    # If the destination wiki is already present, just append the origin to the existing entry
    destination_base_url = destination_site_metadata["base url"]
    existing_destination_redirect = validate_destination_uniqueness(redirects_list, destination_base_url)
    if existing_destination_redirect is not None:
        destination_wiki_name = existing_destination_redirect["destination"]
        print(f"ℹ Destination wiki {destination_wiki_name} ({destination_base_url}) already has an entry. "
              f"Adding {origin_base_url} redirect to the existing entry.")
        origin_entry = generate_origin_entry(origin_site_metadata)

        existing_destination_redirect["origins"].append(origin_entry)

        print("🗒 Generated the following data:\n")
        print(json.dumps(existing_destination_redirect, indent=2, ensure_ascii=False))

    # Otherwise, add a new redirect entry to the list
    else:
        print(f"✅ {destination_base_url} is a new entry!")

        # Have the user enter a unique entry ID
        default_topic_id = extract_topic_from_url(origin_base_url)
        entry_id = None
        while entry_id is None:
            # Determine entry ID
            topic_id = input(f"📥 Enter entry ID — series name, one word, no dashes "
                             f"(default: {default_topic_id}): ").strip()
            if topic_id == "":
                topic_id = default_topic_id

            entry_id = language + "-" + topic_id

            if not entry_id_is_unique(redirects_list, entry_id):
                print(f"⚠ Entry ID {entry_id} is already in use. Please enter a different entry ID.")
                entry_id = None

        # Generate redirect entry
        new_entry = generate_redirect_entry(origin_site_metadata, destination_site_metadata, entry_id)

        # Download the icon file
        icon_url = destination_site_metadata["icon path"]
        while icon_url is None or icon_url.strip() == "":
            icon_url = input("📥 Enter destination wiki icon path: ")

        destination_wiki_name = destination_site_metadata["name"]
        print("🕑 Grabbing destination wiki's favicon...")
        icon_filename = download_wiki_icon(icon_url, destination_wiki_name, language,
                                           headers=headers, iwb_filepath=iwb_filepath)
        if icon_filename is not None:
            print(f"🖼 Favicon saved as {icon_filename}!")
            new_entry["destination_icon"] = icon_filename
        else:
            print(f"⚠ Unable to download icon from {icon_url}")

        print("🗒 Generated the following data:\n")
        print(json.dumps(new_entry, indent=2, ensure_ascii=False))

        # Add new redirect to the redirects list
        redirects_list.append(new_entry)
        redirects_list.sort(key=lambda entry: entry["id"])

    # Save the updated sites JSON file
    sites_json_filename = os.path.basename(sites_json_filepath)
    print(f"🕑 Saving data to {sites_json_filename}...")
    with open(sites_json_filepath, "w", encoding="utf-8") as sites_json_file:
        json.dump(redirects_list, sites_json_file, indent=2, ensure_ascii=False)
    print(f"💾 {sites_json_filename} successfully updated!")

    print("✅ All done!")


if __name__ == '__main__':
    main()
