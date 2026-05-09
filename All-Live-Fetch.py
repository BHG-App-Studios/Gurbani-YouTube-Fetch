#!/usr/bin/env python3
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import firebase_admin
from firebase_admin import credentials, firestore
from bs4 import BeautifulSoup
import json
import os
import sys
import time
import re

# ---------------- CONFIG ----------------
CHANNEL_IDS = [
    "UC_JnnWTC6gHc59JwfMPTjdw",
    "UCbRd_XngDfbGh_7G8A8O0vg",
    "UC5Avhz91GejfLe4sRjm0vhg",
    "UCOI-UyamQwCeKA-VVA2XwYw",
    "UCf2HOqXWwpBbiSWTqXLWteA",
    "UCLMfeT_BVADvx_sTybotSLA",
    "UC884UDwNldmpdEiS1mgtijA",
    "UCl4vnsAZHUJwk0aQsLwg1Aw",
    "UCgoRpla8ubv-Mn7LrzL6Uzw",
    "UCcMsjQs6pMLQWbW3ufhz1SQ",
    "UCQroafhIKCxeQ0e9jj-O51Q",
    "UC5OVS6FMiPPoUgX2YyMFjgA",
    "UCYjgDyvhHZXZ4YOw-9vUNLg",
    "UCbOzsgzfviQNcVUe5N4NjTw",
    "UCUjIneSnBylQOqAk7n7i33A",
    "UC1wecYlMxn33DPHrhHHUyVw",
    "UCh0LDn5Drt44tITPoQiiJ6Q",
    "UCBe8nwY2SqWlrGKKcmxB0_w",
    "UChfKn8lKy182G8m6GZ_ATDw",
    "UCsRllxbDm0oxskk8VI4CmBA",
]

# 🚫 Keywords to exclude (Case Insensitive, Whole Words Only)
EXCLUDED_KEYWORDS = [
    "antim ardaas", "bhog", "bhogg", "antim", "course", "patna sahib", "patna",
]

# Database Configurations (Updated to target live streams)
COLLECTION_NAME = "liveStreams_More"
ALL_IDS_DOC = "-All_Live_Videos_Id"  

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

cred_gurbani = credentials.Certificate(json.loads(SERVICE_ACCOUNT_GURBANI))
app_gurbani = firebase_admin.initialize_app(cred_gurbani, name='gurbani_app')
db_gurbani = firestore.client(app=app_gurbani)

cred_harmandir = credentials.Certificate(json.loads(SERVICE_ACCOUNT_HARMANDIR))
app_harmandir = firebase_admin.initialize_app(cred_harmandir, name='harmandir_app')
db_harmandir = firestore.client(app=app_harmandir)


# ---------------- HELPER METHODS ----------------
def fetch_channel_logo(channel_id):
    """Scrapes the channel HTML for the logo (Cost: 0 Units)"""
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

def chunk_list(data, chunk_size):
    for i in range(0, len(data), chunk_size):
        yield data[i:i + chunk_size]

def get_live_streams_details_batch(video_ids):
    """Checks live status and grabs statistics & channel info (Cost: 1 Unit per 50 videos)"""
    active_live_details = {}
    CHUNK_SIZE = 50 
    
    for chunk in chunk_list(video_ids, CHUNK_SIZE):
        url = "https://www.googleapis.com/youtube/v3/videos"
        params = {
            "part": "snippet,statistics",
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
                
                # ONLY grab videos that are actively "live"
                if broadcast_content == "live":
                    active_live_details[vid] = {
                        "channelName": item["snippet"].get("channelTitle", ""),
                        "channelId": item["snippet"].get("channelId", ""),
                        "viewCount": int(item.get("statistics", {}).get("viewCount", 0))
                    }
                    print(f"🔴 Detected Active LIVE stream: {vid}")
        except Exception as e:
            print(f"⚠️ Error checking live status: {e}")
    return active_live_details

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
            "published": published_dt
        })
    return videos

# ---------------- READ EXISTING IDS ----------------
print(f"\n📖 Fetching existing Video IDs from {COLLECTION_NAME}...")

doc_gurbani = db_gurbani.collection(COLLECTION_NAME).document(ALL_IDS_DOC).get()
existing_ids_gurbani = set(doc_gurbani.to_dict().get("video_id", [])) if doc_gurbani.exists else set()

doc_harmandir = db_harmandir.collection(COLLECTION_NAME).document(ALL_IDS_DOC).get()
existing_ids_harmandir = set(doc_harmandir.to_dict().get("video_id", [])) if doc_harmandir.exists else set()

