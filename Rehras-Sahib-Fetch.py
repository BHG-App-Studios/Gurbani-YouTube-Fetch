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
CHANNEL_ID = "UC5meRCEfnem7_z0O-PSNJsw"
TARGET_TITLE = "🔴LIVE REHRAS SAHIB"
FETCH_LIMIT = 50
MIN_DURATION_SECONDS = 180

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

print("🔌 Initializing Firebase Connections...")
cred_gurbani = credentials.Certificate(json.loads(SERVICE_ACCOUNT_GURBANI))
app_gurbani = firebase_admin.initialize_app(cred_gurbani, name='gurbani_app')
db_gurbani = firestore.client(app=app_gurbani)

cred_harmandir = credentials.Certificate(json.loads(SERVICE_ACCOUNT_HARMANDIR))
app_harmandir = firebase_admin.initialize_app(cred_harmandir, name='harmandir_app')
db_harmandir = firestore.client(app=app_harmandir)

def fetch_channel_logo(channel_id):
    """Scrapes the channel HTML for the logo"""
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
    if not duration_iso or duration_iso == "P0D": 
        return 0
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
    if not duration_iso or duration_iso == "P0D": 
        return "00:00"
    match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_iso)
    if not match: 
        return "00:00"
        
    hours, minutes, seconds = match.groups()
    hours = int(hours) if hours else 0
    minutes = int(minutes) if minutes else 0
    seconds = int(seconds) if seconds else 0
    
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"

def fetch_latest_api_data():
    playlist_id = "UU" + CHANNEL_ID[2:]
    
    playlist_url = f"https://www.googleapis.com/youtube/v3/playlistItems?part=snippet&playlistId={playlist_id}&maxResults={FETCH_LIMIT}&key={YOUTUBE_API_KEY}"
    
    try:
        resp1 = requests.get(playlist_url, timeout=10)
        resp1.raise_for_status()
        playlist_data = resp1.json()
        
        candidates = []
        for item in playlist_data.get("items", []):
            snippet = item["snippet"]
            if TARGET_TITLE in snippet["title"]:
                candidates.append({
                    "video_id": snippet["resourceId"]["videoId"],
                    "title": snippet["title"],
                    "published_at": snippet["publishedAt"]
                })
                
        if not candidates:
            return None
            
        video_ids = ",".join([c["video_id"] for c in candidates])
        video_url = f"https://www.googleapis.com/youtube/v3/videos?id={video_ids}&part=snippet,statistics,contentDetails&key={YOUTUBE_API_KEY}"
        
        resp2 = requests.get(video_url, timeout=10)
        resp2.raise_for_status()
        video_data = resp2.json()
        
        video_details = {item["id"]: item for item in video_data.get("items", [])}
        
        valid_payloads = []

        for candidate in candidates:
            vid_id = candidate["video_id"]
            if vid_id not in video_details:
                continue
                
            yt_item = video_details[vid_id]
            snippet = yt_item["snippet"]
            stats = yt_item["statistics"]
            content = yt_item["contentDetails"]
            
            is_live = snippet.get("liveBroadcastContent") == "live"
            duration_iso = content.get("duration", "")
            
            if is_live or get_total_seconds(duration_iso) >= MIN_DURATION_SECONDS:
                view_count = int(stats.get("viewCount", 0))
                duration_str = "00:00" if is_live else parse_iso_duration(duration_iso)
                
                published_dt = datetime.fromisoformat(candidate["published_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
                published_time_ms = int(published_dt.timestamp() * 1000)

                valid_payloads.append({
                    "imageUrl": get_working_image_url(vid_id),
                    "isLive": is_live,
                    "title": snippet.get("title", ""),
                    "url": f"https://www.youtube.com/watch?v={vid_id}",
                    "channelName": snippet.get("channelTitle", ""),
                    "channelId_temp": snippet.get("channelId", CHANNEL_ID),
                    "viewCount": view_count,
                    "timeAgo": str(published_time_ms),
                    "duration": duration_str,
                    "_sort_time": published_time_ms
                })

        if not valid_payloads:
            print(f"⚠️ Found matching titles, but they were not live AND shorter than {MIN_DURATION_SECONDS} seconds.")
            return None

        valid_payloads.sort(key=lambda x: (x["isLive"], x["_sort_time"]), reverse=True)

        final_winner = valid_payloads[0]
        
        final_winner["channelLogoUrl"] = fetch_channel_logo(final_winner["channelId_temp"])
        
        del final_winner["_sort_time"]
        del final_winner["channelId_temp"]

        return final_winner

    except requests.exceptions.RequestException as e:
        print(f"❌ API Error: {e}")
        return None

def update_firestore_dual(payload):
    print("\n📝 Updating Databases...")

    harmandir_payload = payload.copy()
    harmandir_payload["titleLowercase"] = payload["title"].lower()

    def do_update(db_client, collection_name, app_name, data):
        docs = (
            db_client.collection(collection_name)
            .where(filter=FieldFilter("channel_Id", "==", CHANNEL_ID))
            .limit(1)
            .get()
        )

        if not docs:
            print(f"❌ No document found for channel ID in {app_name}")
            return

        doc = docs[0]
        
        doc.reference.update(data)
        print(f"✅ {app_name} updated successfully with full data!")

    do_update(db_gurbani, COLLECTION_GURBANI, "Gurbani App", payload)
    do_update(db_harmandir, COLLECTION_HARMANDIR, "Harmandir App", harmandir_payload)

if __name__ == "__main__":
    sleep_time = random.randint(1, 5)
    print(f"⏳ Jitter delay: Waiting {sleep_time} seconds...")
    time.sleep(sleep_time)

    print(f"🔄 Fetching latest stream data via Playlist API (Checking top {FETCH_LIMIT})...")
    final_payload = fetch_latest_api_data()

    if not final_payload:
        print("❌ No valid matching video found.")
        sys.exit(0)

    print("\n🎯 Final Payload to Save:")
    print(json.dumps(final_payload, indent=2))

    update_firestore_dual(final_payload)