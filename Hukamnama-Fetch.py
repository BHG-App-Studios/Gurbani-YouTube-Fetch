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
CHANNEL_ID = "UCD5a5KBLu4t7uZdoJQzEppg"
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

MIN_DURATION_SECONDS = 180  # ⏱️ 3 minutes

# Env variables for BOTH service accounts & YouTube API
SERVICE_ACCOUNT_GURBANI = os.environ.get("FIREBASE_SERVICE_ACCOUNT_GURBANI")
SERVICE_ACCOUNT_HARMANDIR = os.environ.get("FIREBASE_SERVICE_ACCOUNT_HARMANDIR")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")

if not SERVICE_ACCOUNT_GURBANI or not SERVICE_ACCOUNT_HARMANDIR:
    print("❌ FIREBASE_SERVICE_ACCOUNT env vars missing for one or both apps")
    sys.exit(1)

if not YOUTUBE_API_KEY:
    print("❌ YOUTUBE_API_KEY env var missing")
    sys.exit(1)

# Collection Names
COLLECTION_GURBANI = "liveStreams"
COLLECTION_HARMANDIR = "Live-Gurdwaras-YouTube"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015"
}

# ---------------- FIREBASE DUAL INIT ----------------
print("🔌 Initializing Firebase Connections...")

# App 1: Gurbani
cred_gurbani = credentials.Certificate(json.loads(SERVICE_ACCOUNT_GURBANI))
app_gurbani = firebase_admin.initialize_app(cred_gurbani, name='gurbani_app')
db_gurbani = firestore.client(app=app_gurbani)

# App 2: Harmandir Sahib
cred_harmandir = credentials.Certificate(json.loads(SERVICE_ACCOUNT_HARMANDIR))
app_harmandir = firebase_admin.initialize_app(cred_harmandir, name='harmandir_app')
db_harmandir = firestore.client(app=app_harmandir)


# ---------------- HELPER: SCRAPE CHANNEL LOGO ----------------
def fetch_channel_logo(channel_id):
    channel_url = f"https://www.youtube.com/channel/{channel_id}"
    print(f"🖼️ Scraping Logo from: {channel_url}...")
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9'
    }
    
    try:
        response = requests.get(channel_url, headers=headers, timeout=15)
        response.raise_for_status()
        
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


# ---------------- HELPER FUNCTIONS: DURATION & IMAGES ----------------
def chunk_list(data, chunk_size):
    for i in range(0, len(data), chunk_size):
        yield data[i:i + chunk_size]

def iso8601_to_seconds(duration):
    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration)
    if not match: return 0
    h = int(match.group(1) or 0)
    m = int(match.group(2) or 0)
    s = int(match.group(3) or 0)
    return h * 3600 + m * 60 + s

def parse_duration(pt_str):
    """Converts YouTube 'PT2M55S' format to '02:55' string for database"""
    if not pt_str or pt_str == "P0D": return "00:00"
    match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', pt_str)
    if not match: return "00:00"
    h, m, s = [int(x) if x else 0 for x in match.groups()]
    if h > 0: return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

def get_working_image_url(video_id):
    maxres_url = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
    fallback_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault_live.jpg"
    
    try:
        response = requests.head(maxres_url, timeout=5)
        if response.status_code == 200:
            return maxres_url
    except Exception:
        pass 
        
    return fallback_url


