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
ALL_IDS_DOC = "-All_Videos_Id"   # use the exact doc name you have in Firestore

SERVICE_ACCOUNT_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
if not SERVICE_ACCOUNT_JSON:
    print("❌ FIREBASE_SERVICE_ACCOUNT env var is not set")
    sys.exit(1)

# ----------------------------------------

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
    data = ids_doc.to_dict() or {}
    existing_ids = set(data.get("video_id", []))

print(f"📦 Existing video IDs in Firebase: {len(existing_ids)}")

# ---------------- COUNTERS ----------------
total_fetched = 0
total_skipped = 0
total_inserted = 0
new_ids_added = []

# ---------------- HELPERS ----------------
def find_child_by_localname(parent, localname):
    """Return first child whose tag local-name matches localname (namespace-agnostic)."""
    for ch in parent:
        # tag may be like '{namespace}localname' or 'localname'
        tag = ch.tag
        if tag is None:
            continue
        if tag == localname or tag.endswith('}' + localname):
            return ch
    return None

def safe_text(elem):
    return elem.text.strip() if (elem is not None and elem.text) else None

# ---------------- FETCH RSS (ROBUST / DEBUG) ----------------
def fetch_videos_from_channel(channel_id):
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    print("  → fetching:", url)
    try:
        res = requests.get(url, timeout=20)
        res.raise_for_status()
    except Exception as e:
        print("  ❌ Request failed:", e)
        return []

    text = res.text
    print(f"  🌐 HTTP {res.status_code}, size: {len(text)} chars")

    # save a copy for debugging in CI runs (optional)
    safe_filename = f"rss_{channel_id}.xml"
    try:
        with open(safe_filename, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("  💾 Saved:", safe_filename)
    except Exception as e:
        print("  ⚠ Could not save RSS file:", e)

    # Parse XML
    try:
        root = ET.fromstring(text)
    except Exception as e:
        print("  ❌ XML parse failed:", e)
        return []

    print("  root.tag:", root.tag)

    # Find entries in a namespace-agnostic way:
    # 1) try explicit atom namespace first (fast)
    entries = root.findall('.//{http://www.w3.org/2005/Atom}entry')
    if not entries:
        # 2) fallback: find any element whose localname is 'entry'
        entries = [el for el in root.iter() if (el.tag is not None and (el.tag == "entry" or el.tag.endswith("}entry")))]

    print("  🔎 Entries found:", len(entries))

    videos = []
    for entry in entries:
        # Find videoId (yt:videoId) by localname
        vid_el = find_child_by_localname(entry, "videoId")
        title_el = find_child_by_localname(entry, "title")
        pub_el = find_child_by_localname(entry, "published")

        # some feeds use media:group/media:thumbnail for image, but we'll use standard thumbnail url
        if not vid_el or not title_el or not pub_el:
            # skip malformed/partial entries
            continue

        video_id = safe_text(vid_el)
        title = safe_text(title_el)
        published_text = safe_text(pub_el)
        if not (video_id and title and published_text):
            continue

        try:
            published_dt = datetime.fromisoformat(published_text.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            # fallback: use now (shouldn't happen normally)
            published_dt = datetime.now(timezone.utc)

        videos.append({
            "video_id": video_id,
            "title": title,
            "published": published_dt,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "imageUrl": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
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
        try:
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
            # small pause to avoid burst (optional)
            time.sleep(0.05)
        except Exception as e:
            print("  ❌ Failed to insert:", v["video_id"], e)

# ---------------- UPDATE ID INDEX ----------------
if new_ids_added:
    try:
        ids_doc_ref.set({
            "video_id": list(existing_ids),
            "total_count": len(existing_ids)
        }, merge=True)
        print("\n✅ Updated ID index doc:", ALL_IDS_DOC)
    except Exception as e:
        print("❌ Failed to update All_Videos_Id doc:", e)

# ---------------- SUMMARY ----------------
print("\n================ SUMMARY ================")
print(f"📥 Total fetched  : {total_fetched}")
print(f"⏭️  Videos skipped : {total_skipped}")
print(f"➕ Videos inserted : {total_inserted}")
print(f"📊 Firebase total : {len(existing_ids)}")
print("========================================")
