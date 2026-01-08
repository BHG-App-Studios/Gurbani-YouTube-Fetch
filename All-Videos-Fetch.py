#!/usr/bin/env python3
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import firebase_admin
from firebase_admin import credentials, firestore
import json
import os
import sys
import time

# ---------------- CONFIG ----------------
CHANNEL_IDS = [
    "UC884UDwNldmpdEiS1mgtijA",
    "UC_JnnWTC6gHc59JwfMPTjdw",
    "UCQroafhIKCxeQ0e9jj-O51Q",
]

COLLECTION_NAME = "Listen_Kirtans_Videos"
ALL_IDS_DOC = "-All_Videos_Id"

SERVICE_ACCOUNT_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
if not SERVICE_ACCOUNT_JSON:
    print("❌ FIREBASE_SERVICE_ACCOUNT env var missing")
    sys.exit(1)

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015"
}

# ---------------- FIREBASE INIT ----------------
if not firebase_admin._apps:
    cred = credentials.Certificate(json.loads(SERVICE_ACCOUNT_JSON))
    firebase_admin.initialize_app(cred)

db = firestore.client()

# ---------------- READ EXISTING IDS (1 READ) ----------------
ids_doc_ref = db.collection(COLLECTION_NAME).document(ALL_IDS_DOC)
ids_doc = ids_doc_ref.get()

existing_ids = set()
if ids_doc.exists:
    existing_ids = set(ids_doc.to_dict().get("video_id", []))

print(f"📦 Existing video IDs in Firebase: {len(existing_ids)}")

# ---------------- COUNTERS ----------------
total_fetched = 0
total_skipped = 0
total_inserted = 0
new_ids_added = []

# ---------------- FETCH RSS ----------------
def fetch_videos_from_channel(channel_id):
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    response = requests.get(url, timeout=20)
    response.raise_for_status()

    root = ET.fromstring(response.text)
    videos = []

    entries = root.findall("atom:entry", NS)
    print(f"🔎 Entries found: {len(entries)}")

    for entry in entries:
        title_el = entry.find("atom:title", NS)
        video_id_el = entry.find("yt:videoId", NS)
        published_el = entry.find("atom:published", NS)

        if title_el is None or video_id_el is None or published_el is None:
            continue

        published_dt = datetime.fromisoformat(
            published_el.text.replace("Z", "+00:00")
        ).astimezone(timezone.utc)

        video_id = video_id_el.text.strip()

        videos.append({
            "video_id": video_id,
            "title": title_el.text.strip(),
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "imageUrl": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            "published": published_dt
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
        time.sleep(0.03)

# ---------------- UPDATE ID INDEX ----------------
if new_ids_added:
    ids_doc_ref.set({
        "video_id": list(existing_ids),
        "total_count": len(existing_ids)
    }, merge=True)

# ---------------- SUMMARY ----------------
print("\n================ SUMMARY ================")
print(f"📥 Total fetched  : {total_fetched}")
print(f"⏭️  Videos skipped : {total_skipped}")
print(f"➕ Videos inserted : {total_inserted}")
print(f"📊 Firebase total : {len(existing_ids)}")
print("========================================")
