#!/usr/bin/env python3
import requests
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from bs4 import BeautifulSoup
from datetime import datetime, timezone
import json
import os
import sys
import time
import re

# ---------------- CONFIGURATION ----------------
COLLECTION_GURBANI = "Listen_Kirtans_Videos_New"
COLLECTION_HARMANDIR = "Kirtan-Youtube-Videos"
ALL_IDS_DOC = "-All_Videos_Id"

# Env variables
SERVICE_ACCOUNT_GURBANI = os.environ.get("FIREBASE_SERVICE_ACCOUNT_GURBANI")
SERVICE_ACCOUNT_HARMANDIR = os.environ.get("FIREBASE_SERVICE_ACCOUNT_HARMANDIR")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")

if not SERVICE_ACCOUNT_GURBANI or not SERVICE_ACCOUNT_HARMANDIR or not YOUTUBE_API_KEY:
    print("❌ Missing required Environment Variables.")
    sys.exit(1)

# ---------------- FIREBASE INIT ----------------
print("🔌 Initializing Firebase Connections...")
cred_gurbani = credentials.Certificate(json.loads(SERVICE_ACCOUNT_GURBANI))
app_gurbani = firebase_admin.initialize_app(cred_gurbani, name='gurbani_app_migration')
db_gurbani = firestore.client(app=app_gurbani)

cred_harmandir = credentials.Certificate(json.loads(SERVICE_ACCOUNT_HARMANDIR))
app_harmandir = firebase_admin.initialize_app(cred_harmandir, name='harmandir_app_migration')
db_harmandir = firestore.client(app=app_harmandir)

# ---------------- CACHES ----------------
CHANNEL_LOGO_CACHE = {}

# ---------------- HELPER METHODS ----------------
def chunk_list(data, chunk_size):
    for i in range(0, len(data), chunk_size):
        yield data[i:i + chunk_size]

def parse_iso_duration(duration_iso):
    match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_iso)
    if not match: return "0:00"
    hours, minutes, seconds = match.groups()
    hours = int(hours) if hours else 0
    minutes = int(minutes) if minutes else 0
    seconds = int(seconds) if seconds else 0
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    else:
        return f"{minutes}:{seconds:02d}"

def fetch_channel_logo(channel_id):
    if channel_id in CHANNEL_LOGO_CACHE:
        return CHANNEL_LOGO_CACHE[channel_id]

    channel_url = f"https://www.youtube.com/channel/{channel_id}"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
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
    
    CHANNEL_LOGO_CACHE[channel_id] = ""
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

def fetch_video_details_batch(video_ids):
    """Fetches full details for up to 50 videos at once."""
    details_map = {}
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "part": "snippet,contentDetails,statistics",
        "id": ",".join(video_ids),
        "key": YOUTUBE_API_KEY
    }
    
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        
        for item in data.get("items", []):
            vid = item["id"]
            pub_dt = datetime.fromisoformat(item["snippet"]["publishedAt"].replace("Z", "+00:00")).astimezone(timezone.utc)
            
            details_map[vid] = {
                "channelId": item["snippet"]["channelId"],
                "channelName": item["snippet"]["channelTitle"],
                "title": item["snippet"]["title"],
                "duration_formatted": parse_iso_duration(item["contentDetails"]["duration"]),
                "timeAgo": str(int(pub_dt.timestamp() * 1000)),
                "viewCount": int(item["statistics"].get("viewCount", 0))
            }
    except Exception as e:
        print(f"⚠️ Error fetching video details: {e}")
        
    return details_map

# ---------------- MAIN LOGIC ----------------

print(f"\n📖 Fetching the master ID list from Harmandir App...")
doc_harmandir = db_harmandir.collection(COLLECTION_HARMANDIR).document(ALL_IDS_DOC).get()

if not doc_harmandir.exists:
    print(f"❌ Could not find document {ALL_IDS_DOC}")
    sys.exit(1)

all_video_ids = doc_harmandir.to_dict().get("video_id", [])
print(f"📦 Total IDs found for migration: {len(all_video_ids)}")

if not all_video_ids:
    sys.exit(0)

total_updated = 0
total_skipped_missing_api = 0

print("\n🚀 Starting One-Time Full Database Standardization...")

for chunk_index, id_chunk in enumerate(chunk_list(all_video_ids, 50)):
    print(f"\n🔄 Processing batch {chunk_index + 1} (IDs: {len(id_chunk)})...")
    
    # 1. Fetch full details from YouTube API
    details_map = fetch_video_details_batch(id_chunk)
    
    # 2. Process and standardize each video
    for vid in id_chunk:
        details = details_map.get(vid)
        
        if not details:
            print(f"⚠️ Skipping {vid} - Not found on YouTube (might be deleted/private).")
            total_skipped_missing_api += 1
            continue

        # Prepare all exact fields
        title = details["title"]
        target_url = f"https://www.youtube.com/watch?v={vid}"
        final_image_url = get_working_image_url(vid)
        logo_url = fetch_channel_logo(details["channelId"])
        current_timestamp_ms = str(int(time.time() * 1000))
        
        # ⚠️ STRICT SCHEMA: Exactly the fields you requested, nothing else.
        clean_doc_data = {
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
            "url": target_url,
            "viewCount": details["viewCount"]
        }

        # --- Update Gurbani App DB ---
        gurbani_docs = db_gurbani.collection(COLLECTION_GURBANI).where(filter=FieldFilter("url", "==", target_url)).stream()
        for doc in gurbani_docs:
            # Using .set() OVERWRITES the document. Old fields like 'searchKeywords' are deleted permanently.
            doc.reference.set(clean_doc_data)
            
        # --- Update Harmandir Sahib App DB ---
        harmandir_docs = db_harmandir.collection(COLLECTION_HARMANDIR).where(filter=FieldFilter("url", "==", target_url)).stream()
        for doc in harmandir_docs:
            # Using .set() OVERWRITES the document.
            doc.reference.set(clean_doc_data)

        total_updated += 1
        print(f"✅ Re-written clean schema for: {vid}")

print("\n================ SUMMARY ================")
print(f"🎯 Total Videos Processed: {len(all_video_ids)}")
print(f"✅ Successfully Cleaned  : {total_updated} videos in both databases")
print(f"⚠️ Skipped (No YT Data)  : {total_skipped_missing_api}")
print("========================================")
print("🎉 One-time schema migration completed successfully!")
