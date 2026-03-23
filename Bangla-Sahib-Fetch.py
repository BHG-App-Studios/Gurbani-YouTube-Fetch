import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import firebase_admin
from firebase_admin import credentials, firestore
import json
import os
import sys
from google.cloud.firestore_v1 import FieldFilter

# ---------------- CONFIG ----------------
CHANNEL_ID = "UCA1Jqo-WXVuMgs4WcD5f5Yw"
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

# Env variables for BOTH service accounts
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


# ---------------- RSS FETCH (LATEST 5 MATCHES) ----------------
def fetch_latest_5_matching():
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

        # ✅ FILTER: Live Gurdwara Bangla Sahib ONLY
        if "Live Gurdwara Bangla Sahib" not in title:
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
        return []

    # ✅ SORT BY TIME & TAKE LATEST 5
    matches.sort(key=lambda x: x["published"], reverse=True)
    return matches[:5]

# ---------------- YOUTUBE API (SINGLE CALL) ----------------
def fetch_video_details(video_ids):
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "key": YOUTUBE_API_KEY,
        "part": "snippet,liveStreamingDetails",
        "id": ",".join(video_ids),
        "maxResults": 5
    }

    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json().get("items", [])

# ---------------- API HELPER: CHECK IMAGE URL ----------------
def get_working_image_url(video_id):
    """Pings the maxres image. If 404, falls back to hqdefault_live"""
    maxres_url = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
    fallback_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault_live.jpg"
    
    try:
        response = requests.head(maxres_url, timeout=5)
        if response.status_code == 200:
            return maxres_url
    except Exception:
        pass 
        
    return fallback_url

# ---------------- SELECT FINAL VIDEO ----------------
def select_best_video(rss_videos, yt_videos):
    yt_map = {v["id"]: v for v in yt_videos}

    live_candidate = None
    latest_candidate = None
    latest_time = None

    for v in rss_videos:
        yt = yt_map.get(v["video_id"])
        if not yt:
            continue

        snippet = yt["snippet"]
        live_status = snippet.get("liveBroadcastContent")

        if latest_time is None or v["published"] > latest_time:
            latest_time = v["published"]
            latest_candidate = yt

        if live_status == "live":
            live_candidate = yt
            break

    final = live_candidate if live_candidate else latest_candidate
    if not final:
        return None

    return {
        "title": final["snippet"]["title"],
        "url": f"https://www.youtube.com/watch?v={final['id']}",
        "imageUrl": get_working_image_url(final['id'])
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
            .where(filter=FieldFilter("channel_Id", "==", CHANNEL_ID))
            .limit(1)
            .get()
        )

        if not docs:
            print(f"❌ No document found for channel ID in {app_name} (Collection: {collection_name})")
            return

        doc = docs[0]
        existing = doc.to_dict()

        if existing.get("url") == payload["url"]:
            print(f"⏭ No change detected for {app_name}. Skipping update.")
            return

        # .update() only modifies the fields passed in. 
        # So "timestamp" is safely ignored and stays untouched.
        doc.reference.update(payload)
        print(f"✅ {app_name} updated successfully")

    # Run for Gurbani
    do_update(db_gurbani, COLLECTION_GURBANI, "Gurbani App", gurbani_update_payload)
    
    # Run for Harmandir Sahib
    do_update(db_harmandir, COLLECTION_HARMANDIR, "Harmandir App", harmandir_update_payload)


# ---------------- MAIN ----------------
if __name__ == "__main__":

    print("🔄 Fetching latest Live Gurdwara Bangla Sahib videos from RSS...")
    rss_videos = fetch_latest_5_matching()

    if not rss_videos:
        print("❌ No Live Gurdwara Bangla Sahib video found")
        exit(0)

    video_ids = [v["video_id"] for v in rss_videos]

    print("📡 Fetching video details from YouTube API (single call)...")
    yt_videos = fetch_video_details(video_ids)

    final_video = select_best_video(rss_videos, yt_videos)

    if not final_video:
        print("❌ No valid video selected")
        exit(0)

    print(f"🎯 Selected: {final_video['title']}")
    print(f"🔗 URL: {final_video['url']}")

    update_firestore_dual(final_video)
