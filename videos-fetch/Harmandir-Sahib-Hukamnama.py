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
CHANNEL_ID = "UCD5a5KBLu4t7uZdoJQzEppg"
TARGET_TITLE = "Today Hukamnama Sri Darbar Sahib"
TARGET_DOC_ID = "hukamnama"
MIN_DURATION_SECONDS = 180  # ⏱️ 3 minutes minimum to reject YouTube Shorts
FETCH_LIMIT = 5             # 🔍 Number of recent uploads to check
STATE_FILE_PATH = "more/last_fetched_harmandir.txt"  # Path for the state file

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
COLLECTION_HARMANDIR = "Kirtan-Youtube-Videos"

# ---------------- FIREBASE DUAL INIT ----------------
print("🔌 Initializing Firebase Connections...")

cred_gurbani = credentials.Certificate(json.loads(SERVICE_ACCOUNT_GURBANI))
app_gurbani = firebase_admin.initialize_app(cred_gurbani, name='gurbani_app')
db_gurbani = firestore.client(app=app_gurbani)

cred_harmandir = credentials.Certificate(json.loads(SERVICE_ACCOUNT_HARMANDIR))
app_harmandir = firebase_admin.initialize_app(cred_harmandir, name='harmandir_app')
db_harmandir = firestore.client(app=app_harmandir)


# ---------------- STATE MANAGEMENT ----------------
def get_last_processed_video_id():
    """Reads the local state file to get the last processed video ID."""
    if os.path.exists(STATE_FILE_PATH):
        try:
            with open(STATE_FILE_PATH, "r") as f:
                data = json.load(f)
                return data.get("video_id")
        except Exception as e:
            print(f"⚠️ Error reading state file: {e}")
            return None
    return None

def save_processed_video_state(video_id, title, url, published_date):
    """Saves the latest processed video info to the repo directory."""
    os.makedirs(os.path.dirname(STATE_FILE_PATH), exist_ok=True)
    data = {
        "video_id": video_id,
        "title": title,
        "url": url,
        "published_date": str(published_date),
        "processed_at": str(datetime.now(timezone.utc))
    }
    try:
        with open(STATE_FILE_PATH, "w") as f:
            json.dump(data, f, indent=4)
        print(f"📝 Successfully saved video state to {STATE_FILE_PATH}")
    except Exception as e:
        print(f"❌ Failed to save state file: {e}")

# ---------------- HELPERS ----------------
def get_latest_rss_video():
    """Fetches the latest video ID from the free YouTube RSS feed."""
    rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
    try:
        response = requests.get(rss_url, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'xml')
        
        entries = soup.find_all('entry')
        for entry in entries:
            title_tag = entry.find('title')
            if title_tag and TARGET_TITLE in title_tag.text:
                video_id_tag = entry.find('yt:videoId')
                if video_id_tag:
                    return video_id_tag.text
        return None
    except Exception as e:
        print(f"❌ RSS Fetch Error: {e}")
        return None

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

def get_total_seconds(duration_iso):
    """Parses ISO 8601 duration and returns total seconds for mathematical comparison."""
    match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_iso)
    if not match: 
        return 0
    hours, minutes, seconds = match.groups()
    total = 0
    if hours: total += int(hours) * 3600
    if minutes: total += int(minutes) * 60
    if seconds: total += int(seconds)
    return total

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
    
    # Unit 1: Fetch the most recent uploads up to FETCH_LIMIT
    playlist_url = f"https://www.googleapis.com/youtube/v3/playlistItems?part=snippet&playlistId={playlist_id}&maxResults={FETCH_LIMIT}&key={YOUTUBE_API_KEY}"
    
    try:
        resp1 = requests.get(playlist_url, timeout=10)
        resp1.raise_for_status()
        playlist_data = resp1.json()
        
        # Gather ALL recent videos from the fetched batch that match the target title
        candidates = []
        for item in playlist_data.get("items", []):
            snippet = item["snippet"]
            if TARGET_TITLE in snippet["title"]:
                candidates.append({
                    "video_id": snippet["resourceId"]["videoId"],
                    "title": snippet["title"],
                    "published_at": snippet["publishedAt"],
                    "channel_name": snippet["channelTitle"]
                })
                
        if not candidates:
            return None
            
        # Unit 2: Fetch Statistics & ContentDetails for ALL candidates at once
        video_ids = ",".join([c["video_id"] for c in candidates])
        video_url = f"https://www.googleapis.com/youtube/v3/videos?id={video_ids}&part=statistics,contentDetails&key={YOUTUBE_API_KEY}"
        resp2 = requests.get(video_url, timeout=10)
        resp2.raise_for_status()
        video_data = resp2.json()
        
        # Map the API response by video ID for easy lookup
        video_details = {item["id"]: item for item in video_data.get("items", [])}
        
        # Loop through candidates in chronological order (newest first)
        for candidate in candidates:
            vid_id = candidate["video_id"]
            if vid_id not in video_details:
                continue
                
            details = video_details[vid_id]
            duration_iso = details["contentDetails"].get("duration", "")
            
            # ✅ FILTER: Find the first (newest) video that meets the duration requirement
            if get_total_seconds(duration_iso) >= MIN_DURATION_SECONDS:
                view_count = int(details["statistics"].get("viewCount", 0))
                duration_formatted = parse_iso_duration(duration_iso)
                
                return {
                    "video_id": vid_id,
                    "title": candidate["title"],
                    "published": datetime.fromisoformat(candidate["published_at"].replace("Z", "+00:00")).astimezone(timezone.utc),
                    "viewCount": view_count,
                    "channelName": candidate["channel_name"],
                    "duration": duration_formatted
                }

        print(f"⚠️ Found matching titles within the last {FETCH_LIMIT} uploads, but all were shorter than {MIN_DURATION_SECONDS} seconds (Shorts rejected).")
        return None

    except requests.exceptions.RequestException as e:
        print(f"❌ API Error: {e}")
        return None

