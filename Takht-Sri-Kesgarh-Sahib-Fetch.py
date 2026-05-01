import os
import sys
import json
import re
import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1 import FieldFilter

# ---------------- CONFIG ----------------
CHANNEL_ID = "UCSx5035_us8h8DOp_YhQDaw"
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

# Env variables
SERVICE_ACCOUNT_GURBANI = os.environ.get("FIREBASE_SERVICE_ACCOUNT_GURBANI")
SERVICE_ACCOUNT_HARMANDIR = os.environ.get("FIREBASE_SERVICE_ACCOUNT_HARMANDIR")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")

if not SERVICE_ACCOUNT_GURBANI or not SERVICE_ACCOUNT_HARMANDIR:
    print("❌ FIREBASE_SERVICE_ACCOUNT env vars missing for one or both apps")
    sys.exit(1)

if not YOUTUBE_API_KEY:
    print("❌ YOUTUBE_API_KEY env var missing")
    sys.exit(1)

COLLECTION_GURBANI = "liveStreams"
COLLECTION_HARMANDIR = "Live-Gurdwaras-YouTube"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015"
}

# ---------------- FIREBASE DUAL INIT ----------------
print("🔌 Initializing Firebase Connections...")
cred_gurbani = credentials.Certificate(json.loads(SERVICE_ACCOUNT_GURBANI))
app_gurbani = firebase_admin.initialize_app(cred_gurbani, name='gurbani_app')
db_gurbani = firestore.client(app=app_gurbani)

cred_harmandir = credentials.Certificate(json.loads(SERVICE_ACCOUNT_HARMANDIR))
app_harmandir = firebase_admin.initialize_app(cred_harmandir, name='harmandir_app')
db_harmandir = firestore.client(app=app_harmandir)

# ---------------- 1. RSS FETCH ----------------
# ---------------- 1. RSS FETCH ----------------
def fetch_latest_5_matching():
    # 🚨 ADD THIS: YouTube को यह न लगे कि कोई बॉट है!
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    # Pass the headers into the request
    response = requests.get(RSS_URL, headers=headers, timeout=15)
    response.raise_for_status()
    
    root = ET.fromstring(response.text)
    matches = []

    for entry in root.findall("atom:entry", NS):
        title_el = entry.find("atom:title", NS)
        video_id_el = entry.find("yt:videoId", NS)
        published_el = entry.find("atom:published", NS)

        if title_el is None or video_id_el is None or published_el is None:
            continue

        title = title_el.text.strip()
        if "Official SGPC LIVE" not in title:
            continue

        published = datetime.fromisoformat(published_el.text.replace("Z", "+00:00")).astimezone(timezone.utc)
        matches.append({
            "video_id": video_id_el.text.strip(),
            "title": title,
            "published": published
        })

    if not matches: return []
    matches.sort(key=lambda x: x["published"], reverse=True)
    return matches[:5]

# ---------------- 2. YOUTUBE API DETAILS ----------------
def fetch_video_details(video_ids):
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "key": YOUTUBE_API_KEY,
        # Added contentDetails and statistics for duration and viewCount
        "part": "snippet,liveStreamingDetails,contentDetails,statistics",
        "id": ",".join(video_ids),
        "maxResults": 5
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json().get("items", [])

# ---------------- 3. SCRAPE CHANNEL LOGO (WITHOUT API) ----------------
def fetch_channel_logo(channel_id):
    channel_url = f"https://www.youtube.com/channel/{channel_id}"
    print(f"🖼️ Scraping Logo from: {channel_url}...")
    
    headers = {
        # YouTube को यह न लगे कि कोई बॉट है, इसलिए सामान्य ब्राउज़र हेडर भेजें
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9'
    }
    
    try:
        response = requests.get(channel_url, headers=headers, timeout=15)
        response.raise_for_status()
        
        # HTML पार्स करें (Node.js के Cheerio की तरह)
        soup = BeautifulSoup(response.text, 'html.parser')
        meta_image = soup.find('meta', property='og:image')
        
        if meta_image and meta_image.get('content'):
            print(f"✅ Logo found: {meta_image['content']}")
            return meta_image['content']
        else:
            print("❌ Logo meta tag not found.")
            return ""
    except Exception as e:
        print(f"❌ Error scraping logo: {e}")
        return ""

