"""
Python script for adding a new language to IWB
"""
import babel
import json
import os
import re
from bs4 import BeautifulSoup

from utils import get_iwb_filepath


def update_languages(iwb_filepath):
    """
    Updates all parts of Indie Wiki Buddy that define which languages are supported, based on the list of languages
    defined in sites JSON files.

    :param iwb_filepath: Path to Indie Wiki Buddy repo
    :return: List of languages
    """
    # Get list of all sites JSON files
    sites_data_path = os.path.join(iwb_filepath, "data")
    sites_data_list = os.listdir(sites_data_path)
    sites_data_list.sort()

    # Get list of languages
    assert all([re.match(r"sites([A-Z]+)\.json", sites_data_json) for sites_data_json in sites_data_list])
    languages_list = [re.match(r"sites([A-Z]+)\.json", sites_data_json).group(1) for sites_data_json in sites_data_list]
    languages_list.sort()

    # Construct resource list for Manifests
    # NOTE: Do not use os.path.join to construct paths as it should always be /, even when run on Windows
    new_resource_list = ["favicons/*"]
    new_resource_list += [f"data/{fn}" for fn in os.listdir(sites_data_path)]

    # Update Chromium Manifest's resource list
    chromium_manifest_path = os.path.join(iwb_filepath, "manifest-chromium.json")
    with open(chromium_manifest_path, "r", encoding="utf-8") as chromium_manifest:
        chromium_manifest_content = json.load(chromium_manifest)
        web_accessible_resources = chromium_manifest_content.get("web_accessible_resources")
        for resource_entry in web_accessible_resources:
            if isinstance(resource_entry, dict) and resource_entry.get("resources") is not None:
                resource_entry["resources"] = new_resource_list
    with open(chromium_manifest_path, "w", encoding="utf-8") as chromium_manifest:
        json.dump(chromium_manifest_content, chromium_manifest, indent=2, ensure_ascii=False)
    print("✅ Updated", os.path.basename(chromium_manifest_path))

    # Update Firefox Manifest's resource list
    firefox_manifest_path = os.path.join(iwb_filepath, "manifest-firefox.json")
    with open(firefox_manifest_path, "w+", encoding="utf-8") as firefox_manifest:
        firefox_manifest_content = json.load(firefox_manifest)
        firefox_manifest_content["web_accessible_resources"] = new_resource_list
        json.dump(firefox_manifest_content, firefox_manifest, ensure_ascii=False)
    print("✅ Updated", os.path.basename(firefox_manifest_path))

    # Update README
    readme_path = os.path.join(iwb_filepath, "README.md")
    with open(readme_path, "r", encoding="utf-8") as readme_file:
        readme_content = readme_file.read()
    language_icon_list = [(f"![{lang_code.upper()} wikis](https://img.shields.io/badge/dynamic/json?style=flat-square"
                           f"&label={lang_code.upper()}%20wikis&query=length"
                           f"&url=https%3A%2F%2Fraw.githubusercontent.com%2FKevinPayravi%2Findie-wiki-buddy"
                           f"%2Fmain%2Fdata%2Fsites{lang_code}.json)")
                          for lang_code in languages_list]

    readme_content = re.sub(r"(?:\n!\[\w+ wikis\]\(.+\))*", '\n' + '\n'.join(language_icon_list), readme_content)
    with open(readme_path, "w", encoding="utf-8") as readme_file:
        readme_file.write(readme_content)
    print("✅ Updated", os.path.basename(readme_path))

    # Update LANGS var in common-functions.js
    common_functions_path = os.path.join(iwb_filepath, "scripts/common-functions.js")
    with open(common_functions_path, "r", encoding="utf-8") as common_functions_file:
        common_functions_content = common_functions_file.read()

    languages_list_js = repr(languages_list).replace("'", '"')
    common_functions_content = re.sub(r'var LANGS = .*;', f"var LANGS = {languages_list_js};", common_functions_content)
    with open(common_functions_path, "w", encoding="utf-8") as common_functions_file:
        common_functions_file.write(common_functions_content)
    print("✅ Updated", os.path.basename(common_functions_path))

    # Build new langSelect
    soup = BeautifulSoup()
    new_lang_select = soup.new_tag("select", id="langSelect")
    new_lang_select["name"] = "lang"

    all_languages_option = soup.new_tag("option", selected="", value="ALL")
    all_languages_option.string = "All languages"
    new_lang_select.append(all_languages_option)

    for lang in languages_list:
        # Get language names
        locale = babel.Locale(lang.lower())
        lang_name_local = locale.get_language_name()
        lang_name_en = locale.get_language_name("en")

        # Build new tag
        new_tag = soup.new_tag("option", value=lang)
        if lang_name_en == lang_name_local:
            new_tag.string = f"{lang_name_en} ({lang.upper()})"
        else:
            new_tag.string = f"{lang_name_en} / {lang_name_local} ({lang.upper()})"
        new_lang_select.append(new_tag)

    # Update settings file
    settings_index_path = os.path.join(iwb_filepath, "pages/settings/index.html")
    with open(settings_index_path, "r", encoding="utf-8") as file:
        raw_settings_html = file.read()
    # Use RegEx replacement to ensure that the rest of the HTML remains unmodified
    settings_regex = re.compile(r'<select(?: name="lang")? id="langSelect"(?: name="lang")?>.*?</select>', flags=re.DOTALL)
    raw_settings_html = re.sub(settings_regex, str(new_lang_select), raw_settings_html, count=1, flags=re.DOTALL)
    with open(settings_index_path, "w", encoding="utf-8") as file:
        file.write(raw_settings_html)
    print("✅ Updated", settings_index_path)  # Not using basename here, as the project has several files called index.html

    return languages_list


def add_language(new_language_code, iwb_filepath):
    new_language_code = new_language_code.upper()

    # Get list of currently supported languages
    sites_data_path = os.path.join(iwb_filepath, "data")
    assert all([re.match(r"sites([A-Z]+)\.json", sites_data_json) for sites_data_json in os.listdir(sites_data_path)])
    languages_list = [re.match(r"sites([A-Z]+)\.json", sites_data_json).group(1)
                      for sites_data_json in os.listdir(sites_data_path)]

    # If the language is already defined, do nothing
    if new_language_code in languages_list:
        return False

    # Create an empty sites JSON for the new language
    new_sites_json_filename = f"sites{new_language_code}.json"
    with open(os.path.join(sites_data_path, new_sites_json_filename), "x", encoding="utf-8") as new_sites_json:
        json.dump(list(), new_sites_json)
    print(f"✏ Created {new_sites_json_filename}")

    # Create a folder for icons for that language
    icon_folderpath = os.path.join(iwb_filepath, "favicons", new_language_code)
    if not os.path.isdir(icon_folderpath):
        os.mkdir(icon_folderpath)
        print(f"✏ Created {new_language_code} icon folder")

    # Update all other files that specify supported languages
    update_languages(iwb_filepath)


def main():
    iwb_path = get_iwb_filepath()
    update_languages(iwb_path)


if __name__ == '__main__':
    main()
