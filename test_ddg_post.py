import urllib.request
import urllib.parse
import re

def search_ddg_post(query):
    url = "https://html.duckduckgo.com/html/"
    data = urllib.parse.urlencode({'q': f'site:instagram.com {query}'}).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Content-Type': 'application/x-www-form-urlencoded'
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read().decode('utf-8', errors='ignore')
            raw_handles = re.findall(r'instagram\.com/([a-zA-Z0-9_\.]+)', html)
            excluded = {'p', 'explore', 'reels', 'stories', 'direct', 'accounts', 'developer', 'about', 'privacy', 'legal', 'static', 'style'}
            found = []
            for h in raw_handles:
                h_clean = h.strip('/').lower()
                if h_clean not in excluded and len(h_clean) > 2 and not h_clean.isdigit():
                    if f"@{h_clean}" not in found:
                        found.append(f"@{h_clean}")
            return found
    except Exception as e:
        return [str(e)]

print("Found via POST:", search_ddg_post("dubai luxury watches"))
