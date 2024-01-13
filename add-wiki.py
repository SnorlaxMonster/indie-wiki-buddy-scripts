##########################################################################################################
# This Python3 script guides you to quickly generate a JSON object for adding a wiki to Indie Wiki Buddy.
# It will also automatically download the destination wiki's favicon,
# convert it to PNG, resize to 16px, and add to the /favicons directory.

# When running, this script should be in the Indie Wiki Buddy project root folder.
##########################################################################################################

import os
import time
import json
import re
import requests
from urllib.request import Request, urlopen
from urllib.parse import urlparse
from PIL import Image
from bs4 import BeautifulSoup

# Get data from user input:
lang = input("📥 Enter language as two letter code (leave blank for 'en'): ") or "en"
id = input("📥 Enter entry ID (series name, one word, no dashes e.g. animalcrossing): ")
id = id.lower()
origin_name = input("📥 Enter origin wiki name (leave blank for '<id.capitalize()> Fandom Wiki'): " ) or id.capitalize() + " Fandom Wiki"
origin_link = input("📥 Enter origin wiki link (leave blank for '<id>.fandom.com'): ") or id + ".fandom.com"

# Pull main page title:
print("👷 Getting origin path info...")
origin_request = requests.get(url='https://' + origin_link, headers={'User-Agent': 'Mozilla/5.0'})
origin_final_link = origin_request.url
origin_main_page = urlparse(origin_final_link).path.split('/')[-1]
print("ℹ️ Found origin main page: " + origin_main_page)
origin_content_path = urlparse(origin_final_link).path.replace(origin_main_page, '')
print("ℹ️ Found origin content path: " + origin_content_path)

destination_name = input("📥 Enter destination wiki name: ")
destination_link = input("📥 Enter destination wiki link: ")

# Pull main page title:
print("👷 Getting destination main page...")
destination_request = requests.get(url='https://' + destination_link, headers={'User-Agent': 'Mozilla/5.0'})
destination_final_link = destination_request.url
destination_main_page = urlparse(destination_final_link).path.split('/')[-1]
print("ℹ️ Found destination main page: " + destination_main_page)

destination_search_path = input("📥 Enter destination wiki search path: ")
destination_platform = input("📥 Enter destination wiki platform (leave blank for 'mediawiki'): ") or "mediawiki"

# Output JSON:
data = {
  "id": lang + "-" + id,
  "origins_label": origin_name,
  "origins": [
    {
      "origin": origin_name,
      "origin_base_url": origin_link,
      "origin_content_path": origin_content_path,
      "origin_main_page": origin_main_page
    }
  ],
  "destination": destination_name,
  "destination_base_url": destination_link,
  "destination_platform": destination_platform,
  "destination_icon": re.sub('[^A-Za-z0-9]+', '', destination_name).lower().replace('wikigg', 'wiki') + ".png",
  "destination_main_page": destination_main_page,
  "destination_search_path": destination_search_path
}

if ('wiki.gg' in destination_name):
  data['tags'] = ['wiki.gg']

print("🗒️ Generated the following data:")
print("")
print(json.dumps(data, indent=2))
print("")

print("🕑 Saving data to sites" + lang.upper() + ".json...")

# Add site to data file:
data_filename = 'data/sites' + lang.upper() + '.json'
with open(data_filename, 'r') as file:
  wiki_data = json.load(file)

wiki_data.append(data)
wiki_data.sort(key=lambda obj: obj['id'])

# Write the updated data back to the JSON file
with open(data_filename, 'w', encoding='utf8') as file:
  json.dump(wiki_data, file, indent=2, ensure_ascii=False)

print("💾 Data saved and sorted in sites" + lang.upper() + ".json!")
print("🕑 Now grabbing wiki's favicon...")

# Pull favicon from destination wiki:
page = urlopen(Request(url="https://" + destination_link, headers={'User-Agent': 'Mozilla/5.0'}))
soup = BeautifulSoup(page, "html.parser")
icon_link = soup.find("link", rel="shortcut icon")
if(icon_link is None):
  icon_link = soup.find("link", rel="icon")
print(icon_link)
print(requests.compat.urljoin("https://" + destination_link, icon_link['href']))
icon = urlopen(Request(url=requests.compat.urljoin("https://" + destination_link, icon_link['href']), headers={'User-Agent': 'Mozilla/5.0'}))
temp_icon_filename = os.path.join("favicons/" + lang + "/temp_icon")
icon_filename = os.path.join("favicons/" + lang + "/" + re.sub('[^A-Za-z0-9]+', '', destination_name).lower().replace('wikigg', 'wiki'))
with open(temp_icon_filename, "wb+") as f:
  f.write(icon.read())

# Convert favicon to PNG, resize to 16px, save, and delete the original file:
time.sleep(1)
Image.open(temp_icon_filename).resize((16, 16)).save(icon_filename + ".png")
os.remove(temp_icon_filename)
print("🖼️ Favicon saved!")
print("✅ All done!")
