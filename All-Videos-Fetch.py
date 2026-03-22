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
import re

# ---------------- CONFIG ----------------
CHANNEL_IDS = [
    "UC884UDwNldmpdEiS1mgtijA",
    "UC_JnnWTC6gHc59JwfMPTjdw",
    "UCQroafhIKCxeQ0e9jj-O51Q",
    "UC71aJD7c8-FWf-nJ7ug2sfg",
    "UCUjIneSnBylQOqAk7n7i33A",
    "UC1wecYlMxn33DPHrhHHUyVw",
    "UCh0LDn5Drt44tITPoQiiJ6Q",
    "UCBe8nwY2SqWlrGKKcmxB0_w",
]

# 🚫 Keywords to exclude (Case Insensitive, Whole Words Only)
EXCLUDED_KEYWORDS = [
    "antim ardaas",
    "samagam",
    "semagam", 
    "promo",
    "mela",
    "nagar kirtan",
    "teaser",
    "live",
    "chaupai",
    "japji",
    "sukhmani",
    "rehras",
    "ardaas",
    "ardas",
    "bhog",
    "bhogg",
    "akhand",
    "asa ki vaar",
    "sohila sahib",
    "sohela sahib",
]

COLLECTION_NAME = "Listen_Kirtans_Videos_New"
ALL_IDS_DOC = "-All_Videos_Id"
MIN_DURATION_SECONDS = 180  # ⏱️ 3 minutes

SERVICE_ACCOUNT_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")

if not SERVICE_ACCOUNT_JSON:
    print("❌ FIREBASE_SERVICE_ACCOUNT env var missing")
    sys.exit(1)

if not YOUTUBE_API_KEY:
    print("❌ YOUTUBE_API_KEY env var missing")
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
total_skipped_existing = 0
total_skipped_live = 0
total_skipped_short = 0
total_skipped_keywords = 0
total_inserted = 0
new_ids_added = []

# ---------------- RSS FETCH ----------------
def fetch_videos_from_channel(channel_id):
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        response = requests.get(url, timeout=20)
        response.raise_for_status()
    except Exception as e:
        print(f"⚠️ Error fetching channel {channel_id}: {e}")
        return []

    root = ET.fromstring(response.text)
    videos = []

    entries = root.findall("atom:entry", NS)
    
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
            "imageUrl": f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
            "published": published_dt
        })

    return videos

# ---------------- HELPER: CHUNK LIST ----------------
def chunk_list(data, chunk_size):
    """Yield successive chunks from list."""
    for i in range(0, len(data), chunk_size):
        yield data[i:i + chunk_size]

# ---------------- API HELPER: CHECK LIVE STATUS ----------------
def get_live_status_batch(video_ids):
    live_or_upcoming_ids = set()
    CHUNK_SIZE = 30 
    
    for chunk in chunk_list(video_ids, CHUNK_SIZE):
        url = "https://www.googleapis.com/youtube/v3/videos"
        params = {
            "part": "snippet",
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
                broadcast_content = item["snippet"].get("liveBroadcastContent", "none")
                
                if broadcast_content in ["live", "upcoming"]:
                    live_or_upcoming_ids.add(vid)
                    print(f"🚫 Detected Live/Upcoming stream: {vid} ({broadcast_content})")
                    
        except Exception as e:
            print(f"⚠️ Error checking live status: {e}")
    
    return live_or_upcoming_ids

# ---------------- API HELPER: FETCH DURATIONS ----------------
def iso8601_to_seconds(duration):
    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration)
    if not match:
        return 0
    h = int(match.group(1) or 0)
    m = int(match.group(2) or 0)
    s = int(match.group(3) or 0)
    return h * 3600 + m * 60 + s

# ---------------- SEARCH HELPER ----------------
def generate_search_keywords(title):
    if not isinstance(title, str):
        return []
        
    # Split by spaces, pipes, and common punctuation
    words = re.split(r'[\s|()\[\]{}.,\'":;?!\-_]+', title.lower())
    keywords = set()
    
    for word in words:
        word = word.strip()
        if len(word) > 0:
            prefix = ""
            for char in word:
                prefix += char
                # Add to set if it's not empty space
                if prefix.strip():
                    keywords.add(prefix)
                    
    return list(keywords)


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


