"""
Python script for refreshing the properties of existing redirects
"""
import json
import os
import warnings

from requests.exceptions import RequestException
from profilewiki import profile_wiki, MediaWikiAPIError, UnsupportedWikiSoftwareError
from utils import (read_user_config, get_iwb_filepath, confirm_yes_no, download_wiki_icon,
                   DEFAULT_TIMEOUT, DESTINATION_ENTRY_PROPERTIES, ORIGIN_ENTRY_PROPERTIES)


def get_wiki_url_from_entry(json_entry: dict, site_class: str) -> str:
    base_url = json_entry[site_class + "_base_url"]

    # Use the content path if defined
    content_path = json_entry.get(site_class + "_content_path")
    if content_path is not None:
        return "https://" + base_url + content_path

    # Use the search_path if defined as a fallback (which is the script path for MediaWiki and DokuWiki)
    search_path = json_entry.get(site_class + "_search_path")
    if search_path is not None:
        return "https://" + base_url + search_path

    # Otherwise, just use the base_url
    return "https://" + base_url


def refresh_site_entry(entry: dict, site_class: str, update_names: bool = True, update_icons: bool = False,
                       iwb_filepath: str | os.PathLike = ".", **kwargs):
    """
    Updates a site in a redirect entry based on the site's current status.

    :param entry: Dict containing a destination or origin entry from a sites JSON file
    :param site_class: Whether the entry is the origin or destination
    :param update_names: Whether to update the names of wikis
    :param update_icons: Whether to update the icons of wikis
    :param iwb_filepath: Filepath to IWB code, so that icons can be saved there
    :param kwargs: kwargs to use for the HTTP requests
    """

    wiki_name = entry.get(site_class)
    wiki_url = get_wiki_url_from_entry(entry, site_class)

    # Request metadata from wiki
    print(f"🕑 Refreshing {site_class} wiki [{wiki_name}] using URL {wiki_url}")
    try:
        site_metadata = profile_wiki(wiki_url, full_profile=False, **kwargs)
    except (RequestException, MediaWikiAPIError) as e:
        warnings.warn(f"Unable to refresh [{wiki_name}] using URL {wiki_url} due to:\n{e}")
        return
    except UnsupportedWikiSoftwareError:
        warnings.warn(f"Unable to refresh [{wiki_name}] using URL {wiki_url} due to unsupported wiki software")
        return

    # To ensure tags are always placed at the end, remove them temporarily, then reinsert them later
    tags = entry.get("tags")
    if tags is not None:
        entry.pop("tags")

    # Update the site's name
    if update_names:
        entry[site_class] = site_metadata.get("name")

    # Update the site's properties
    if site_class == "destination":
        property_list = DESTINATION_ENTRY_PROPERTIES
    else:
        property_list = ORIGIN_ENTRY_PROPERTIES

    icon_filename = entry.get(site_class + "_" + "icon")
    for prop in property_list:
        entry[site_class + "_" + prop] = site_metadata.get(prop)

    # Update the icon property
    if "icon" in property_list:
        # Preserve the pre-existing icon filename
        entry[site_class + "_" + "icon"] = icon_filename

        # If enabled, re-download the favicon
        icon_url = site_metadata.get("icon_path")
        if update_icons and icon_url is not None:
            downloaded_filename = download_wiki_icon(icon_url, icon_filename, site_metadata["language"],
                                                     iwb_filepath=iwb_filepath, **kwargs)
            if downloaded_filename is None:
                print(f"Failed to download icon for [{wiki_name}] using URL {icon_url}")

    # Record protocol if it is not HTTPS
    if site_metadata.get("protocol") != "https":
        entry[site_class + "_" + "protocol"] = site_metadata.get("protocol")

    # Update tags
    if site_class == "destination":
        # If the site is a wikifarm, ensure it is tagged as such
        wikifarm = site_metadata.get("wikifarm")
        if wikifarm is not None:
            if tags is None:
                tags = [wikifarm]
            elif wikifarm not in tags:
                tags.append(wikifarm)

        # Reinsert tags, if any
        if tags is not None:
            entry["tags"] = tags