# ---------------- MAIN SYNC PROCESS ----------------
def process_and_update_firestore():
    # 1. Pre-Check: Free RSS Feed vs Local State
    last_processed_id = get_last_processed_video_id()
    print("📡 Checking free RSS feed for latest uploads...")
    rss_video_id = get_latest_rss_video()

    if rss_video_id and rss_video_id == last_processed_id:
        print(f"✅ Video {rss_video_id} is already saved in Firestore today. Exiting to save API Quota.")
        return

    print("🔄 New potential video detected! Proceeding with logic...")

    # Add a slight jitter to make automated execution less predictable 
    sleep_time = random.randint(1, 15)
    print(f"⏳ Jitter delay: Waiting {sleep_time} seconds...")
    time.sleep(sleep_time)

    print(f"🔄 Fetching latest stream data from YouTube Data API (Checking top {FETCH_LIMIT} - Cost: 2 Units)...")
    latest_data = fetch_latest_api_data()

    if not latest_data:
        print(f"❌ No valid '{TARGET_TITLE}' video found matching duration criteria.")
        return

    video_id = latest_data["video_id"]
    
    # Secondary check: If the API pulled an older valid video we already processed, safely exit.
    if video_id == last_processed_id:
        print(f"✅ The valid video found ({video_id}) is already processed. Exiting.")
        return

    new_url = f"https://www.youtube.com/watch?v={video_id}"

    print(f"\n🔍 Target Stream Found: {latest_data['title']} ({latest_data['viewCount']} views, Duration: {latest_data['duration']})")

    # ---------------- 2. BUILD FINAL PAYLOAD ----------------
    time_ago_ms = str(int(latest_data["published"].timestamp() * 1000))
    current_timestamp_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    logo_url = fetch_channel_logo(CHANNEL_ID)

    base_payload = {
        "channelLogoUrl": logo_url,
        "channelName": latest_data["channelName"],
        "channel_id": CHANNEL_ID,
        "duration": latest_data["duration"],
        TARGET_DOC_ID: CHANNEL_ID,  # ✅ Dynamic field injection
        "imageUrl": get_working_image_url(video_id),
        "isLive": False,
        "timeAgo": time_ago_ms,
        "timestamp": current_timestamp_ms,
        "title": latest_data["title"],
        "url": new_url,
        "viewCount": latest_data["viewCount"]
    }

    # ---------------- 3. CREATE NEW FIREBASE DOCUMENTS ----------------
    def safe_create(collection_ref, payload, app_name):
        try:
            # .add() creates a brand new document with an auto-generated ID
            _, doc_ref = collection_ref.add(payload)
            print(f"✅ New document created in {app_name} successfully! (ID: {doc_ref.id})")
            return True
        except Exception as e:
            print(f"❌ Failed to create document in {app_name}: {e}")
            return False

    print("\n📝 Creating new documents in Databases...")
    
    # Create new document in Gurbani App 
    gurbani_success = safe_create(db_gurbani.collection(COLLECTION_GURBANI), base_payload, "Gurbani App")

    # Create new document in Harmandir App
    harmandir_payload = base_payload.copy()
    harmandir_payload["titleLowercase"] = base_payload["title"].lower()
    harmandir_success = safe_create(db_harmandir.collection(COLLECTION_HARMANDIR), harmandir_payload, "Harmandir App")

    # ---------------- 4. UPDATE LOCAL STATE ON SUCCESS ----------------
    if gurbani_success or harmandir_success:
        save_processed_video_state(video_id, latest_data["title"], new_url, latest_data["published"])

if __name__ == "__main__":
    process_and_update_firestore()
