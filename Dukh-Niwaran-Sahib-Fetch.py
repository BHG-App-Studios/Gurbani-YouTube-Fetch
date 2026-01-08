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
    service_account_info = json.loads(SERVICE_ACCOUNT_JSON)
    cred = credentials.Certificate(service_account_info)
    firebase_admin.initialize_app(cred)

db = firestore.client()

# ---------------- RSS FETCH (LATEST 5 VIDEOS) ----------------
def fetch_latest_5_videos():
    response = requests.get(RSS_URL, timeout=15)
    response.raise_for_status()

    root = ET.fromstring(response.text)
    videos = []

    for entry in root.findall("atom:entry", NS):
        title_el = entry.find("atom:title", NS)
        video_id_el = entry.find("yt:videoId", NS)
        published_el = entry.find("atom:published", NS)

        if not title_el or not video_id_el or not published_el:
            continue

        published = datetime.fromisoformat(
            published_el.text.replace("Z", "+00:00")
        ).astimezone(timezone.utc)

        videos.append({
            "video_id": video_id_el.text.strip(),
            "title": title_el.text.strip(),
            "published": published
        })

    # Sort newest first and take top 5
    videos.sort(key=lambda x: x["published"], reverse=True)
    return videos[:5]

# ---------------- YOUTUBE OFFICIAL API (1 CALL) ----------------
def fetch_video_details(video_ids):
    ids = ",".join(video_ids)

    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "part": "snippet,liveStreamingDetails",
        "id": ids,
        "key": YOUTUBE_API_KEY
    }

    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json().get("items", [])

# ---------------- SELECT FINAL VIDEO ----------------
def select_best_video(videos, api_items):
    live_video = None
    latest_video = max(videos, key=lambda x: x["published"])

    for item in api_items:
        if item["snippet"].get("liveBroadcastContent") == "live":
            live_video = item
            break

    selected = live_video if live_video else next(
        v for v in api_items if v["id"] == latest_video["video_id"]
    )

    return {
        "imageUrl": f"https://i.ytimg.com/vi/{selected['id']}/hqdefault.jpg",
        "title": selected["snippet"]["title"],
        "url": f"https://www.youtube.com/watch?v={selected['id']}"
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
        print("❌ No Firestore document found")
        return

    doc_ref = docs[0].reference
    existing = docs[0].to_dict()

    if existing.get("url") == data["url"]:
        print("⏭ No change detected. Skipping update.")
        return

    doc_ref.update({
        "imageUrl": data["imageUrl"],
        "title": data["title"],
        "url": data["url"]
    })

    print("✅ Firebase updated successfully")

# ---------------- MAIN ----------------
if __name__ == "__main__":

    latest_videos = fetch_latest_5_videos()

    if not latest_videos:
        print("❌ No videos found in RSS")
        exit(0)

    video_ids = [v["video_id"] for v in latest_videos]

    api_items = fetch_video_details(video_ids)

    if not api_items:
        print("❌ YouTube API returned no data")
        exit(0)

    final_video = select_best_video(latest_videos, api_items)

    print("🎯 FINAL SELECTED VIDEO")
    print(final_video)

    update_firestore(final_video)
