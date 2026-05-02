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
CHANNEL_ID = "UCudVHqnOekwcvpzNpY8_ERw"
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

SERVICE_ACCOUNT_GURBANI = os.environ.get("FIREBASE_SERVICE_ACCOUNT_GURBANI")
SERVICE_ACCOUNT_HARMANDIR = os.environ.get("FIREBASE_SERVICE_ACCOUNT_HARMANDIR")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")

if not SERVICE_ACCOUNT_GURBANI or not SERVICE_ACCOUNT_HARMANDIR:
    print("❌ FIREBASE_SERVICE_ACCOUNT env vars missing for one or both apps")
    sys.exit(1)

if not YOUTUBE_API_KEY:
    print("❌ YOUTUBE_API_KEY env var missing. Required for fetching duration.")
    sys.exit(1)

# Collection Names
COLLECTION_GURBANI = "liveStreams"
COLLECTION_HARMANDIR = "Live-Gurdwaras-YouTube"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/"
}

# ---------------- FIREBASE DUAL INIT ----------------
print("🔌 Initializing Firebase Connections...")

cred_gurbani = credentials.Certificate(json.loads(SERVICE_ACCOUNT_GURBANI))
app_gurbani = firebase_admin.initialize_app(cred_gurbani, name='gurbani_app')
db_gurbani = firestore.client(app=app_gurbani)

cred_harmandir = credentials.Certificate(json.loads(SERVICE_ACCOUNT_HARMANDIR))
app_harmandir = firebase_admin.initialize_app(cred_harmandir, name='harmandir_app')
db_harmandir = firestore.client(app=app_harmandir)


# ---------------- HELPERS ----------------
def fetch_channel_logo(channel_id):
    channel_url = f"https://www.youtube.com/channel/{channel_id}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    try:
        response = requests.get(channel_url, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        meta_image = soup.find('meta', property='og:image')
        if meta_image and meta_image.get('content'):
            return meta_image['content']
    except Exception as e:
        print(f"❌ Error scraping logo: {e}")
    return ""

def get_working_image_url(video_id):
    maxres_url = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
    fallback_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault_live.jpg"
    try:
        if requests.head(maxres_url, timeout=5).status_code == 200:
            return maxres_url
    except Exception:
        pass
    return fallback_url

def fetch_yt_api_duration(video_id):
    """Fetches video duration using YouTube Data API and converts ISO 8601 to MM:SS"""
    url = f"https://www.googleapis.com/youtube/v3/videos?id={video_id}&part=contentDetails&key={YOUTUBE_API_KEY}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        if not data.get("items"):
            return "0:00"
            
        duration_iso = data["items"][0]["contentDetails"]["duration"]
        
        # Regex to parse ISO 8601 duration (e.g., PT8M33S -> 8:33)
        match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_iso)
        if not match: 
            return "0:00"
            
        hours, minutes, seconds = match.groups()
        hours = int(hours) if hours else 0
        minutes = int(minutes) if minutes else 0
        seconds = int(seconds) if seconds else 0
        
        if hours > 0:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        else:
            return f"{minutes}:{seconds:02d}"
    except Exception as e:
        print(f"❌ Error fetching duration from YT API: {e}")
        return "0:00"


# ---------------- CORE LOGIC ----------------
def fetch_latest_rss_data():
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
        author_name_el = entry.find("atom:author/atom:name", NS)
        stats_el = entry.find("media:group/media:community/media:statistics", NS)

        if title_el is None or video_id_el is None or published_el is None:
            continue

        title = title_el.text.strip()

        # ✅ FILTER: Official SGPC LIVE | Katha Hukamnama Sahib ONLY
        if "Official SGPC LIVE | Katha Hukamnama Sahib" not in title:
            continue

        published = datetime.fromisoformat(
            published_el.text.replace("Z", "+00:00")
        ).astimezone(timezone.utc)

        view_count = int(stats_el.attrib.get("views", 0)) if stats_el is not None else 0
        channel_name = author_name_el.text if author_name_el is not None else ""

        matches.append({
            "video_id": video_id_el.text.strip(),
            "title": title,
            "published": published,
            "viewCount": view_count,
            "channelName": channel_name
        })

    if not matches:
        return None

    return max(matches, key=lambda x: x["published"])


