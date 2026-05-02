import os
import sys
import json
import re
import time
import random
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1 import FieldFilter

# ---------------- CONFIG ----------------
CHANNEL_ID = "UCudVHqnOekwcvpzNpY8_ERw"
TARGET_TITLE = "Official SGPC LIVE | Katha Hukamnama Sahib"
TARGET_DOC_ID = "hukamnama_katha_fatehgarh_sahib"

SERVICE_ACCOUNT_GURBANI = os.environ.get("FIREBASE_SERVICE_ACCOUNT_GURBANI")
SERVICE_ACCOUNT_HARMANDIR = os.environ.get("FIREBASE_SERVICE_ACCOUNT_HARMANDIR")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")

if not SERVICE_ACCOUNT_GURBANI or not SERVICE_ACCOUNT_HARMANDIR:
    print("❌ FIREBASE_SERVICE_ACCOUNT env vars missing for one or both apps")
    sys.exit(1)

if not YOUTUBE_API_KEY:
    print("❌ YOUTUBE_API_KEY env var missing.")
    sys.exit(1)

# Collection Names
COLLECTION_GURBANI = "Listen_Kirtans_Videos_New"
COLLECTION_HARMANDIR = "Live-Gurdwaras-YouTube"

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
    """Scrapes the channel HTML for the logo (Does not trigger API quota)."""
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
    """Checks if maxres is available, otherwise falls back to hqdefault."""
    maxres_url = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
    fallback_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault_live.jpg"
    try:
        if requests.head(maxres_url, timeout=5).status_code == 200:
            return maxres_url
    except Exception:
        pass
    return fallback_url

def parse_iso_duration(duration_iso):
    """Converts ISO 8601 duration (e.g., PT8M33S) to MM:SS format."""
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

# ---------------- CORE API LOGIC (2 QUOTA UNITS TOTAL) ----------------
def fetch_latest_api_data():
    # Convert Channel ID (UC...) to Uploads Playlist ID (UU...)
    playlist_id = "UU" + CHANNEL_ID[2:]
    
    # Unit 1: Fetch the 5 most recent uploads to find the specific stream
    playlist_url = f"https://www.googleapis.com/youtube/v3/playlistItems?part=snippet&playlistId={playlist_id}&maxResults=5&key={YOUTUBE_API_KEY}"
    
    try:
        resp1 = requests.get(playlist_url, timeout=10)
        resp1.raise_for_status()
        playlist_data = resp1.json()
        
        target_video_id = None
        published_at = None
        channel_name = None
        title = None
        
        for item in playlist_data.get("items", []):
            snippet = item["snippet"]
            # ✅ FILTER: Target Title ONLY
            if TARGET_TITLE in snippet["title"]:
                target_video_id = snippet["resourceId"]["videoId"]
                title = snippet["title"]
                published_at = snippet["publishedAt"]
                channel_name = snippet["channelTitle"]
                break
                
        if not target_video_id:
            return None
            
        # Unit 2: Fetch both Statistics (views) and ContentDetails (duration) in one call!
        video_url = f"https://www.googleapis.com/youtube/v3/videos?id={target_video_id}&part=statistics,contentDetails&key={YOUTUBE_API_KEY}"
        resp2 = requests.get(video_url, timeout=10)
        resp2.raise_for_status()
        video_data = resp2.json()
        
        view_count = 0
        duration_formatted = "0:00"
        
        if video_data.get("items"):
            item_data = video_data["items"][0]
            view_count = int(item_data["statistics"].get("viewCount", 0))
            duration_iso = item_data["contentDetails"].get("duration", "")
            duration_formatted = parse_iso_duration(duration_iso)

        return {
            "video_id": target_video_id,
            "title": title,
            "published": datetime.fromisoformat(published_at.replace("Z", "+00:00")).astimezone(timezone.utc),
            "viewCount": view_count,
            "channelName": channel_name,
            "duration": duration_formatted
        }

    except requests.exceptions.RequestException as e:
        print(f"❌ API Error: {e}")
        return None

# ---------------- MAIN SYNC PROCESS ----------------
def process_and_update_firestore():
    # Add a slight jitter to make automated execution less predictable 
    sleep_time = random.randint(1, 5)
    print(f"⏳ Jitter delay: Waiting {sleep_time} seconds...")
    time.sleep(sleep_time)

    print("🔄 Fetching latest stream data from YouTube Data API (Cost: 2 Units)...")
    latest_data = fetch_latest_api_data()

    if not latest_data:
        print(f"❌ No '{TARGET_TITLE}' video found.")
        return

    video_id = latest_data["video_id"]
    new_url = f"https://www.youtube.com/watch?v={video_id}"

    # ---------------- 1. READ FIREBASE ----------------
    print(f"\n🔍 Target Stream Found: {latest_data['title']} ({latest_data['viewCount']} views)")
    print("🔍 Reading existing data from Firestore...")
    
    # ✅ FILTER: Target Document ID
    gurbani_docs = db_gurbani.collection(COLLECTION_GURBANI).where(
        filter=FieldFilter(TARGET_DOC_ID, "==", CHANNEL_ID)
    ).limit(1).get()
    
    harmandir_docs = db_harmandir.collection(COLLECTION_HARMANDIR).where(
        filter=FieldFilter(TARGET_DOC_ID, "==", CHANNEL_ID)
    ).limit(1).get()

    gurbani_doc = gurbani_docs[0] if gurbani_docs else None
    harmandir_doc = harmandir_docs[0] if harmandir_docs else None

    # ---------------- 2. BUILD FINAL PAYLOAD ----------------
    time_ago_ms = str(int(latest_data["published"].timestamp() * 1000))
    current_timestamp_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    logo_url = fetch_channel_logo(CHANNEL_ID)

    base_payload = {
        "channelLogoUrl": logo_url,
        "channelName": latest_data["channelName"],
        "channel_id": CHANNEL_ID,
        "duration": latest_data["duration"],
        "hukamnama": CHANNEL_ID,
        TARGET_DOC_ID: CHANNEL_ID,  # ✅ Dynamic field injection
        "imageUrl": get_working_image_url(video_id),
        "isLive": False,
        "timeAgo": time_ago_ms,
        "timestamp": current_timestamp_ms,
        "title": latest_data["title"],
        "url": new_url,
        "viewCount": latest_data["viewCount"]
    }

    # ---------------- 3. CONDITIONAL WRITE FIREBASE ----------------
    def safe_update(doc_snapshot, payload, app_name):
        if not doc_snapshot:
            print(f"❌ Document missing in {app_name}, cannot update.")
            return

        existing = doc_snapshot.to_dict()
        
        # Prevent unnecessary writes if nothing important changed
        if (existing.get("url") == payload["url"] and 
            existing.get("viewCount") == payload["viewCount"] and 
            existing.get("title") == payload["title"]):
            print(f"⏭ No data changed for {app_name} (Views still {payload['viewCount']}). Write skipped.")
            return

        doc_snapshot.reference.update(payload)
        print(f"✅ {app_name} updated successfully!")

    print("\n📝 Pushing updates to Databases...")
    
    safe_update(gurbani_doc, base_payload, "Gurbani App")

    harmandir_payload = base_payload.copy()
    harmandir_payload["titleLowercase"] = base_payload["title"].lower()
    safe_update(harmandir_doc, harmandir_payload, "Harmandir App")

if __name__ == "__main__":
    process_and_update_firestore()
