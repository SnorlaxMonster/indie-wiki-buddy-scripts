"""
Python script for adding a new wiki redirect
"""
import re
import json
import os
import urllib.parse
import requests
import lxml.html
from io import StringIO, BytesIO
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
    parsed_url = urllib.parse.urlparse(url)
    return parsed_url.hostname


def normalize_url(url: str, default_scheme="https") -> str:
    """
    Enforces that the URL specifies a scheme.

    :param url: Unnormalized URL
    :return: URL with a scheme, or // prefix to indicate the lack of one
    """
    if url.startswith(("http://", "https://")):
        return url
    elif url.startswith("//"):
        return f"{default_scheme}:{url}"
    else:
        return f"{default_scheme}://{url}"


def normalize_wikia_url(original_url: str) -> str:
    """
    Old Wikia URLs included the language as a subdomain for non-English wikis, but these URLs no longer work.
    Non-English Wikia URLs need to be modified to move the language to the path of the URL.

    :param original_url: Wikia URL
    :return: Modified URL with language moved to the path, if necessary
    """
    parsed_url = urllib.parse.urlparse(normalize_url(original_url))

    lang_match = re.match(r"([a-z]+)\.(.*)\.(?:fandom|wikia)\.com", parsed_url.hostname)
    if lang_match:
        # Always use "fandom.com" when restructuring URLs
        lang, subdomain = lang_match.groups()
        new_domain = f"{subdomain}.fandom.com"
        new_path = urllib.parse.urljoin(lang, parsed_url.path)
        parsed_url = parsed_url._replace(netloc=new_domain, path=new_path)

        return urllib.parse.urlunparse(parsed_url)

    else:
        return original_url


def normalize_api_url(parsed_api_url: urllib.parse.ParseResult, parsed_response_url: urllib.parse.ParseResult) -> str:
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

    return urllib.parse.urlunparse(parsed_api_url)


def request_with_error_handling(raw_url: str, params: Optional[dict] = None, headers: Optional[dict] = None,
                                ignorable_errors: Optional[list[int]] = None) -> Optional[requests.Response]:
    """
    Given a base URL, attempts to resolve the URL as HTTPS, then falls back to HTTP if that isn't possible.

    :param url: URL to resolve
    :param params: URL parameters
    :param headers: Headers to include in the request (e.g. user-agent)
    :param ignorable_errors: Error codes that should be ignored as long as the response is not null
    :return: GET request response
    """
    params = [] if (params is None) else params
    ignorable_errors = [] if (ignorable_errors is None) else ignorable_errors
    url = normalize_url(raw_url)

    # GET request the URL
    try:
        response = requests.get(url, params=params, headers=headers)

    # If using HTTPS results in an SSLError, try HTTP instead
    except requests.exceptions.SSLError:
        parsed_url = urllib.parse.urlparse(url)
        if parsed_url.scheme != "http":
            print(f"WARNING: SSLError. Defaulting to HTTP connection for {raw_url}")
            url = urllib.parse.urlunparse(parsed_url._replace(scheme="http"))
            return request_with_error_handling(url, params=params, headers=headers)
        else:
            print(f"SSLError: Unable to connect to {raw_url} , even via HTTP")
            return None

    # If unable to connect to the URL at all, return None
    except requests.exceptions.ConnectionError:
        print(f"ConnectionError: Unable to connect to {url}")
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

    :param wiki_url: Wiki's URL
    :param headers: Headers to include in the request (e.g. user-agent)
    :return: Wiki's API URL
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


