import requests   
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import firebase_admin
from firebase_admin import credentials, firestore
import json
import os

# ---------------- CONFIG ----------------
CHANNEL_IDS = [
    "UC884UDwNldmpdEiS1mgtijA",
    "UC_JnnWTC6gHc59JwfMPTjdw",
    "UCQroafhIKCxeQ0e9jj-O51Q",
]

COLLECTION_NAME = "Listen_Kirtans_Videos_New"
ALL_IDS_DOC = "-All_Videos_Id"  

SERVICE_ACCOUNT_JSON = os.environ["FIREBASE_SERVICE_ACCOUNT"]

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}

# ---------------- FIREBASE INIT ----------------
if not firebase_admin._apps:
    cred = credentials.Certificate(json.loads(SERVICE_ACCOUNT_JSON))
    firebase_admin.initialize_app(cred)

db = firestore.client()

# ---------------- READ EXISTING IDS (1 READ ONLY) ----------------
ids_doc_ref = db.collection(COLLECTION_NAME).document(ALL_IDS_DOC)
ids_doc = ids_doc_ref.get()

existing_ids = set()
if ids_doc.exists:
    data = ids_doc.to_dict()
    existing_ids = set(data.get("video_id", []))

print(f"📦 Existing video IDs in Firebase: {len(existing_ids)}")

# ---------------- COUNTERS ----------------
total_fetched = 0
total_skipped = 0
total_inserted = 0
new_ids_added = []

# ---------------- FETCH RSS ----------------
def fetch_videos_from_channel(channel_id):
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    res = requests.get(url, timeout=20)
    res.raise_for_status()

    root = ET.fromstring(res.text)
    videos = []

    for entry in root.findall("atom:entry", NS):
        vid = entry.find("yt:videoId", NS)
        title = entry.find("atom:title", NS)
        published = entry.find("atom:published", NS)

        if not vid or not title or not published:
            continue

        published_dt = datetime.fromisoformat(
            published.text.replace("Z", "+00:00")
        ).astimezone(timezone.utc)

        video_id = vid.text.strip()

        videos.append({
            "video_id": video_id,
            "title": title.text.strip(),
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "imageUrl": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            "published": published_dt,
        })

    return videos

# ---------------- MAIN ----------------
for channel_id in CHANNEL_IDS:
    print(f"\n🔍 Fetching channel: {channel_id}")

    videos = fetch_videos_from_channel(channel_id)
    print(f"📺 Videos in RSS: {len(videos)}")

    total_fetched += len(videos)

    for v in videos:
        if v["video_id"] in existing_ids:
            total_skipped += 1
            continue

        # ➕ INSERT NEW VIDEO DOC (AUTO ID)
        db.collection(COLLECTION_NAME).document().set({
            "title": v["title"],
            "url": v["url"],
            "imageUrl": v["imageUrl"],
            "timestamp": firestore.Timestamp.from_datetime(v["published"]),
        })

        existing_ids.add(v["video_id"])
        new_ids_added.append(v["video_id"])
        total_inserted += 1

        print("➕ Inserted:", v["video_id"])

# ---------------- UPDATE ID INDEX ----------------
if new_ids_added:
    ids_doc_ref.set({
        "video_id": list(existing_ids),
        "total_count": len(existing_ids)
    }, merge=True)

# ---------------- SUMMARY ----------------
print("\n================ SUMMARY ================")
print(f"📥 Total fetched  : {total_fetched}")
print(f"⏭️  Skipped        : {total_skipped}")
print(f"➕ Inserted       : {total_inserted}")
print(f"📊 Firebase total : {len(existing_ids)}")
print("========================================")
