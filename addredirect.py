"""
Python script for adding a new wiki redirect
"""
import json
import warnings

import lxml.html
import os
import re
import requests
import unicodedata
from io import BytesIO
from urllib.parse import urlparse
from requests.exceptions import RequestException
from typing import Optional, Iterable

from utils import (normalize_url_protocol, request_with_http_fallback, WikiSoftware, read_user_config,
                   download_wiki_icon, get_iwb_filepath, confirm_yes_no,
                   DEFAULT_TIMEOUT, ORIGIN_ENTRY_PROPERTIES, DESTINATION_ENTRY_PROPERTIES)
from addlanguage import add_language
from profilewiki import profile_wiki, determine_wiki_software
from mediawiki_tools import (get_mediawiki_api_url, query_mediawiki_api, get_mediawiki_favicon_url,
                             extract_metadata_from_siteinfo, MediaWikiAPIError)
from fextralife_tools import extract_metadata_from_fextralife_page
from dokuwiki_tools import profile_dokuwiki_wiki
from wikidot_tools import profile_wikidot_wiki


def extract_topic_from_url(wiki_url: str) -> str:
    """
    Auto-generate the topic portion of an Entry ID, based on the subdomain of the origin wiki.

    :param wiki_url: URL of the origin wiki
    :return: topic of an entry ID string
    """
    # Retrieve the subdomain
    normalized_url = normalize_url_protocol(wiki_url)
    hostname = urlparse(normalized_url).hostname
    domain_parts = hostname.split('.')
    subdomain = domain_parts[0]

    # Extract topic from subdomain
    if hostname.endswith(".fextralife.com"):
        return subdomain.split('-')[0]  # Split language off Fextralife subdomain
    else:
        return subdomain.replace("-", "")


