import urllib.request
import re

url = "https://search.yahoo.com/search?p=site%3Ainstagram.com+dubai+watch"
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
try:
    with urllib.request.urlopen(req, timeout=10) as res:
        html = res.read().decode('utf-8', errors='ignore')
        matches = re.findall(r'instagram\.com/([a-zA-Z0-9_\.]+)', html)
        print("Yahoo search results:", list(set(matches[:15])))
except Exception as e:
    print("Error:", e)