def get_api_url(wiki_url: str, headers: Optional[dict] = None) -> Optional[str]:
    """
    Given a wiki's URL, determines what its API URL is.

    :param wiki_url: Wiki's URL
    :param headers: Headers to include in the request (e.g. user-agent)
    :return: Wiki's API URL
    """

    # Non-English Wikia URLs need the language moved from a subdomain to the path
    if "wikia.com" in wiki_url:
        wiki_url = normalize_wikia_url(wiki_url)

    response = request_with_error_handling(wiki_url, headers=headers)
    if response is None:
        return None

    # Parse the HTML
    parsed_html = lxml.html.parse(StringIO(response.text))

    # If the site is not a MediaWiki wiki, abort trying to determine the API URL
    if not is_mediawiki(parsed_html):
        print(f"{wiki_url} is not a MediaWiki page")
        return None

    # Retrieve the API URL via EditURI element
    edit_uri_node = parsed_html.find('/head/link[@rel="EditURI"]')
    if edit_uri_node is not None:
        parsed_api_url = urllib.parse.urlparse(edit_uri_node.get('href'))
        parsed_response_url = urllib.parse.urlparse(response.url)

        return normalize_api_url(parsed_api_url, parsed_response_url)

    # If EditURI is missing, try to find the permalink URL and determine the API URL from that
    permalink_node = parsed_html.find('//li[@id="t-permalink"]/a')
    if permalink_node is not None:
        print(f"WARNING: Retrieved API URL for {wiki_url} via permalink node")
        permalink_url = permalink_node.get("href")
        parsed_permalink_url = urllib.parse.urlparse(permalink_url)
        parsed_api_url = parsed_permalink_url._replace(path=parsed_permalink_url.path.replace('index.php', 'api.php'))
        parsed_response_url = urllib.parse.urlparse(response.url)

        return normalize_api_url(parsed_api_url, parsed_response_url)

    # Otherwise, the API URL retrieval has failed
    print(f"Unable to determine API URL for {wiki_url}")
    return None


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
    Runs a "siteinfo" query using the MediaWiki API of the specified wiki, for 'general' and 'statistics' data.

    :param api_url: MediaWiki API URL
    :param headers: Headers to include in the request (e.g. user-agent).
    :return: siteinfo query result
    """
    siteinfo_params = {'action': 'query', 'meta': 'siteinfo', 'siprop': 'general|statistics', 'format': 'json'}
    siteinfo = query_wiki_api(api_url, siteinfo_params, headers=headers)

    return siteinfo


def generate_entry_id(language: str, origin_url: str) -> str:
    """
    Auto-generate an entry ID, based on the language and subdomain of the origin wiki.

    :param language: Normalized language code
    :param origin_url: URL of the origin wiki
    :return: entry ID string
    """
    # Retrieve the topic
    hostname = extract_hostname(origin_url)
    domain_parts = hostname.split('.')
    topic = domain_parts[0].replace("-", "")

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


def generate_origin_entry(origin_siteinfo: dict) -> dict:
    """
    Using siteinfo metadata from the origin wiki, generates an IWB origin entry that can be added to a redirection entry
    in a sites JSON file.

    :param origin_siteinfo: Origin's MediaWiki API response for a "siteinfo" query including siprop=general
    :return: IWB origin entry for a redirection entry in a sites JSON file
    """
    origin_siteinfo_general = origin_siteinfo["general"]
    origin_sitename = origin_siteinfo_general["sitename"].replace(" Wiki", " Fandom Wiki")
    origin_language = origin_siteinfo_general["lang"]
    origin_base_url = extract_hostname(origin_siteinfo_general["base"])
    origin_content_path = origin_siteinfo_general["articlepath"].replace("$1", "")

    # For Fandom wikis, ensure the language path is part of the base_url instead of the content_path
    if origin_language != "en" and "fandom.com" in origin_base_url:
        origin_full_path_parts = (origin_base_url + origin_content_path).split("/")
        if origin_full_path_parts[1] == origin_language:
            origin_base_url = "/".join(origin_full_path_parts[0:2])
            origin_content_path = "/" + "/".join(origin_full_path_parts[2:])

    entry = {
        "origin": origin_sitename,
        "origin_base_url": origin_base_url,
        "origin_content_path": origin_content_path,
        "origin_main_page": origin_siteinfo_general["mainpage"].replace(" ", "_"),
    }
    return entry


def generate_redirect_entry(origin_siteinfo: dict, destination_siteinfo: dict, entry_id: Optional[str] = None) -> dict:
    """
    Using siteinfo metadata from the origin and destination wikis, generates an IWB redirection entry that can be added
    to the sites JSON file. The icon file is not downloaded in this process, so "destination_icon" is set to null.

    :param origin_siteinfo: Origin's MediaWiki API response for a "siteinfo" query including siprop=general
    :param destination_siteinfo: Destination's MediaWiki API response for a "siteinfo" query including siprop=general
    :param entry_id: ID to use for the new entry. Determined automatically if not specified.
    :return: IWB redirection entry for a sites JSON file
    """
    origin_siteinfo_general = origin_siteinfo["general"]
    destination_siteinfo_general = destination_siteinfo["general"]

    # Generate an entry id if it was not provided
    if entry_id is None:
        language = get_wiki_language(origin_siteinfo_general["lang"], destination_siteinfo_general["lang"])
        entry_id = generate_entry_id(language, origin_siteinfo_general["base"])

    # Generate origin entry
    origin_entry = generate_origin_entry(origin_siteinfo)

    entry = {
        "id": entry_id,
        "origins_label": origin_entry["origin"],
        "origins": [origin_entry],
        "destination": destination_siteinfo_general["sitename"],
        "destination_base_url": extract_hostname(destination_siteinfo_general["base"]),
        "destination_platform": "mediawiki",
        "destination_icon": None,  # Filename cannot be determined at this time. Populate it when the icon is added.
        "destination_main_page": destination_siteinfo_general["mainpage"].replace(" ", "_"),
        "destination_search_path": destination_siteinfo_general["script"],
    }

    # Generate tags
    tags = []

    # If the site URL or logo URL contains the name of a wikifarm, assume the wiki is hosted on that wikifarm
    # Checking the logo URL should catch any wikis hosted on a wikifarm that use a custom URL
    destination_url = destination_siteinfo_general["base"]
    destination_logo_url = destination_siteinfo_general.get("logo", "")
    for wikifarm in ("shoutwiki", "wiki.gg", "miraheze", "wikitide"):
        if wikifarm in destination_url or wikifarm in destination_logo_url:
            tags.append(wikifarm)

    if len(tags) > 0:
        entry["tags"] = tags

    return entry


def generate_favicon_filename(sitename: str) -> str:
    """
    Auto-generates a filename for the icon file, by stripping all non-alphabetic characters from the sitename.
    File will be saved as .png

    :param sitename: Name of the wiki
    :return: Filename of the icon file
    """
    return "".join(c for c in sitename.lower() if c.isalpha()) + ".png"


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


def add_redirect_entry(origin_siteinfo: dict, destination_siteinfo: dict, entry_id: Optional[str] = None,
                       headers: Optional[dict] = None, iwb_filepath: str = ".") -> Optional[str]:
    """
    Using siteinfo metadata from the origin and destination wikis, generates and adds an IWB redirection entry to the
    appropriate sites JSON. Also downloads the destination wiki's favicon to the appropriate folder, if necessary.

    :param origin_siteinfo: Origin's MediaWiki API response for a "siteinfo" query including siprop=general
    :param destination_siteinfo: Destination's MediaWiki API response for a "siteinfo" query including siprop=general
    :param entry_id: ID to use for the new entry. Determined automatically if not specified.
    :param headers: Headers to use for the favicon download (e.g. user-agent).
    :param iwb_filepath: Filepath to IWB code, if it differs from the directory the script is being run from.
    :return: ID of the newly added entry, or None if it was not added
    """
    origin_siteinfo_general = origin_siteinfo["general"]
    destination_siteinfo_general = destination_siteinfo["general"]

    # Get wiki language
    language = get_wiki_language(origin_siteinfo_general["lang"], destination_siteinfo_general["lang"])

    # Generate entry
    if entry_id is None:
        entry_id = generate_entry_id(language, origin_siteinfo_general["base"])
    new_entry = generate_redirect_entry(origin_siteinfo, destination_siteinfo, entry_id)

    # Open relevant sites JSON file
    assert os.path.isdir(os.path.join(iwb_filepath, "data"))
    sites_json_filepath = os.path.join(iwb_filepath, "data", f"sites{language.upper()}.json")
    try:
        with open(sites_json_filepath) as sites_json_file:
            sites_json = json.load(sites_json_file)
    except OSError:  # If there is not currently a sites JSON for this language, start from an empty list
        sites_json = []

    # Check whether either wiki is already present
    new_origin_entry = new_entry["origins"][0]
    destination_is_new = True
    for json_entry in sites_json:
        # Check that the origin is distinct
        for json_origin_entry in json_entry["origins"]:
            if json_origin_entry["origin_base_url"] == new_origin_entry["origin_base_url"]:
                print(f"Redirect addition failed: {json_origin_entry['origin_base_url']} already redirects to "
                      f"{json_entry['destination_base_url']}")
                return None  # If the origin is already present, do not attempt to add it again

        # Check if the destination is distinct
        if new_entry["destination_base_url"] == json_entry["destination_base_url"]:
            # If the destination is already present, add the origin as a new entry to the existing
            json_entry["origins"].append(new_origin_entry)
            destination_is_new = False
            print(f"Added redirect from {new_origin_entry['origin']} ({new_origin_entry['origin_base_url']}) to "
                  f"existing entry for {json_entry['destination']} ({json_entry['destination_base_url']}) "
                  f"at {json_entry['id']}.")

        # Check that the new ID is unique, since we know that the destinations are distinct
        elif new_entry["id"] == json_entry["id"]:
            raise DuplicateIDError(new_entry["id"])

    # If the destination is not already present, add a new entry for it
    if destination_is_new:
        # Download the favicon
        icon_url = destination_siteinfo_general.get("favicon")
        if icon_url is not None:
            icon_filename = generate_favicon_filename(new_entry["destination"])
            image_file = download_icon(icon_url, icon_filename, language, iwb_filepath=iwb_filepath, headers=headers)
            if image_file is not None:  # If the download was successful
                new_entry["destination_icon"] = icon_filename
        else:
            print(f"Unable to determine favicon URL for {new_entry['destination']}")

        # Add the entry and sort the list
        sites_json.append(new_entry)
        sites_json.sort(key=lambda entry: entry["id"])

        print(f"Added redirect from {new_origin_entry['origin']} ({new_origin_entry['origin_base_url']}) to "
              f"{new_entry['destination']} ({new_entry['destination_base_url']}) at {new_entry['id']}.")

    # Save the updated sites JSON file
    with open(sites_json_filepath, "w") as sites_json_file:
        json.dump(sites_json, sites_json_file, indent=2, ensure_ascii=False)

    return entry_id


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
    origin_api_url = get_api_url(origin_wiki_url, headers=headers)
    destination_api_url = get_api_url(destination_wiki_url, headers=headers)

    # Validate API URL
    if origin_api_url is None or destination_api_url is None:
        print(f"Unable to add redirection from {origin_wiki_url} to {destination_wiki_url}.")
        return None

    origin_siteinfo = request_siteinfo(origin_api_url, headers=headers)
    destination_siteinfo = request_siteinfo(destination_api_url, headers=headers)

    # Validate the siteinfo data
    if origin_siteinfo is None or destination_siteinfo is None:
        print(f"Unable to add redirection from {origin_wiki_url} to {destination_wiki_url}.")
        return None

    # Add the entry
    entry_id = add_redirect_entry(origin_siteinfo, destination_siteinfo, entry_id,
                                  headers=headers, iwb_filepath=iwb_filepath)
    return entry_id