def process_and_update_firestore():
    print("🔄 Fetching latest stream data from RSS (Zero API Quota for views)...")
    latest_rss = fetch_latest_rss_data()

    if not latest_rss:
        print("❌ No Official SGPC LIVE | Katha Hukamnama Sahib video found in RSS")
        return

    video_id = latest_rss["video_id"]
    new_url = f"https://www.youtube.com/watch?v={video_id}"

    # ---------------- 1. READ FIREBASE (Exactly 2 Reads) ----------------
    print("\n🔍 Reading existing data from Firestore...")
    
    gurbani_docs = db_gurbani.collection(COLLECTION_GURBANI).where(
        filter=FieldFilter("hukamnama_katha_fatehgarh_sahib", "==", CHANNEL_ID)
    ).limit(1).get()
    
    harmandir_docs = db_harmandir.collection(COLLECTION_HARMANDIR).where(
        filter=FieldFilter("hukamnama_katha_fatehgarh_sahib", "==", CHANNEL_ID)
    ).limit(1).get()

    gurbani_doc = gurbani_docs[0] if gurbani_docs else None
    harmandir_doc = harmandir_docs[0] if harmandir_docs else None

    existing_gurbani = gurbani_doc.to_dict() if gurbani_doc else {}
    existing_harmandir = harmandir_doc.to_dict() if harmandir_doc else {}


    # ---------------- 2. CONDITIONAL YT DATA API (Max 1 Call) ----------------
    existing_url = existing_gurbani.get("url")
    
    if existing_url == new_url and "duration" in existing_gurbani:
        print("⏭ URL matches database. Fetching duration from Firebase (Saved 1 YT API Call!).")
        duration = existing_gurbani["duration"]
    else:
        print("🆕 New URL detected! Fetching duration from YouTube Data API...")
        duration = fetch_yt_api_duration(video_id)


    # ---------------- 3. BUILD FINAL PAYLOAD ----------------
    time_ago_ms = str(int(latest_rss["published"].timestamp() * 1000))
    current_timestamp_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    logo_url = fetch_channel_logo(CHANNEL_ID)

    base_payload = {
        "channelLogoUrl": logo_url,
        "channelName": latest_rss["channelName"],
        "channel_id": CHANNEL_ID,
        "duration": duration,
        "hukamnama": CHANNEL_ID,
        "hukamnama_katha_fatehgarh_sahib": CHANNEL_ID,
        "imageUrl": get_working_image_url(video_id),
        "isLive": False,
        "timeAgo": time_ago_ms,
        "timestamp": current_timestamp_ms,
        "title": latest_rss["title"],
        "url": new_url,
        "viewCount": latest_rss["viewCount"]
    }

    print("\n🎯 Final Payload Assembled:")
    print(json.dumps(base_payload, indent=2))


    # ---------------- 4. CONDITIONAL WRITE FIREBASE (Max 2 Writes) ----------------
    def safe_update(doc_snapshot, payload, app_name):
        if not doc_snapshot:
            print(f"❌ Document missing in {app_name}, cannot update.")
            return

        existing = doc_snapshot.to_dict()
        
        # Check if an update is genuinely needed to save write costs
        if (existing.get("url") == payload["url"] and 
            existing.get("viewCount") == payload["viewCount"] and 
            existing.get("title") == payload["title"]):
            print(f"⏭ No data changed for {app_name} (Views still {payload['viewCount']}). Write skipped.")
            return

        doc_snapshot.reference.update(payload)
        print(f"✅ {app_name} updated successfully with {payload['viewCount']} views!")

    print("\n📝 Pushing updates to Databases...")
    
    # Gurbani Payload
    safe_update(gurbani_doc, base_payload, "Gurbani App")

    # Harmandir Payload (Requires titleLowercase)
    harmandir_payload = base_payload.copy()
    harmandir_payload["titleLowercase"] = base_payload["title"].lower()
    safe_update(harmandir_doc, harmandir_payload, "Harmandir App")


# ---------------- MAIN ----------------
if __name__ == "__main__":
    process_and_update_firestore()
