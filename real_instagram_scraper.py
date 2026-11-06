import sys
import io
import re
import json
import time
import urllib.parse
import urllib.request

# Fix encoding for Windows console
if sys.platform.startswith('win'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
}

QUERY_TEMPLATES = [
    'site:instagram.com "yacht charter" dubai',
    'site:instagram.com "yacht rental" miami',
    'site:instagram.com "luxury watches" dubai',
    'site:instagram.com "luxury watches" miami',
    'site:instagram.com "real estate" dubai',
    'site:instagram.com "exotic car rental" miami',
    'site:instagram.com "car rental" dubai',
    'site:instagram.com "private jet charter" monaco'
]

def search_instagram_handles(query):
    print(f"Searching real profiles: {query}")
    encoded_query = urllib.parse.quote(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded_query}"
    
    req = urllib.request.Request(url, headers=HEADERS)
    found_handles = set()
    
    try:
        with urllib.request.urlopen(req) as response:
            html = response.read().decode('utf-8', errors='ignore')
            matches = re.findall(r'instagram\.com/([a-zA-Z0-9_\.]+)', html)
            for m in matches:
                handle = m.lower().strip()
                if handle not in ['p', 'reel', 'reels', 'explore', 'stories', 'tv', 'accounts', 'developer', 'about', 'legal', 'privacy']:
                    found_handles.add('@' + handle)
    except Exception as e:
        print(f"Error for {query}: {e}")
        
    return list(found_handles)

def main():
    print("Starting real Instagram parser...")
    all_real_handles = set()
    
    for q in QUERY_TEMPLATES:
        handles = search_instagram_handles(q)
        print(f"Found {len(handles)} handles: {handles[:5]}")
        all_real_handles.update(handles)
        time.sleep(1)
        
    print(f"\nTotal collected: {len(all_real_handles)} real unique profiles.")
    
    output_data = {
        "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_count": len(all_real_handles),
        "handles": sorted(list(all_real_handles))
    }
    
    with open("scraped_instagram_leads.json", "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
        
    print("Data saved to scraped_instagram_leads.json")

if __name__ == "__main__":
    main()
