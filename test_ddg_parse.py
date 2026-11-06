import urllib.request
import urllib.parse
import re

def search_ddg_instagram(query):
    encoded = urllib.parse.quote(f"site:instagram.com {query}")
    url = f"https://html.duckduckgo.com/html/?q={encoded}"
    req = urllib.request.Request(
        url,
        headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9'
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read().decode('utf-8', errors='ignore')
            # Extract instagram handles from links
            handles = re.findall(r'instagram\.com/([a-zA-Z0-9_\.]+)', html)
            unique_handles = []
            excluded = {'p', 'explore', 'reels', 'stories', 'direct', 'accounts', 'developer', 'about', 'privacy', 'legal'}
            for h in handles:
                h_clean = h.strip('/').lower()
                if h_clean not in excluded and len(h_clean) > 2:
                    if f"@{h_clean}" not in unique_handles:
                        unique_handles.append(f"@{h_clean}")
            return unique_handles
    except Exception as e:
        return [str(e)]

print("Found handles:", search_ddg_instagram("dubai watch dealer"))
