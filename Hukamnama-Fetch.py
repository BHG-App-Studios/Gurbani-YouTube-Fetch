import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import firebase_admin
from firebase_admin import credentials, firestore
import json
import os
import sys
import re
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


# ---------------- API HELPER: DURATION FETCH ----------------
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

def fetch_durations_batch(video_ids):
    duration_map = {}
    CHUNK_SIZE = 50 
    for chunk in chunk_list(video_ids, CHUNK_SIZE):
        url = "https://www.googleapis.com/youtube/v3/videos"
        params = {
            "part": "contentDetails",
            "id": ",".join(chunk),
            "key": YOUTUBE_API_KEY,
            "maxResults": 50
        }
        try:
            r = requests.get(url, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
            for item in data.get("items", []):
                vid = item["id"]
                iso = item["contentDetails"]["duration"]
                duration_map[vid] = iso8601_to_seconds(iso)
        except Exception as e:
            print(f"⚠️ Error fetching durations: {e}")
    return duration_map

# ---------------- API HELPER: CHECK IMAGE URL ----------------
def get_working_image_url(video_id):
    """Pings the maxres image. If 404, falls back to hqdefault_live"""
    maxres_url = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
    fallback_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault_live.jpg"
    
    try:
        # HEAD is faster because it doesn't download the image body, just the status
        response = requests.head(maxres_url, timeout=5)
        if response.status_code == 200:
            return maxres_url
    except Exception:
        pass # Ignore timeouts/errors and just fallback
        
    return fallback_url

# ---------------- RSS FETCH ----------------
def fetch_latest_hukamnama_katha():
    response = requests.get(RSS_URL, timeout=15)
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

        # ✅ FILTER: Today Hukamnama Sri Darbar Sahib ONLY
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

    # ✅ Sort matches by latest date first
    matches.sort(key=lambda x: x["published"], reverse=True)

    # Fetch duration for all matched candidate IDs in batch
    candidate_ids = [m["video_id"] for m in matches]
    duration_map = fetch_durations_batch(candidate_ids)

    # Find the latest video that is longer than the MIN_DURATION
    latest = None
    for match in matches:
        vid = match["video_id"]
        duration = duration_map.get(vid, 0)
        
        if duration >= MIN_DURATION_SECONDS:
            latest = match
            print(f"✅ Found latest qualifying video (Duration: {duration}s): {vid}")
            break
        else:
            print(f"⏭️ Skipping video (Duration: {duration}s < 180s): {vid}")

    if not latest:
        print("❌ No matching videos found that meet the 3-minute minimum length requirement.")
        return None

    return {
        # 👇 CHANGED: Now uses the helper function to check the image URL
        "imageUrl": get_working_image_url(latest['video_id']),
        "title": latest["title"],
        "url": f"https://www.youtube.com/watch?v={latest['video_id']}"
    }

# ---------------- FIRESTORE UPDATE DUAL DATABASES ----------------
def update_firestore_dual(data):
    print("\n📝 Updating Databases...")
    
    # --- Prepare Data for Gurbani App (Basic Fields Only) ---
    gurbani_update_payload = {
        "imageUrl": data["imageUrl"],
        "title": data["title"],
        "url": data["url"]
    }

    # --- Prepare Data for Harmandir Sahib App (With lowercase, NO timestamp edit) ---
    harmandir_update_payload = {
        "imageUrl": data["imageUrl"],
        "title": data["title"],
        "url": data["url"],
        "titleLowercase": data["title"].lower()
    }

    # helper function to execute update
    def do_update(db_client, collection_name, app_name, payload):
        docs = (
            db_client.collection(collection_name)
            # ❗ Important: Kept the specific query field from your original script
            .where(filter=FieldFilter("hukamnama", "==", CHANNEL_ID))
            .limit(1)
            .get()
        )

        if not docs:
            print(f"❌ No document found for channel ID in {app_name} (Collection: {collection_name})")
            return

        doc = docs[0]
        existing = doc.to_dict()

        # 🔒 CHANGE-DETECTION
        if existing.get("url") == payload["url"]:
            print(f"⏭ No change detected for {app_name}. Skipping update.")
            return

        # ✅ UPDATE ONLY IF CHANGED
        doc.reference.update(payload)
        print(f"✅ {app_name} updated successfully")

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
        print("🎯 Selected Today Hukamnama Sri Darbar Sahib:")
        print(f"Title: {result['title']}")
        print(f"URL: {result['url']}")
        update_firestore_dual(result)