# ---------------- HELPER FUNCTIONS ----------------
def get_working_image_url(video_id):
    maxres_url = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
    fallback_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault_live.jpg"
    try:
        if requests.head(maxres_url, timeout=5).status_code == 200:
            return maxres_url
    except: pass
    return fallback_url

def parse_duration(pt_str):
    """Converts YouTube 'PT2M55S' format to '02:55'"""
    if not pt_str or pt_str == "P0D": return "00:00"
    match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', pt_str)
    if not match: return "00:00"
    h, m, s = [int(x) if x else 0 for x in match.groups()]
    if h > 0: return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

# ---------------- 4. BUILD FINAL DATA PAYLOAD ----------------
def build_video_payload(rss_videos, yt_videos):
    yt_map = {v["id"]: v for v in yt_videos}
    final_yt = None
    published_dt = None

    # Find the live video, or fallback to the newest
    for v in rss_videos:
        yt = yt_map.get(v["video_id"])
        if not yt: continue
        
        if final_yt is None or v["published"] > published_dt:
            published_dt = v["published"]
            final_yt = yt
            
        if yt["snippet"].get("liveBroadcastContent") == "live":
            final_yt = yt
            published_dt = v["published"]
            break

    if not final_yt: return None

    # Extract required fields
    snippet = final_yt.get("snippet", {})
    stats = final_yt.get("statistics", {})
    content = final_yt.get("contentDetails", {})
    
    is_live = snippet.get("liveBroadcastContent") == "live"
    
    # Process View Count (Ensure it's an INT)
    view_count = int(stats.get("viewCount", 0))
    
    # Process Duration
    duration_str = "00:00" if is_live else parse_duration(content.get("duration", ""))
    
    # Timestamps in milliseconds (Strings)
    published_time_ms = str(int(published_dt.timestamp() * 1000))

    # Fetch Logo via Scraper
    logo_url = fetch_channel_logo(snippet.get("channelId", CHANNEL_ID))

    return {
        "imageUrl": get_working_image_url(final_yt['id']), # String
        "isLive": is_live,                                 # Boolean
        "title": snippet.get("title", ""),                 # String
        "url": f"https://www.youtube.com/watch?v={final_yt['id']}", # String
        
        # --- NEW FIELDS ---
        "channelName": snippet.get("channelTitle", ""),    # String
        "channelLogoUrl": logo_url,                        # String
        "viewCount": view_count,                           # Int64 (Number)
        "timeAgo": published_time_ms,                      # String (Publish time)
        "duration": duration_str                           # String
    }

# ---------------- 5. UPDATE FIRESTORE ----------------
def update_firestore_dual(payload):
    print("\n📝 Updating Databases...")

    # Data for Harmandir app includes titleLowercase for its specific searching needs
    harmandir_payload = payload.copy()
    harmandir_payload["titleLowercase"] = payload["title"].lower()

    def do_update(db_client, collection_name, app_name, data):
        docs = (
            db_client.collection(collection_name)
            .where(filter=FieldFilter("channel_Id", "==", CHANNEL_ID)) # Assuming channel_Id is the linking key
            .limit(1)
            .get()
        )

        if not docs:
            print(f"❌ No document found for channel ID in {app_name}")
            return

        doc = docs[0]
        # .update() modifies only these exact fields without removing existing ones (like channel_Id)
        doc.reference.update(data)
        print(f"✅ {app_name} updated successfully with full data!")

    do_update(db_gurbani, COLLECTION_GURBANI, "Gurbani App", payload)
    do_update(db_harmandir, COLLECTION_HARMANDIR, "Harmandir App", harmandir_payload)

# ---------------- MAIN EXECUTION ----------------
if __name__ == "__main__":
    print("🔄 Fetching latest Official SGPC LIVE videos from RSS...")
    rss_videos = fetch_latest_5_matching()

    if not rss_videos:
        print("❌ No Official SGPC LIVE video found")
        sys.exit(0)

    video_ids = [v["video_id"] for v in rss_videos]

    print("📡 Fetching video details from YouTube API...")
    yt_videos = fetch_video_details(video_ids)

    print("⚙️ Building Payload and Scraping Logo...")
    final_payload = build_video_payload(rss_videos, yt_videos)

    if not final_payload:
        print("❌ Failed to build video payload")
        sys.exit(0)

    print("\n🎯 Final Payload to Save:")
    print(json.dumps(final_payload, indent=2))

    update_firestore_dual(final_payload)