def refresh_sites_json(sites_json_path: str | os.PathLike, update_names: bool = True, update_icons: bool = False,
                       iwb_filepath: str | os.PathLike = ".", **kwargs):
    """
    Updates a specified sites JSON file based on the current status of the sites it lists.

    :param sites_json_path: Path to the sites JSON file
    :param update_names: Whether to update the names of wikis
    :param update_icons: Whether to update the icons of wikis
    :param iwb_filepath: Filepath to IWB code, so that icons can be saved there
    :param kwargs: kwargs to use for the HTTP requests
    """
    # Read file
    with open(sites_json_path, "r", encoding="utf-8") as sites_json_file:
        sites_json = json.load(sites_json_file)

    # Refresh redirects
    for redirect_entry in sites_json:
        # Refresh destination
        refresh_site_entry(redirect_entry, "destination", update_names=update_names,
                           update_icons=update_icons, iwb_filepath=iwb_filepath, **kwargs)

        # Refresh origin
        for origin_entry in redirect_entry["origins"]:
            refresh_site_entry(origin_entry, "origin", update_names=update_names, **kwargs)

    # Write file
    with open(sites_json_path, "w", encoding="utf-8") as sites_json_file:
        json.dump(sites_json, sites_json_file, indent=2, ensure_ascii=False)


def refresh_all_redirects(iwb_filepath: str | os.PathLike = ".", update_names: bool = True, update_icons: bool = False,
                          **kwargs):
    """
    Updates a specified sites JSON file based on the current status of the sites it lists.

    :param iwb_filepath: Filepath to IWB code, if it differs from the directory the script is being run from.
    :param update_names: Whether to update the names of wikis
    :param update_icons: Whether to update the icons of wikis
    :param kwargs: kwargs to use for the HTTP requests
    """
    # Construct list of sites data JSON files
    sites_data_path = os.path.join(iwb_filepath, "data")
    if not os.path.isdir(sites_data_path):
        raise OSError('Cannot find the "data" folder. Ensure that you specified the correct "iwb_filepath".')

    sites_data_list = os.listdir(sites_data_path)
    sites_data_list.sort()

    # Refresh all sites data JSON files
    for sites_json_filename in sites_data_list:
        sites_json_path = os.path.join(sites_data_path, sites_json_filename)
        refresh_sites_json(sites_json_path, update_names=update_names, update_icons=update_icons,
                           iwb_filepath=iwb_filepath, **kwargs)


def main():
    # Prepare user-agent
    headers = {'User-Agent': read_user_config("User-Agent")}  # case-sensitive key, unlike the HTTP header

    # Get IWB filepath
    iwb_filepath = get_iwb_filepath()

    # Check scope to refresh
    sites_json_filename = input("❔ Enter file to refresh (leave blank to refresh all files): ")
    sites_json_filename = sites_json_filename.strip()

    # Check what should be updated
    update_names = confirm_yes_no(f"❔ Update wiki names (Y/N)?: ")
    update_icons = confirm_yes_no(f"❔ Update icons (Y/N)?: ")

    # Refresh all redirects
    if sites_json_filename == "":
        refresh_all_redirects(iwb_filepath, update_names=update_names, update_icons=update_icons, headers=headers,
                              timeout=DEFAULT_TIMEOUT)
        print(f"💾 Updated all sites JSON files!")
    else:
        sites_json_path = os.path.join(iwb_filepath, "data", sites_json_filename)
        refresh_sites_json(sites_json_path, update_names=update_names, update_icons=update_icons, headers=headers,
                           timeout=DEFAULT_TIMEOUT, iwb_filepath=iwb_filepath)
        print(f"💾 Updated {sites_json_path}!")


if __name__ == '__main__':
    main()
