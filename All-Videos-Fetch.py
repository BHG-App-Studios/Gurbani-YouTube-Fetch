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

# Database Configurations
COLLECTION_GURBANI = "Listen_Kirtans_Videos_New"
COLLECTION_HARMANDIR = "Kirtan-Youtube-Videos"
ALL_IDS_DOC = "-All_Videos_Id"
MIN_DURATION_SECONDS = 180  # ⏱️ 3 minutes

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

# ---------------- READ EXISTING IDS (2 READS) ----------------
print("📖 Fetching existing Video IDs from both databases...")

# Read Gurbani DB
doc_gurbani = db_gurbani.collection(COLLECTION_GURBANI).document(ALL_IDS_DOC).get()
existing_ids_gurbani = set(doc_gurbani.to_dict().get("video_id", [])) if doc_gurbani.exists else set()

# Read Harmandir DB
doc_harmandir = db_harmandir.collection(COLLECTION_HARMANDIR).document(ALL_IDS_DOC).get()
existing_ids_harmandir = set(doc_harmandir.to_dict().get("video_id", [])) if doc_harmandir.exists else set()

print(f"📦 Existing in Gurbani App: {len(existing_ids_gurbani)}")
print(f"📦 Existing in Harmandir App: {len(existing_ids_harmandir)}")

# ---------------- COUNTERS ----------------
total_fetched = 0
total_skipped_existing = 0
total_skipped_live = 0
total_skipped_short = 0
total_skipped_keywords = 0
total_inserted_gurbani = 0
total_inserted_harmandir = 0
new_ids_gurbani = []
new_ids_harmandir = []

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

# ---------------- HELPER METHODS ----------------
def chunk_list(data, chunk_size):
    for i in range(0, len(data), chunk_size):
        yield data[i:i + chunk_size]

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

def get_working_image_url(video_id):
    maxres_url = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
    fallback_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault_live.jpg"
    try:
        response = requests.head(maxres_url, timeout=5)
        if response.status_code == 200:
            return maxres_url
    except Exception:
        pass
    return fallback_url

def generate_search_keywords(title):
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

# ---------------- MAIN LOGIC ----------------
rss_videos = []

# 1. Gather all videos from RSS
for channel_id in CHANNEL_IDS:
    print(f"\n🔍 Fetching channel: {channel_id}")
    videos = fetch_videos_from_channel(channel_id)
    total_fetched += len(videos)
    rss_videos.extend(videos)

# 2. Filter out Existing IDs 
# Only process a video if it is missing in AT LEAST ONE of the databases
candidates = []
for v in rss_videos:
    vid = v["video_id"]
    if vid in existing_ids_gurbani and vid in existing_ids_harmandir:
        total_skipped_existing += 1
        continue
    if any(c["video_id"] == vid for c in candidates):
        continue
    candidates.append(v)

print(f"\n📝 Candidates needing processing (missing in at least one DB): {len(candidates)}")

if not candidates:
    print("✅ No new videos to process for either database.")
    sys.exit(0)

candidate_ids = [v["video_id"] for v in candidates]

# 3. Check Live Status (API Call 1)
print("\n📡 Checking Live/Upcoming status...")
live_ids_to_exclude = get_live_status_batch(candidate_ids)
total_skipped_live = len(live_ids_to_exclude)

vod_candidates = [v for v in candidates if v["video_id"] not in live_ids_to_exclude]
vod_candidate_ids = [v["video_id"] for v in vod_candidates]

if not vod_candidates:
    print("✅ No videos remaining after live check.")
    sys.exit(0)

# 4. Check Durations (API Call 2)
print("\n⏱️ Checking Durations...")
duration_map = fetch_durations_batch(vod_candidate_ids)

# 5. Insert Final Videos into Respective DBs
print("\n🚀 Starting Final Filtering & Firebase Insertion...")
for v in vod_candidates:
    vid = v["video_id"]
    duration = duration_map.get(vid, 0)
    title = v["title"]
    
    # --- FILTER 1: Title Keywords ---
    found_keyword = False
    for keyword in EXCLUDED_KEYWORDS:
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

    # --- PREPARE DATA ---
    final_image_url = get_working_image_url(vid)
    doc_data = {
        "title": v["title"],
        "titleLowercase": v["title"].lower(),
        "searchKeywords": generate_search_keywords(v["title"]),
        "url": v["url"],
        "imageUrl": final_image_url,
        "timestamp": str(int(time.time() * 1000)),
    }

    inserted_any = False

    # Insert into Gurbani App DB if it's not already there
    if vid not in existing_ids_gurbani:
        db_gurbani.collection(COLLECTION_GURBANI).document().set(doc_data)
        existing_ids_gurbani.add(vid)
        new_ids_gurbani.append(vid)
        total_inserted_gurbani += 1
        inserted_any = True

    # Insert into Harmandir Sahib App DB if it's not already there
    if vid not in existing_ids_harmandir:
        db_harmandir.collection(COLLECTION_HARMANDIR).document().set(doc_data)
        existing_ids_harmandir.add(vid)
        new_ids_harmandir.append(vid)
        total_inserted_harmandir += 1
        inserted_any = True

    if inserted_any:
        print(f"➕ Inserted ({duration}s): {vid} - {title[:30]}...")
        time.sleep(0.03)

# ---------------- UPDATE ID INDEXES ----------------
if new_ids_gurbani:
    print(f"\n💾 Updating {ALL_IDS_DOC} index for Gurbani App...")
    db_gurbani.collection(COLLECTION_GURBANI).document(ALL_IDS_DOC).set({
        "video_id": list(existing_ids_gurbani),
        "total_count": len(existing_ids_gurbani)
    }, merge=True)

if new_ids_harmandir:
    print(f"💾 Updating {ALL_IDS_DOC} index for Harmandir App...")
    db_harmandir.collection(COLLECTION_HARMANDIR).document(ALL_IDS_DOC).set({
        "video_id": list(existing_ids_harmandir),
        "total_count": len(existing_ids_harmandir)
    }, merge=True)

# ---------------- SUMMARY ----------------
print("\n================ SUMMARY ================")
print(f"📥 Total RSS Fetched        : {total_fetched}")
print(f"⏭️  Skipped (In Both DBs)   : {total_skipped_existing}")
print(f"🚫 Skipped (Live/Upc)       : {total_skipped_live}")
print(f"🛑 Skipped (Keywords)       : {total_skipped_keywords}")
print(f"✂️  Skipped (Short)         : {total_skipped_short}")
print(f"➕ Inserted to Gurbani     : {total_inserted_gurbani} (Total: {len(existing_ids_gurbani)})")
print(f"➕ Inserted to Harmandir   : {total_inserted_harmandir} (Total: {len(existing_ids_harmandir)})")
print("========================================")
