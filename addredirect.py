"""
Python script for adding a new wiki redirect
"""
import re
import json
import os
from urllib.parse import urlparse
import unicodedata
import requests
from requests.exceptions import HTTPError, SSLError
from io import BytesIO
from typing import Optional, Iterable
from PIL import Image

from scrapewiki import normalize_url_protocol, get_mediawiki_api_url, get_favicon_url, determine_wiki_software, \
    request_with_http_fallback, query_mediawiki_api, extract_hostname, MediaWikiAPIError


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
    wikifarm = detect_wikifarm([base_url, logo_path])

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
        "platform": "mediawiki",  # This data necessarily comes from the MediaWiki API
    }
    return site_properties


def extract_topic_from_url(wiki_url: str) -> str:
    # Retrieve the topic
    normalized_url = normalize_url_protocol(wiki_url)
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


def detect_wikifarm(url_list: Iterable[str]) -> Optional[str]:
    # If the site URL or logo URL contains the name of a wikifarm, assume the wiki is hosted on that wikifarm
    # Checking the logo URL should catch any wikis hosted on a wikifarm that use a custom URL

    # This is only relevant for destinations, so "fandom" is not checked for (and it would likely give false positives)
    known_wikifarms = {"shoutwiki", "wiki.gg", "miraheze", "wikitide"}

    for wikifarm in known_wikifarms:
        for url in url_list:
            if wikifarm in url:
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
        entry_id = generate_entry_id(language, origin_entry["origin_base_url"])

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
        icon_file_response = requests.get(normalize_url_protocol(icon_url), headers=headers)
    except ConnectionError:
        icon_file_response = None

    if not icon_file_response:
        return None

    # Determine filepath
    icon_filename = generate_icon_filename(wiki_name)
    icon_folderpath = os.path.join(iwb_filepath, "favicons", language_code)
    if not os.path.isdir(icon_folderpath):  # If the folder doesn't already exist, create it
        os.mkdir(icon_folderpath)
        print(f"✏ Created new {language_code} icon folder")
    icon_filepath = os.path.join(icon_folderpath, icon_filename)

    # Write to file
    image_file = Image.open(BytesIO(icon_file_response.content))
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
    # If there is not currently a sites JSON for this language, start from an empty list
    if not os.path.isfile(sites_json_filepath):
        return []

    with open(sites_json_filepath, "r", encoding='utf-8') as sites_json_file:
        return json.load(sites_json_file)


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
        raise OSError('Cannot find the "data" folder. Ensure that you specified the correct "iwb_filepath".')

    # Get origin API URL
    try:
        origin_api_url = get_mediawiki_api_url(origin_wiki_url, headers=headers)
    except (HTTPError, ConnectionError, SSLError) as e:
        print(e)
        return None

    if origin_api_url is None:
        print(f"⚠ Unable to determine API URL for {origin_wiki_url}")
        return None

    # Get destination API URL
    try:
        destination_api_url = get_mediawiki_api_url(destination_wiki_url, headers=headers)
    except (HTTPError, ConnectionError, SSLError) as e:
        print(e)
        return None

    if destination_api_url is None:
        print(f"⚠ Unable to determine API URL for {destination_wiki_url}")
        return None

    # Request siteinfo data
    siteinfo_params = {'action': 'query', 'meta': 'siteinfo', 'siprop': 'general', 'format': 'json'}
    try:
        origin_siteinfo = query_mediawiki_api(origin_api_url, params=siteinfo_params, headers=headers)
        destination_siteinfo = query_mediawiki_api(destination_api_url, params=siteinfo_params, headers=headers)
    except (HTTPError, ConnectionError, SSLError, MediaWikiAPIError) as e:
        print(e)
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


def confirm_yes_no(caption: str) -> bool:
    no_values = {"", "n", "no"}
    yes_values = {"y", "yes"}

    user_input = input(caption).lower().strip()
    while user_input not in (yes_values | no_values):
        print("⚠ Unrecognized input. Please enter 'Y' or 'N' (blank counts as 'N').")
        user_input = input(caption).lower().strip()

    if user_input in yes_values:
        return True
    else:
        return False


def get_wiki_metadata_cli(site_class: str, key_properties: Iterable, headers: Optional[dict] = None):
    """
    CLI for preparing the data for a new origin or destination site.
    Retrieves the base_url, as well as any properties specified in key_properties.

    :param site_class: Whether the site is the origin or destination site
    :param key_properties: The properties that need to be collected for this site
    :param headers: Headers to use for HTTP requests (e.g. user-agent)
    :return: Dict of the required properties
    """

    # Resolve input URL
    wiki_url = ""
    while wiki_url.strip() == "":
        wiki_url = input(f"📥 Enter {site_class} wiki URL: ")
    response = request_with_http_fallback(wiki_url, headers=headers)

    wiki_data = {}
    auto_properties = []  # Properties that have been added automatically (i.e. that the user may want to edit)

    if not response:
        print(f"⚠ Unable to connect to {wiki_url} . Details will need to be entered manually.")
    else:
        # Check wiki software
        wiki_software = determine_wiki_software(response)

        # For MediaWiki wikis, retrieve details via the API
        if wiki_software == "mediawiki":
            print(f"🕑 Getting {site_class} site info...")
            api_url = get_mediawiki_api_url(response)
            if api_url is None:
                print(f"⚠ Unable to automatically retrieve API URL for {wiki_url}.")
                api_url = input(f"📥 Enter {site_class} wiki API URL: ")
                print(f"🕑 Getting {site_class} site info...")

            siteinfo_params = {'action': 'query', 'meta': 'siteinfo', 'siprop': 'general', 'format': 'json'}
            siteinfo = query_mediawiki_api(api_url, params=siteinfo_params, headers=headers)
            wiki_data = extract_site_metadata_from_siteinfo(siteinfo)

        else:
            print(f"⚠ {wiki_url} uses currently unsupported software. Details will need to be entered manually.")

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
