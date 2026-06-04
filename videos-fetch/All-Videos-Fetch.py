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
print("\n📖 Fetching existing Video IDs from both databases...")

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
total_skipped_duplicate_titles = 0
total_inserted_gurbani = 0
total_inserted_harmandir = 0
new_ids_gurbani = []
new_ids_harmandir = []

# Cache for channel logos to avoid redundant scraping
CHANNEL_LOGO_CACHE = {}

# ---------------- HELPER METHODS ----------------
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

def chunk_list(data, chunk_size):
    for i in range(0, len(data), chunk_size):
        yield data[i:i + chunk_size]

def parse_iso_duration(duration_iso):
    """Converts ISO 8601 duration (e.g., PT8M33S) to MM:SS format."""
    match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_iso)
    if not match: 
        return "0:00"
        
    hours, minutes, seconds = match.groups()
    hours = int(hours) if hours else 0
    minutes = int(minutes) if minutes else 0
    seconds = int(seconds) if seconds else 0
    
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    else:
        return f"{minutes}:{seconds:02d}"

def iso8601_to_seconds(duration):
    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration)
    if not match: return 0
    h = int(match.group(1) or 0)
    m = int(match.group(2) or 0)
    s = int(match.group(3) or 0)
    return h * 3600 + m * 60 + s

