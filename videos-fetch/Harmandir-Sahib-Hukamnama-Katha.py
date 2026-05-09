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
CHANNEL_ID = "UCYn6UEtQ771a_OWSiNBoG8w"
TARGET_TITLE = "Hukamnama Katha"
TARGET_DOC_ID = "hukamnama_katha"
MIN_DURATION_SECONDS = 180  # ⏱️ 3 minutes minimum to reject YouTube Shorts
FETCH_LIMIT = 5             # 🔍 Number of recent uploads to check

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

def generate_search_keywords(title):
    """Generates prefix keywords for search indexing."""
    if not isinstance(title, str): return []
    words = re.split(r'[\s|()\[\]{}.,\'":;?!\-_]+', title.lower())
    keywords = set()
    for word in words:
        word = word.strip()
        if len(word) > 0:
            prefix = ""
            for char in word:
                prefix += char
                if prefix.strip():
                    keywords.add(prefix)
    return list(keywords)

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
    new_url = f"https://www.youtube.com/watch?v={video_id}"

    print(f"\n🔍 Target Stream Found: {latest_data['title']} ({latest_data['viewCount']} views, Duration: {latest_data['duration']})")

    # ---------------- 1. BUILD FINAL PAYLOAD ----------------
    time_ago_ms = str(int(latest_data["published"].timestamp() * 1000))
    current_timestamp_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    logo_url = fetch_channel_logo(CHANNEL_ID)

    base_payload = {
        "channelLogoUrl": logo_url,
        "channelName": latest_data["channelName"],
        "duration": latest_data["duration"],
        TARGET_DOC_ID: CHANNEL_ID,
        "imageUrl": get_working_image_url(video_id),
        "isLive": False,
        "timeAgo": time_ago_ms,
        "timestamp": current_timestamp_ms,
        "title": latest_data["title"],
        "url": new_url,
        "viewCount": latest_data["viewCount"],
        "searchKeywords": generate_search_keywords(latest_data["title"])
    }

    # ---------------- 2. CREATE/UPDATE FIREBASE DOCUMENTS ----------------
    def safe_create_or_update(collection_ref, payload, app_name, custom_doc_id):
        try:
            # .set() smartly overwrites an existing document with this ID, or creates a new one if it doesn't exist
            collection_ref.document(custom_doc_id).set(payload)
            print(f"✅ Document successfully updated/created in {app_name}! (ID: {custom_doc_id})")
        except Exception as e:
            print(f"❌ Failed to process document in {app_name}: {e}")

    document_id = f"{TARGET_DOC_ID}-{video_id}"
    print(f"\n📝 Processing documents with specific ID: {document_id} ...")
    
    # Create/Update document in Gurbani App 
    safe_create_or_update(db_gurbani.collection(COLLECTION_GURBANI), base_payload, "Gurbani App", document_id)

    # Create/Update document in Harmandir App
    harmandir_payload = base_payload.copy()
    harmandir_payload["titleLowercase"] = base_payload["title"].lower()
    safe_create_or_update(db_harmandir.collection(COLLECTION_HARMANDIR), harmandir_payload, "Harmandir App", document_id)

if __name__ == "__main__":
    process_and_update_firestore()
