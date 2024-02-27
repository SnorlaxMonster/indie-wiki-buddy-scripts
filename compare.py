import pandas as pd

from utils import normalize_url_protocol, read_user_config, DEFAULT_TIMEOUT
from profilewiki import profile_wiki

headers = {'User-Agent': read_user_config("User-Agent")}
timeout = DEFAULT_TIMEOUT

# Ask how many wikis to compare
wiki_count_input = "null"
while not (wiki_count_input.isnumeric() or wiki_count_input.strip() == ""):
    wiki_count_input = input("How many wikis do you want to compare (default: 2)?: ")
if wiki_count_input.strip() == "":
    wiki_count_input = 2

# Take URLs as input
input_wiki_urls = []
for wiki_index in range(int(wiki_count_input)):
    wiki_url = ""
    while wiki_url.strip() == "":
        wiki_url = input(f"📥 Enter wiki URL {wiki_index+1}: ").strip()
    wiki_url = normalize_url_protocol(wiki_url)
    input_wiki_urls.append(wiki_url)

# Profile each wiki
all_profiles = []
for wiki_url in input_wiki_urls:
    print(f"🕑 Profiling {wiki_url}")
    wiki_profile = profile_wiki(wiki_url, headers=headers, timeout=DEFAULT_TIMEOUT)
    all_profiles.append(wiki_profile)

# Create output DataFrame
df = pd.DataFrame(all_profiles)

output_df = df[["name", "content_pages", "active_users", "recent_edit_count"]]
output_df = output_df.rename(columns={"content_pages": "Content pages", "active_users": "Active users",
                                      "recent_edit_count": "Recent edits (last 30 days)"})
output_df = output_df.set_index("name")
output_df = output_df.transpose()
output_df.columns.name = None
output_df.index.name = "Metric"

# Print Markdown table
print(output_df.to_markdown())