def fetch_video_details_batch(video_ids):
    """Fetches comprehensive details AND live status for up to 50 videos in a SINGLE API call."""
    details_map = {}
    CHUNK_SIZE = 50 
    for chunk in chunk_list(video_ids, CHUNK_SIZE):
        url = "https://www.googleapis.com/youtube/v3/videos"
        params = {
            "part": "snippet,contentDetails,statistics",
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
                
                # Live Status
                broadcast_content = item["snippet"].get("liveBroadcastContent", "none")
                
                # Duration
                iso_duration = item["contentDetails"]["duration"]
                duration_sec = iso8601_to_seconds(iso_duration)
                duration_formatted = parse_iso_duration(iso_duration)
                
                # Timestamps
                pub_dt = datetime.fromisoformat(item["snippet"]["publishedAt"].replace("Z", "+00:00")).astimezone(timezone.utc)
                time_ago_ms = str(int(pub_dt.timestamp() * 1000))
                
                # Views
                view_count = int(item["statistics"].get("viewCount", 0))

                details_map[vid] = {
                    "liveBroadcastContent": broadcast_content,
                    "duration_sec": duration_sec,
                    "duration_formatted": duration_formatted,
                    "channelName": item["snippet"]["channelTitle"],
                    "channelId": item["snippet"]["channelId"],
                    "title": item["snippet"]["title"],
                    "timeAgo": time_ago_ms,
                    "viewCount": view_count
                }
        except Exception as e:
            print(f"⚠️ Error fetching video details: {e}")
    return details_map

def fetch_channel_logo(channel_id):
    """Scrapes the channel HTML for the logo. Uses cache to prevent multiple requests."""
    if channel_id in CHANNEL_LOGO_CACHE:
        return CHANNEL_LOGO_CACHE[channel_id]

    channel_url = f"https://www.youtube.com/channel/{channel_id}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    try:
        response = requests.get(channel_url, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        meta_image = soup.find('meta', property='og:image')
        if meta_image and meta_image.get('content'):
            img_url = meta_image['content']
            CHANNEL_LOGO_CACHE[channel_id] = img_url
            return img_url
    except Exception as e:
        print(f"❌ Error scraping logo for {channel_id}: {e}")
    
    CHANNEL_LOGO_CACHE[channel_id] = "" # Prevent retries on failure
    return ""

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
    
# ---------------- MAIN LOGIC PIPELINE ----------------

# 1. Gather all videos from RSS
print("\n---------------- STARTING RSS FETCH ----------------")
rss_videos = []
for channel_id in CHANNEL_IDS:
    print(f"🔍 Fetching channel: {channel_id}")
    videos = fetch_videos_from_channel(channel_id)
    total_fetched += len(videos)
    rss_videos.extend(videos)

# 2. Local Filters (ID & Keyword Exclusions - NO API COST)
print("\n🧹 Filtering out existing DB videos and bad keywords locally...")
candidates_for_api = []
seen_rss_ids = set()

for v in rss_videos:
    vid = v["video_id"]
    title = v["title"]

    # Filter A: Existing in DB Check
    if vid in existing_ids_gurbani and vid in existing_ids_harmandir:
        total_skipped_existing += 1
        continue
        
    if vid in seen_rss_ids:
        continue

    # Filter B: Bad Keywords Check 
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
        
    # Filter C: Fast Shorts Hack (Drops obvious shorts before API check)
    if "#shorts" in title.lower():
        print(f"✂️ Skipped (Obvious Short in Title): {title[:40]}...")
        total_skipped_short += 1
        continue

    candidates_for_api.append(v)
    seen_rss_ids.add(vid)

print(f"\n📝 Candidates surviving local filters needing API checking: {len(candidates_for_api)}")

if not candidates_for_api:
    print("✅ No new valid videos to process for either database.")
    sys.exit(0)

# 3. Fetch Complete Video Details (API Call - Chunks of 50)
print("\n⏱️ Fetching Full Video Details & Live Status (via YouTube API)...")
candidate_ids = [v["video_id"] for v in candidates_for_api]
details_map = fetch_video_details_batch(candidate_ids)

# 4. Final Filters & Firebase Insertion
print("\n🚀 Starting Final API Filtering & Firebase Insertion...")
current_timestamp_ms = str(int(time.time() * 1000))
seen_final_titles = set()

for v in candidates_for_api:
    vid = v["video_id"]
    details = details_map.get(vid)
    
    if not details:
        print(f"⚠️ Skipping {vid} because API returned no details.")
        continue

    title = details["title"]

    # --- FINAL API FILTER 1: Live Status ---
    if details["liveBroadcastContent"] in ["live", "upcoming"]:
        print(f"🚫 Skipped (Live/Upcoming stream): {vid}")
        total_skipped_live += 1
        continue

    # --- FINAL API FILTER 2: Duration Check ---
    duration_sec = details["duration_sec"]
    if duration_sec < MIN_DURATION_SECONDS:
        print(f"⏭️ Skipped short ({duration_sec}s): {vid}")
        total_skipped_short += 1
        continue
        
    # --- FINAL API FILTER 3: Title Deduplication ---
    # Prevents two identical videos uploaded by different channels from both being pushed
    if title in seen_final_titles:
        print(f"👯 Skipped Duplicate Title: {title[:40]}...")
        total_skipped_duplicate_titles += 1
        continue
    seen_final_titles.add(title)

    # --- PREPARE DATA BASE (Shared by both) ---
    final_image_url = get_working_image_url(vid)
    logo_url = fetch_channel_logo(details["channelId"])
    
    base_doc_data = {
        "channelLogoUrl": logo_url,
        "channelName": details["channelName"],
        "channel_id": details["channelId"],
        "duration": details["duration_formatted"],
        "imageUrl": final_image_url,
        "isLive": False,
        "timeAgo": details["timeAgo"],
        "timestamp": current_timestamp_ms,
        "title": title,
        "titleLowercase": title.lower(),
        "url": f"https://www.youtube.com/watch?v={vid}",
        "viewCount": details["viewCount"]
    }

    inserted_any = False

    # Insert into Gurbani App DB
    if vid not in existing_ids_gurbani:
        # 1. Create a reference to get the auto-generated Document ID
        doc_ref_gurbani = db_gurbani.collection(COLLECTION_GURBANI).document()
        
        # 2. Set the data into the video collection
        doc_ref_gurbani.set(base_doc_data)
        
        # 3. Safely update Search_Collection -> streams with the new ID and lowercase title
        db_gurbani.collection("Search_Collection").document("streams").set({
            doc_ref_gurbani.id: base_doc_data["titleLowercase"]
        }, merge=True)

        existing_ids_gurbani.add(vid)
        new_ids_gurbani.append(vid)
        total_inserted_gurbani += 1
        inserted_any = True

    # Insert into Harmandir Sahib App DB
    if vid not in existing_ids_harmandir:
        # 1. Create a reference to get the auto-generated Document ID
        doc_ref_harmandir = db_harmandir.collection(COLLECTION_HARMANDIR).document()
        
        # 2. Set the data into the video collection
        doc_ref_harmandir.set(base_doc_data)
        
        # 3. Safely update Search_Collection -> streams with the new ID and lowercase title
        db_harmandir.collection("Search_Collection").document("streams").set({
            doc_ref_harmandir.id: base_doc_data["titleLowercase"]
        }, merge=True)

        existing_ids_harmandir.add(vid)
        new_ids_harmandir.append(vid)
        total_inserted_harmandir += 1
        inserted_any = True

    if inserted_any:
        print(f"➕ Inserted ({details['duration_formatted']}): {vid} - {title[:30]}...")
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
print(f"🛑 Skipped (Bad Keywords)   : {total_skipped_keywords}")
print(f"✂️  Skipped (Shorts)        : {total_skipped_short}")
print(f"🚫 Skipped (Live/Upc)       : {total_skipped_live}")
print(f"👯 Skipped (Duplicate Title): {total_skipped_duplicate_titles}")
print(f"➕ Inserted to Gurbani     : {total_inserted_gurbani} (Total: {len(existing_ids_gurbani)})")
print(f"➕ Inserted to Harmandir   : {total_inserted_harmandir} (Total: {len(existing_ids_harmandir)})")
print("========================================")