def generate_entry_id(language: str, origin_url: str) -> str:
    """
    Auto-generate an entry ID, based on the language and URL of the origin wiki.

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
    origin_full_language = origin_site_metadata.get("full_language", origin_base_language)
    destination_full_language = destination_site_metadata.get("full_language", destination_base_language)

    # If the languages are the same but dialects differ, print a warning
    if origin_full_language != destination_full_language:
        print(f"⚠ WARNING: Cross-dialect redirection from '{origin_full_language}' to '{destination_full_language}'")

    return True


def generate_origin_entry(origin_site_metadata: dict) -> dict:
    origin_entry = {
        "origin": origin_site_metadata["name"],
    }
    for prop in ORIGIN_ENTRY_PROPERTIES:
        origin_entry["origin" + "_" + prop] = origin_site_metadata.get(prop)

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
        entry_id = generate_entry_id(language, origin_entry["origin_base_url"])

    # Generate redirect entry
    entry = {
        "id": entry_id,
        "origins_label": origin_entry["origin"],
        "origins": [origin_entry],
        "destination": destination_site_metadata["name"],
    }
    for prop in DESTINATION_ENTRY_PROPERTIES:
        entry["destination" + "_" + prop] = destination_site_metadata.get(prop)

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


def get_sites_json_filepath(language_code: str, iwb_filepath: str | os.PathLike = ".") -> str:
    """
    Gets the filepath of the sites JSON file for a particular language.

    :param language_code: The language of the sites JSON file
    :param iwb_filepath: Filepath to Indie Wiki Buddy repo
    :return: Filepath of the corresponding sites JSON file
    """
    # Open relevant sites JSON file
    assert os.path.isdir(os.path.join(iwb_filepath, "data"))
    return os.path.join(iwb_filepath, "data", f"sites{language_code.upper()}.json")


def read_sites_json(sites_json_filepath: str | os.PathLike) -> list[dict]:
    """
    Read the contents of a sites JSON file.

    :param sites_json_filepath: Filepath of a sites JSON file
    :return: Contents of the sites JSON file (or an empty list if it doesn't exist)
    """
    # If there is not currently a sites JSON for this language, start from an empty list
    if not os.path.isfile(sites_json_filepath):
        return []

    try:
        with open(sites_json_filepath, "r", encoding='utf-8') as sites_json_file:
            return json.load(sites_json_file)
    # Handle the JSON file including a UTF-8 BOM
    except json.JSONDecodeError as e:
        if e.msg == "Unexpected UTF-8 BOM (decode using utf-8-sig)":
            with open(sites_json_filepath, "r", encoding='utf-8-sig') as sites_json_file:
                warnings.warn(f"{os.path.basename(sites_json_filepath)} includes a UTF-8 Byte Order Mark (BOM). "
                              f"Use of a BOM is not recommended in UTF-8.\n"
                              f"The BOM will be removed if any changes are saved to the JSON file.", UnicodeWarning)
                return json.load(sites_json_file)
        else:
            raise e


def add_redirect_entry(new_entry: dict, language_code: str, icon_url: Optional[str] = None,
                       iwb_filepath: str | os.PathLike = ".", **kwargs) -> Optional[str]:
    """
    Adds a provided IWB redirection entry to the appropriate sites JSON, and returns the entry's ID.
    Also downloads the destination wiki's favicon to the appropriate folder, if necessary.

    :param new_entry: Entry to insert
    :param language_code: Language of the new entry (as 2-letter language code)
    :param icon_url: URL for the wiki's icon
    :param kwargs: kwargs to use for the HTTP requests
    :param iwb_filepath: Filepath to IWB code, if it differs from the directory the script is being run from.
    :return: ID of the newly added entry, or None if it was not added
    """
    # If this is a new language, add this language to the list of supported languages
    sites_json_filepath = get_sites_json_filepath(language_code, iwb_filepath=iwb_filepath)
    if not os.path.isfile(sites_json_filepath):
        add_language(language_code, iwb_filepath=iwb_filepath)

    # Open relevant sites JSON file
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
            icon_filename = generate_icon_filename(destination_wiki_name)
            icon_filename = download_wiki_icon(icon_url, icon_filename, language_code, iwb_filepath=iwb_filepath,
                                               **kwargs)
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
                                iwb_filepath: str | os.PathLike = ".", **kwargs) -> Optional[str]:
    """
    Creates a new redirect entry between the input URLs. Metadata is generated by retrieving it from the URLs.
    Also downloads the destination wiki's favicon to the appropriate folder, if necessary.

    :param origin_wiki_url: URL of the origin wiki
    :param destination_wiki_url: URL of the destination wiki
    :param entry_id: ID to use for the new entry. Determined automatically if not specified.
    :param kwargs: kwargs to use for the HTTP requests
    :param iwb_filepath: Filepath to IWB code, if it differs from the directory the script is being run from.
    :return: ID of the newly added entry, or None if it was not added
    """
    # Check the IWB filepath is correct
    if not os.path.isdir(os.path.join(iwb_filepath, "data")):
        raise OSError('Cannot find the "data" folder. Ensure that you specified the correct "iwb_filepath".')

    # Get site metadata for both wikis
    try:
        origin_site_metadata = profile_wiki(origin_wiki_url, full_profile=False, **kwargs)
        if origin_site_metadata is None:
            print(f"🗙 ERROR: Unable to retrieve metadata from {origin_wiki_url}")

        destination_site_metadata = profile_wiki(destination_wiki_url, full_profile=False, **kwargs)
        if destination_site_metadata is None:
            print(f"🗙 ERROR: Unable to retrieve metadata from {destination_wiki_url}")

    except (RequestException, MediaWikiAPIError) as e:
        print(e)
        return None

    # Check if languages are the same
    if not validate_wiki_languages(origin_site_metadata, destination_site_metadata):
        print(f"🗙 ERROR: The wikis have different languages. "
              f"{origin_site_metadata['name']} is {origin_site_metadata['language']} "
              f"while {destination_site_metadata['name']} is {destination_site_metadata['language']}.")
        return None

    # Get properties from site metadata
    language = origin_site_metadata["language"]
    icon_url = destination_site_metadata["icon_path"]

    # Generate entry
    new_entry = generate_redirect_entry(origin_site_metadata, destination_site_metadata, entry_id)

    # Add the entry
    entry_id = add_redirect_entry(new_entry, language, icon_url=icon_url, iwb_filepath=iwb_filepath, **kwargs)
    return entry_id


def get_wiki_metadata_cli(site_class: str, key_properties: Iterable, session: Optional[requests.Session] = None,
                          **kwargs):
    """
    CLI for preparing the data for a new origin or destination site.
    Retrieves the base_url, as well as any properties specified in key_properties.

    :param site_class: Whether the site is the origin or destination site
    :param key_properties: The properties that need to be collected for this site
    :param session: requests Session to use for resolving the URL
    :param kwargs: kwargs to use for the HTTP requests
    :return: Dict of the required properties
    """
    # Create a new session if one was not provided
    if session is None:
        session = requests.Session()

    # Resolve input URL
    wiki_url = ""
    while wiki_url == "":
        wiki_url = input(f"📥 Enter {site_class} wiki URL: ").strip()
    try:
        response = request_with_http_fallback(wiki_url, session=session, **kwargs)
    except RequestException as e:
        print(e)
        response = None

    wiki_data = {}

    if not response:
        print(f"⚠ Unable to connect to {wiki_url} (Error {response.status_code}: {response.reason}). "
              f"Details will need to be entered manually.")
    else:
        # Check wiki software
        parsed_html = lxml.html.parse(BytesIO(response.content))
        wiki_software = determine_wiki_software(parsed_html)

        # For MediaWiki wikis, retrieve details via the API
        if wiki_software == WikiSoftware.MEDIAWIKI:
            print(f"ℹ Detected MediaWiki software")
            print(f"🕑 Getting {site_class} wiki info...")
            api_url = get_mediawiki_api_url(response, session=session, **kwargs)
            if api_url is None:
                print(f"⚠ Unable to automatically retrieve API URL for {wiki_url}.")
                api_url = input(f"📥 Enter {site_class} wiki API URL: ")
                print(f"🕑 Getting {site_class} site info...")

            siteinfo_params = {'action': 'query', 'meta': 'siteinfo', 'siprop': 'general', 'format': 'json'}
            siteinfo = query_mediawiki_api(api_url, params=siteinfo_params, session=session, **kwargs)
            wiki_data = extract_metadata_from_siteinfo(siteinfo)

            # If the search path was not retrieved, manually derive it from the API URL
            if wiki_data.get("search_path") is None:
                wiki_data["search_path"] = urlparse(api_url).path.replace("/api.php", "index.php")

            # If the icon URL was not found via the standard method, try to find it from the HTML
            if "icon_path" in key_properties:
                icon_path = wiki_data.get("icon_path")
                if icon_path is None:
                    wiki_data["icon_path"] = get_mediawiki_favicon_url(response)

        elif wiki_software == WikiSoftware.FEXTRALIFE:
            print(f"ℹ Detected Fextralife software")
            print(f"🕑 Getting {site_class} wiki info...")
            wiki_data = extract_metadata_from_fextralife_page(response)

        elif wiki_software == WikiSoftware.DOKUWIKI:
            print(f"ℹ Detected DokuWiki software")
            print(f"🕑 Getting {site_class} wiki info...")
            wiki_data = profile_dokuwiki_wiki(response, full_profile=False, session=session, **kwargs)

        elif wiki_software == WikiSoftware.WIKIDOT:
            print(f"ℹ Detected Wikidot software")
            print(f"🕑 Getting {site_class} wiki info...")
            wiki_data = profile_wikidot_wiki(response, full_profile=False, session=session, **kwargs)

        else:
            print(f"⚠ {wiki_url} uses currently unsupported software. Details will need to be entered manually.")

    # Check all key properties. Print retrieved ones, require manual entry of missing ones.
    auto_properties = []  # Properties that have been added automatically (i.e. that the user may want to edit)
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
        if not confirm_yes_no(f"❔ Edit auto-generated metadata (Y/N)?: "):
            return wiki_data

        # Allow the user to edit the properties
        print(f"ℹ Enter new values for {site_class} wiki properties. Leave blank to retain the current value.")
        for prop in auto_properties:
            current_value = wiki_data.get(prop, "null")
            new_value = input(f'📥 Enter {site_class} wiki {prop} (current value: "{current_value}"): ')
            new_value = new_value.strip()
            if new_value != "":
                # Basic sanity checks for common input errors
                if "url" in prop and (" " in new_value or "." not in new_value):
                    # If the user rejects the change, cancel the edit
                    if not confirm_yes_no(f'⚠ {new_value} does not look like a URL. Is this value correct? (Y/N): '):
                        print(f'ℹ {prop} not changed from "{current_value}"): ')
                        continue
                elif "path" in prop and "/" not in new_value:
                    if not confirm_yes_no(f'⚠ {new_value} does not look like a path. Is this value correct? (Y/N): '):
                        print(f'ℹ {prop} not changed from "{current_value}"): ')
                        continue

                # Update the property
                wiki_data[prop] = new_value
                print(f'ℹ Updated {prop} to "{new_value}"')

    return wiki_data


def main():
    """
    Interactive CLI for adding new wikis one at a time
    """
    # Prepare user-agent
    headers = {'User-Agent': read_user_config("User-Agent")}  # case-sensitive key, unlike the HTTP header

    # Get IWB filepath
    iwb_filepath = get_iwb_filepath()

    # Get origin wiki data
    origin_session = requests.Session()
    origin_key_properties = ["base_url", "name", "language", "main_page", "content_path"]
    origin_site_metadata = get_wiki_metadata_cli("origin", origin_key_properties, session=origin_session,
                                                 headers=headers, timeout=DEFAULT_TIMEOUT)

    # Determine sites JSON filepath
    language = origin_site_metadata["language"]
    sites_json_filepath = get_sites_json_filepath(language, iwb_filepath)

    # Check whether this language is already supported
    if not os.path.isfile(sites_json_filepath):
        user_choice = confirm_yes_no(f"⚠ {language} is not currently supported. Would you like to add a new language"
                                     f" (Y/N)?: ")
        if not user_choice:
            print("🗙 Redirect addition aborted by user due to new language")
            return
        else:
            print(f"🕑 Updating IWB data to support new language {language}...")
            add_language(language, iwb_filepath)

    # Check whether origin wiki is already present
    redirects_list = read_sites_json(sites_json_filepath)
    origin_base_url = origin_site_metadata["base_url"]

    existing_origin_redirect = validate_origin_uniqueness(redirects_list, origin_base_url)
    if existing_origin_redirect is not None:
        print(f"🗙 Redirect addition failed: {origin_base_url} already redirects to "
              f"{existing_origin_redirect['destination_base_url']}")
        return
    else:
        print(f"✅ {origin_base_url} is a new entry!")

    # Get destination wiki data
    destination_session = requests.Session()
    destination_key_properties = ["base_url", "name", "language", "main_page", "search_path", "platform", "icon_path"]
    destination_site_metadata = get_wiki_metadata_cli("destination", destination_key_properties,
                                                      session=destination_session, headers=headers,
                                                      timeout=DEFAULT_TIMEOUT)

    # Validate wiki language
    if not validate_wiki_languages(origin_site_metadata, destination_site_metadata):
        origin_language = origin_site_metadata["language"]
        destination_language = destination_site_metadata["language"]
        print(f"🗙 Redirect addition failed: The wikis are in different languages "
              f"('{origin_language}' and '{destination_language}')!")
        return

    # If the destination wiki is already present, just append the origin to the existing entry
    destination_base_url = destination_site_metadata["base_url"]
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
        icon_url = destination_site_metadata["icon_path"]
        while icon_url is None or icon_url.strip() == "":
            icon_url = input("📥 Enter destination wiki icon path: ")

        destination_wiki_name = destination_site_metadata["name"]
        print("🕑 Grabbing destination wiki's favicon...")
        icon_filename = generate_icon_filename(destination_wiki_name)
        icon_filename = download_wiki_icon(icon_url, icon_filename, language, iwb_filepath=iwb_filepath,
                                           session=destination_session, headers=headers, timeout=DEFAULT_TIMEOUT)
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