print(f"📦 Existing in Gurbani App: {len(existing_ids_gurbani)}")
print(f"📦 Existing in Harmandir App: {len(existing_ids_harmandir)}")

# ---------------- CLEANUP STALE LIVE STREAMS ----------------
all_existing_ids = existing_ids_gurbani.union(existing_ids_harmandir)
total_deleted_gurbani = 0
total_deleted_harmandir = 0

if all_existing_ids:
    print(f"\n🔄 Checking {len(all_existing_ids)} previously saved live streams...")
    still_live_ids = set(get_live_streams_details_batch(list(all_existing_ids)).keys())
    stale_ids = all_existing_ids - still_live_ids

    if stale_ids:
        print(f"🗑️ Found {len(stale_ids)} streams no longer live. Cleaning up...")
        for vid in stale_ids:
            target_url = f"https://www.youtube.com/watch?v={vid}"
            
            if vid in existing_ids_gurbani:
                existing_ids_gurbani.remove(vid)
                docs = db_gurbani.collection(COLLECTION_NAME).where(filter=firestore.FieldFilter("url", "==", target_url)).stream()
                for doc in docs: doc.reference.delete()
                total_deleted_gurbani += 1
                
            if vid in existing_ids_harmandir:
                existing_ids_harmandir.remove(vid)
                docs = db_harmandir.collection(COLLECTION_NAME).where(filter=firestore.FieldFilter("url", "==", target_url)).stream()
                for doc in docs: doc.reference.delete()
                total_deleted_harmandir += 1

        if total_deleted_gurbani > 0:
            db_gurbani.collection(COLLECTION_NAME).document(ALL_IDS_DOC).set({
                "video_id": list(existing_ids_gurbani), "total_count": len(existing_ids_gurbani)
            }, merge=True)
        if total_deleted_harmandir > 0:
            db_harmandir.collection(COLLECTION_NAME).document(ALL_IDS_DOC).set({
                "video_id": list(existing_ids_harmandir), "total_count": len(existing_ids_harmandir)
            }, merge=True)
    else:
        print("✅ All previously saved streams are still actively live.")

# ---------------- COUNTERS ----------------
total_fetched = 0
total_skipped_no_live_word = 0
total_skipped_existing = 0
total_skipped_keywords = 0
total_skipped_not_live = 0
total_skipped_duplicate_titles = 0
total_inserted_gurbani = 0
total_inserted_harmandir = 0

new_ids_gurbani = []
new_ids_harmandir = []

# ---------------- MAIN LOGIC PIPELINE ----------------

# STEP 1: Gather all videos from RSS
print("\n---------------- STARTING RSS FETCH ----------------")
rss_videos = []
for channel_id in CHANNEL_IDS:
    print(f"🔍 Fetching channel: {channel_id}")
    videos = fetch_videos_from_channel(channel_id)
    total_fetched += len(videos)
    rss_videos.extend(videos)

# STEP 2: The "Live" Word Title Hack & Exclusions (NO API COST YET)
print("\n🧹 Filtering out obvious non-live videos, existing DB videos, and bad keywords...")
candidates_for_api = []
seen_rss_ids = set()

for v in rss_videos:
    vid = v["video_id"]
    title = v["title"]

    # Filter A: The "Live" Word Hack
    if "live" not in title.lower():
        total_skipped_no_live_word += 1
        continue

    # Filter B: Existing in DB Check
    if vid in existing_ids_gurbani and vid in existing_ids_harmandir:
        total_skipped_existing += 1
        continue

    if vid in seen_rss_ids:
        continue

    # Filter C: Excluded Bad Keywords check
    found_keyword = False
    for keyword in EXCLUDED_KEYWORDS:
        pattern = r"\b" + re.escape(keyword) + r"\b"
        if re.search(pattern, title, re.IGNORECASE):
            found_keyword = True
            print(f"🛑 Bad Keyword '{keyword}': {title[:40]}...")
            break
            
    if found_keyword:
        total_skipped_keywords += 1
        continue

    candidates_for_api.append(v)
    seen_rss_ids.add(vid)

print(f"\n📝 Candidates surviving local filters needing API checking: {len(candidates_for_api)}")

if not candidates_for_api:
    print("✅ No new valid candidates found to check against YouTube API.")
    sys.exit(0)

# STEP 3: API Call (REAL Live Check)
print("\n📡 Checking Real Live status & fetching details via YouTube API...")
candidate_ids = [v["video_id"] for v in candidates_for_api]
active_live_details = get_live_streams_details_batch(candidate_ids)

# Keep ONLY the candidates that the API confirms are currently LIVE
live_candidates = [v for v in candidates_for_api if v["video_id"] in active_live_details]
total_skipped_not_live = len(candidates_for_api) - len(live_candidates)

