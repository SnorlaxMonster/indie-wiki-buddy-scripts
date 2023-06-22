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

def get_article_path(url):
  url_path = urlparse(url).path
  paths = ["index.php", "/wiki/", "/w/", "/"]

  for path in paths:
    if url_path.startswith(path):
      return path

  return "/"

# Get data from user input:
lang = input("📥 Enter language as two letter code (leave blank for 'en'): ") or "en"
id = input("📥 Enter entry ID (series name, one word, no dashes e.g. animalcrossing): ")
id = id.lower()
origin_name = input("📥 Enter origin wiki name (leave blank for '<id.capitalize()> Fandom Wiki'): " ) or id.capitalize() + " Fandom Wiki"
origin_link = input("📥 Enter origin wiki link (leave blank for '<id>.fandom.com'): ") or id + ".fandom.com"
origin_content_path = input("📥 Enter origin article path (leave blank for '/wiki/'): ") or "/wiki/"
destination_name = input("📥 Enter destination wiki name: ")
destination_link = input("📥 Enter destination wiki link: ")
print("🕑 Getting wiki's article path...")
if(not destination_link.startswith('http')):
  destination_link = "https://" + destination_link
destination_response = requests.head(destination_link, allow_redirects=True)
destination_url = destination_response.url
article_path = get_article_path(destination_url)
destination_content_path = input("📥 Detected article path " + article_path + ", you may keep this or overwrite: ") or article_path
destination_platform = input("📥 Enter destination wiki platform (leave blank for 'mediawiki'): ") or "mediawiki"

# Output JSON:
data = {
  "id": lang + "-" + id,
  "origins_label": origin_name,
  "origins": [
    {
      "origin": origin_name,
      "origin_base_url": origin_link,
      "origin_content_path": origin_content_path
    }
  ],
  "destination": destination_name,
  "destination_base_url": destination_link,
  "destination_content_path": destination_content_path,
  "destination_platform": destination_platform,
  "destination_icon": destination_name.lower() + ".png"
}
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
if(not destination_link.startswith('http')):
  destination_link = "https://" + destination_link
page = urlopen(Request(url=destination_link, headers={'User-Agent': 'Mozilla/5.0'}))
soup = BeautifulSoup(page, "html.parser")
icon_link = soup.find("link", rel="shortcut icon")
if(icon_link is None):
  icon_link = soup.find("link", rel="icon")
icon = urlopen(Request(url=requests.compat.urljoin(destination_link + "/favicon.ico", icon_link['href']), headers={'User-Agent': 'Mozilla/5.0'}))
icon_filename =  os.path.join("favicons\\" + lang + "\\" + re.sub('[^A-Za-z0-9]+', '', destination_name).lower() + icon_link['href'][-4:])
with open(icon_filename, "wb+") as f:
  f.write(icon.read())

# Convert favicon to PNG, resize to 16px, save, and delete the original file:
time.sleep(1)
Image.open(icon_filename).resize((16, 16)).save(icon_filename[0:icon_filename.find('.')] + ".png")
os.remove(icon_filename)
print("🖼️ Favicon saved!")
print("✅ All done!")

