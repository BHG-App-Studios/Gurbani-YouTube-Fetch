import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import firebase_admin
from firebase_admin import credentials, firestore
import json
import os
from google.cloud.firestore_v1 import FieldFilter

# ---------------- CONFIG ----------------
CHANNEL_ID = "UCPKPN4bzM8Ja-F_kIEZoAhA"
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

YOUTUBE_API_KEY = os.environ["YOUTUBE_API_KEY"]
SERVICE_ACCOUNT_JSON = os.environ["FIREBASE_SERVICE_ACCOUNT"]

COLLECTION_NAME = "liveStreams"
# --------------------------------------

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015"
}

# ---------------- FIREBASE INIT ----------------
if not firebase_admin._apps:
    cred = credentials.Certificate(json.loads(SERVICE_ACCOUNT_JSON))
    firebase_admin.initialize_app(cred)

db = firestore.client()

# ---------------- GET CURRENT FIREBASE URL ----------------
def get_current_firebase_url():
    docs = (
        db.collection(COLLECTION_NAME)
        .where(filter=FieldFilter("channel_Id", "==", CHANNEL_ID))
        .limit(1)
        .get()
    )
    if not docs:
        return None
    return docs[0].to_dict().get("url")

# ---------------- RSS FETCH (LATEST 5 VIDEOS) ----------------
def fetch_latest_5_from_rss():
    response = requests.get(RSS_URL, timeout=15)
    response.raise_for_status()

    root = ET.fromstring(response.text)
    videos = []

    for entry in root.findall("atom:entry", NS):
        video_id_el = entry.find("yt:videoId", NS)
        published_el = entry.find("atom:published", NS)

        if video_id_el is None or published_el is None:
            continue

        published = datetime.fromisoformat(
            published_el.text.replace("Z", "+00:00")
        ).astimezone(timezone.utc)

        videos.append({
            "video_id": video_id_el.text.strip(),
            "published": published
        })

    videos.sort(key=lambda x: x["published"], reverse=True)
    return videos[:5]

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
    return r.json()["items"]

# ---------------- SELECT FINAL VIDEO ----------------
def select_best_video(videos):
    live_video = None
    latest_video = None
    latest_time = None

    for v in videos:
        snippet = v["snippet"]
        live_status = snippet.get("liveBroadcastContent")

        published = datetime.fromisoformat(
            snippet["publishedAt"].replace("Z", "+00:00")
        ).astimezone(timezone.utc)

        if latest_time is None or published > latest_time:
            latest_time = published
            latest_video = v

        if live_status == "live":
            live_video = v
            break

    final = live_video if live_video else latest_video

    return {
        "title": final["snippet"]["title"],
        "url": f"https://www.youtube.com/watch?v={final['id']}",
        "imageUrl": final["snippet"]["thumbnails"]["high"]["url"]
    }

# ---------------- FIRESTORE UPDATE ----------------
def update_firestore(data):
    docs = (
        db.collection(COLLECTION_NAME)
        .where(filter=FieldFilter("channel_Id", "==", CHANNEL_ID))
        .limit(1)
        .get()
    )

    if not docs:
        print("❌ No Firestore document found for channel")
        return

    doc = docs[0]
    existing = doc.to_dict()

    if existing.get("url") == data["url"]:
        print("⏭ No change detected. Skipping update.")
        return

    doc.reference.update({
        "title": data["title"],
        "url": data["url"],
        "imageUrl": data["imageUrl"]
    })

    print("✅ Firebase updated successfully")

# ---------------- MAIN ----------------
if __name__ == "__main__":

    print("🔄 Fetching latest 5 videos from RSS...")
    rss_videos = fetch_latest_5_from_rss()

    if not rss_videos:
        print("❌ No videos found in RSS")
        exit(0)

    video_ids = [v["video_id"] for v in rss_videos]

    print("📡 Fetching video details from YouTube API (single call)...")
    yt_videos = fetch_video_details(video_ids)

    final_video = select_best_video(yt_videos)

    print("🎯 Selected Video:")
    print(final_video)

    update_firestore(final_video)