if not live_candidates:
    print("✅ No API-confirmed active live streams found right now.")
    sys.exit(0)

# STEP 4: Title Deduplication
# Note: Because the API check just happened, EVERY video in `live_candidates` is 100% LIVE.
# If we find 3 matching titles, keeping just the 1st one guarantees we are keeping a LIVE one!
print("\n👯 Checking for Duplicate Titles among confirmed Live streams...")
unique_live_candidates = []
seen_titles = set()

for v in live_candidates:
    if v["title"] in seen_titles:
        print(f"👯 Skipped Duplicate Title: {v['title'][:40]}...")
        total_skipped_duplicate_titles += 1
    else:
        seen_titles.add(v["title"])
        unique_live_candidates.append(v)

live_candidates = unique_live_candidates

if not live_candidates:
    print("✅ No unique active live streams found after deduplication.")
    sys.exit(0)

# STEP 5: Firebase Push
print("\n🚀 Starting Firebase Insertion for Final Confirmed Streams...")
channel_logos = {}

for v in live_candidates:
    vid = v["video_id"]
    title = v["title"]
    
    details = active_live_details[vid]
    channel_id = details["channelId"]
    
    if channel_id not in channel_logos:
        channel_logos[channel_id] = fetch_channel_logo(channel_id)
        
    logo_url = channel_logos[channel_id]
    final_image_url = get_working_image_url(vid)
    published_ms = str(int(v["published"].timestamp() * 1000))

    base_doc_data = {
        "channelLogoUrl": logo_url,
        "channelName": details["channelName"],
        "imageUrl": final_image_url,
        "isLive": True,
        "timeAgo": published_ms,
        "title": v["title"],
        "titleLowercase": v["title"].lower(),
        "url": v["url"],
        "viewCount": details["viewCount"],
        "timestamp": str(int(time.time() * 1000)),
        "searchKeywords": generate_search_keywords(v["title"]) # <--- ADDED SEARCH KEYWORDS HERE
    }

    inserted_any = False

    # Insert into Gurbani App DB
    if vid not in existing_ids_gurbani:
        db_gurbani.collection(COLLECTION_NAME).document().set(base_doc_data)
        existing_ids_gurbani.add(vid)
        new_ids_gurbani.append(vid)
        total_inserted_gurbani += 1
        inserted_any = True

    # Insert into Harmandir App DB
    if vid not in existing_ids_harmandir:
        db_harmandir.collection(COLLECTION_NAME).document().set(base_doc_data)
        existing_ids_harmandir.add(vid)
        new_ids_harmandir.append(vid)
        total_inserted_harmandir += 1
        inserted_any = True

    if inserted_any:
        print(f"➕ Inserted LIVE STREAM: {vid} - {title[:30]}...")
        time.sleep(0.03)

# ---------------- UPDATE ID INDEXES ----------------
if new_ids_gurbani:
    print(f"\n💾 Updating {ALL_IDS_DOC} index for Gurbani App...")
    db_gurbani.collection(COLLECTION_NAME).document(ALL_IDS_DOC).set({
        "video_id": list(existing_ids_gurbani),
        "total_count": len(existing_ids_gurbani)
    }, merge=True)

if new_ids_harmandir:
    print(f"💾 Updating {ALL_IDS_DOC} index for Harmandir App...")
    db_harmandir.collection(COLLECTION_NAME).document(ALL_IDS_DOC).set({
        "video_id": list(existing_ids_harmandir),
        "total_count": len(existing_ids_harmandir)
    }, merge=True)

# ---------------- SUMMARY ----------------
print("\n================ SUMMARY ================")
print(f"🗑️  Stale Streams Deleted   : Gurbani: {total_deleted_gurbani} | Harmandir: {total_deleted_harmandir}")
print(f"📥 Total RSS Fetched        : {total_fetched}")
print(f"✂️  Skipped (No 'Live' word): {total_skipped_no_live_word}")
print(f"⏭️  Skipped (Already in DB) : {total_skipped_existing}")
print(f"🛑 Skipped (Bad Keywords)   : {total_skipped_keywords}")
print(f"🗑️  Skipped (API: Not Live) : {total_skipped_not_live}")
print(f"👯 Skipped (Duplicate Title): {total_skipped_duplicate_titles}")
print(f"➕ Inserted to Gurbani     : {total_inserted_gurbani} (Total Live: {len(existing_ids_gurbani)})")
print(f"➕ Inserted to Harmandir   : {total_inserted_harmandir} (Total Live: {len(existing_ids_harmandir)})")
print("========================================")