# ---------------- MAIN LOGIC ----------------
rss_videos = []

# 1. Gather all videos from RSS
for channel_id in CHANNEL_IDS:
    print(f"\n🔍 Fetching channel: {channel_id}")
    videos = fetch_videos_from_channel(channel_id)
    print(f"📺 Videos in RSS: {len(videos)}")
    total_fetched += len(videos)
    rss_videos.extend(videos)

# 2. Filter out Existing IDs (Local Check)
candidates = []
for v in rss_videos:
    if v["video_id"] in existing_ids:
        total_skipped_existing += 1
        continue
    if any(c["video_id"] == v["video_id"] for c in candidates):
        continue
    candidates.append(v)

print(f"\n📝 Candidates after DB check: {len(candidates)}")

if not candidates:
    print("✅ No new videos to process.")
    sys.exit(0)

candidate_ids = [v["video_id"] for v in candidates]

# 3. Check Live Status (API Call 1 - Batched)
print("\n📡 Checking Live/Upcoming status...")
live_ids_to_exclude = get_live_status_batch(candidate_ids)
total_skipped_live = len(live_ids_to_exclude)

vod_candidates = [v for v in candidates if v["video_id"] not in live_ids_to_exclude]
vod_candidate_ids = [v["video_id"] for v in vod_candidates]

print(f"📉 Remaining after Live filter: {len(vod_candidates)}")

if not vod_candidates:
    print("✅ No videos remaining after live check.")
    sys.exit(0)

# 4. Check Durations (API Call 2 - Batched)
print("\n⏱️ Checking Durations...")
duration_map = fetch_durations_batch(vod_candidate_ids)

# 5. Insert Final Videos
print("\n🚀 Starting Final Filtering & Firebase Insertion...")
for v in vod_candidates:
    vid = v["video_id"]
    duration = duration_map.get(vid, 0)
    title = v["title"]
    
    # --- FILTER 1: Title Keywords (Regex Whole Word) ---
    found_keyword = False
    for keyword in EXCLUDED_KEYWORDS:
        # \b ensures "ardas" does NOT match "sardara"
        # re.IGNORECASE makes it case insensitive
        pattern = r"\b" + re.escape(keyword) + r"\b"
        if re.search(pattern, title, re.IGNORECASE):
            found_keyword = True
            print(f"🛑 Skipped (Keyword '{keyword}'): {title[:40]}...")
            break
            
    if found_keyword:
        total_skipped_keywords += 1
        continue

    # --- FILTER 2: Duration ---
    if duration < MIN_DURATION_SECONDS:
        print(f"⏭️ Skipped short ({duration}s): {vid}")
        total_skipped_short += 1
        continue

    # --- CHECK FINAL IMAGE URL ---
    final_image_url = get_working_image_url(vid)

    # --- INSERT ---
    # FIXED: Using v["published"] instead of time.time()
    db.collection(COLLECTION_NAME).document().set({
        "title": v["title"],
        "titleLowercase": v["title"].lower(),
        "searchKeywords": generate_search_keywords(v["title"]),
        "url": v["url"],
        "imageUrl": final_image_url,  # <--- Now uses the checked/fallback image
        "timestamp": str(int(time.time() * 1000)),
    })

    existing_ids.add(vid)
    new_ids_added.append(vid)
    total_inserted += 1

    print(f"➕ Inserted ({duration}s): {vid} - {title[:30]}...")
    time.sleep(0.03)

# ---------------- UPDATE ID INDEX ----------------
if new_ids_added:
    print(f"\n💾 Updating {ALL_IDS_DOC} index...")
    ids_doc_ref.set({
        "video_id": list(existing_ids),
        "total_count": len(existing_ids)
    }, merge=True)

# ---------------- SUMMARY ----------------
print("\n================ SUMMARY ================")
print(f"📥 Total RSS Fetched   : {total_fetched}")
print(f"⏭️  Skipped (Existing)  : {total_skipped_existing}")
print(f"🚫 Skipped (Live/Upc)  : {total_skipped_live}")
print(f"🛑 Skipped (Keywords)  : {total_skipped_keywords}")
print(f"✂️  Skipped (Short)     : {total_skipped_short}")
print(f"➕ Videos Inserted     : {total_inserted}")
print(f"📊 New Firebase Total  : {len(existing_ids)}")
print("========================================")