# ---------------- API HELPER: FETCH FULL VIDEO DETAILS ----------------
def fetch_video_details_batch(video_ids):
    """Fetches details (snippet, stats, duration) for all candidate IDs in batch"""
    yt_data_map = {}
    CHUNK_SIZE = 50 
    for chunk in chunk_list(video_ids, CHUNK_SIZE):
        url = "https://www.googleapis.com/youtube/v3/videos"
        params = {
            "part": "snippet,contentDetails,statistics,liveStreamingDetails",
            "id": ",".join(chunk),
            "key": YOUTUBE_API_KEY,
            "maxResults": 50
        }
        try:
            r = requests.get(url, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
            for item in data.get("items", []):
                yt_data_map[item["id"]] = item
        except Exception as e:
            print(f"⚠️ Error fetching details: {e}")
    return yt_data_map


# ---------------- MAIN LOGIC: RSS FETCH & FILTER ----------------
def fetch_latest_hukamnama_katha():
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
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

        # ✅ ORIGINAL LOGIC FILTER: Today Hukamnama Sri Darbar Sahib ONLY
        if "Today Hukamnama Sri Darbar Sahib" not in title:
            continue

        published = datetime.fromisoformat(
            published_el.text.replace("Z", "+00:00")
        ).astimezone(timezone.utc)

        matches.append({
            "video_id": video_id_el.text.strip(),
            "title": title,
            "published": published
        })

    if not matches:
        return None

    # Sort matches by latest date first
    matches.sort(key=lambda x: x["published"], reverse=True)

    # Fetch full details for all matched candidate IDs in batch
    candidate_ids = [m["video_id"] for m in matches]
    yt_data_map = fetch_video_details_batch(candidate_ids)

    # Find the latest video that is longer than the MIN_DURATION
    latest = None
    latest_yt_data = None
    
    for match in matches:
        vid = match["video_id"]
        yt_item = yt_data_map.get(vid)
        if not yt_item: continue
        
        iso = yt_item["contentDetails"].get("duration", "")
        duration_sec = iso8601_to_seconds(iso)
        
        if duration_sec >= MIN_DURATION_SECONDS:
            latest = match
            latest_yt_data = yt_item
            print(f"✅ Found latest qualifying video (Duration: {duration_sec}s): {vid}")
            break
        else:
            print(f"⏭️ Skipping video (Duration: {duration_sec}s < 180s): {vid}")

    if not latest or not latest_yt_data:
        print("❌ No matching videos found that meet the 3-minute minimum length requirement.")
        return None

    # --- BUILD FINAL PAYLOAD WITH ALL METADATA ---
    snippet = latest_yt_data.get("snippet", {})
    stats = latest_yt_data.get("statistics", {})
    content = latest_yt_data.get("contentDetails", {})
    
    is_live = snippet.get("liveBroadcastContent") == "live"
    view_count = int(stats.get("viewCount", 0))
    duration_str = "00:00" if is_live else parse_duration(content.get("duration", ""))
    
    # Timestamps in milliseconds 
    published_time_ms = str(int(latest["published"].timestamp() * 1000))
    current_time_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    
    # Fetch Logo via Scraper
    fetched_channel_id = snippet.get("channelId", CHANNEL_ID)
    logo_url = fetch_channel_logo(fetched_channel_id)

    return {
        "channelLogoUrl": logo_url,
        "channelName": snippet.get("channelTitle", ""),
        "channel_id": fetched_channel_id,
        "duration": duration_str,
        "imageUrl": get_working_image_url(latest['video_id']),
        "isLive": is_live,
        "timeAgo": published_time_ms,
        "timestamp": current_time_ms,
        "title": snippet.get("title", latest["title"]),
        "url": f"https://www.youtube.com/watch?v={latest['video_id']}",
        "viewCount": view_count
    }


# ---------------- FIRESTORE UPDATE DUAL DATABASES ----------------
def update_firestore_dual(data):
    print("\n📝 Updating Databases...")
    
    # --- Prepare Data for Gurbani App (ALL FIELDS) ---
    gurbani_update_payload = data.copy()

    # --- Prepare Data for Harmandir Sahib App (ALL FIELDS + lowercase title) ---
    harmandir_update_payload = data.copy()
    harmandir_update_payload["titleLowercase"] = data["title"].lower()

    # helper function to execute update
    def do_update(db_client, collection_name, app_name, payload):
        docs = (
            db_client.collection(collection_name)
            # ❗ ORIGINAL LOGIC: Query by 'hukamnama' field matching channel ID
            .where(filter=FieldFilter("hukamnama", "==", CHANNEL_ID))
            .limit(1)
            .get()
        )

        if not docs:
            print(f"❌ No document found for channel ID in {app_name} (Collection: {collection_name})")
            return

        doc = docs[0]
        existing = doc.to_dict()

        # 🔒 CHANGE-DETECTION (Kept exactly from original logic)
        if existing.get("url") == payload["url"]:
            print(f"⏭ No change in URL detected for {app_name}. Skipping update.")
            return

        # ✅ UPDATE ONLY IF CHANGED
        doc.reference.update(payload)
        print(f"✅ {app_name} updated successfully with full metadata")

    # Run for Gurbani
    do_update(db_gurbani, COLLECTION_GURBANI, "Gurbani App", gurbani_update_payload)
    
    # Run for Harmandir Sahib
    do_update(db_harmandir, COLLECTION_HARMANDIR, "Harmandir App", harmandir_update_payload)


# ---------------- MAIN ----------------
if __name__ == "__main__":
    result = fetch_latest_hukamnama_katha()

    if not result:
        print("❌ No Today Hukamnama Sri Darbar Sahib video found")
    else:
        print("\n🎯 Final Payload to Save:")
        print(json.dumps(result, indent=2))
        update_firestore_dual(result)
