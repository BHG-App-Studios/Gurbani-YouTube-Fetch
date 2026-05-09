#!/usr/bin/env python3
import requests
import firebase_admin
from firebase_admin import credentials, firestore
from bs4 import BeautifulSoup
import json
import os
import sys
import time
import re
from datetime import datetime, timezone

# ---------------- CONFIG (Same as your fetch script) ----------------
COLLECTION_GURBANI = "Listen_Kirtans_Videos_New"
COLLECTION_HARMANDIR = "Kirtan-Youtube-Videos"
ALL_IDS_DOC = "-All_Videos_Id"

SERVICE_ACCOUNT_GURBANI = os.environ.get("FIREBASE_SERVICE_ACCOUNT_GURBANI")
SERVICE_ACCOUNT_HARMANDIR = os.environ.get("FIREBASE_SERVICE_ACCOUNT_HARMANDIR")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")

REQUIRED_FIELDS = [
    "viewCount", "duration", "channelName", "url", "imageUrl", 
    "title", "titleLowercase", "timeAgo", "channel_id", 
    "isLive", "channelLogoUrl", "timestamp"
]

# ---------------- INITIALIZATION ----------------
if not all([SERVICE_ACCOUNT_GURBANI, SERVICE_ACCOUNT_HARMANDIR, YOUTUBE_API_KEY]):
    print("❌ Missing Environment Variables.")
    sys.exit(1)

# App 1: Gurbani
cred_gurbani = credentials.Certificate(json.loads(SERVICE_ACCOUNT_GURBANI))
app_gurbani = firebase_admin.initialize_app(cred_gurbani, name='gurbani_fix')
db_gurbani = firestore.client(app=app_gurbani)

# App 2: Harmandir Sahib
cred_harmandir = credentials.Certificate(json.loads(SERVICE_ACCOUNT_HARMANDIR))
app_harmandir = firebase_admin.initialize_app(cred_harmandir, name='harmandir_fix')
db_harmandir = firestore.client(app=app_harmandir)

CHANNEL_LOGO_CACHE = {}

# ---------------- HELPERS ----------------

def extract_video_id(url):
    """Extracts ID from watch?v= or mobile urls."""
    match = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11}).*", url)
    return match.group(1) if match else None

def parse_iso_duration(duration_iso):
    match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_iso)
    if not match: return "0:00"
    h, m, s = [int(x) if x else 0 for x in match.groups()]
    return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m}:{s:02d}"

def fetch_channel_logo(channel_id):
    if channel_id in CHANNEL_LOGO_CACHE: return CHANNEL_LOGO_CACHE[channel_id]
    url = f"https://www.youtube.com/channel/{channel_id}"
    try:
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        soup = BeautifulSoup(r.text, 'html.parser')
        meta = soup.find('meta', property='og:image')
        if meta:
            CHANNEL_LOGO_CACHE[channel_id] = meta['content']
            return meta['content']
    except: pass
    return ""

def get_working_image_url(video_id):
    url = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
    try:
        if requests.head(url, timeout=5).status_code == 200: return url
    except: pass
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

def fetch_youtube_details(video_ids):
    """Batched API call for 50 videos."""
    details_map = {}
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i+50]
        params = {
            "part": "snippet,contentDetails,statistics",
            "id": ",".join(chunk),
            "key": YOUTUBE_API_KEY
        }
        r = requests.get("https://www.googleapis.com/youtube/v3/videos", params=params)
        data = r.json()
        for item in data.get("items", []):
            vid = item["id"]
            pub_dt = datetime.fromisoformat(item["snippet"]["publishedAt"].replace("Z", "+00:00"))
            details_map[vid] = {
                "viewCount": int(item["statistics"].get("viewCount", 0)),
                "duration": parse_iso_duration(item["contentDetails"]["duration"]),
                "channelName": item["snippet"]["channelTitle"],
                "channel_id": item["snippet"]["channelId"],
                "title": item["snippet"]["title"],
                "isLive": item["snippet"].get("liveBroadcastContent") != "none",
                "timeAgo": str(int(pub_dt.timestamp() * 1000))
            }
    return details_map

# ---------------- PROCESSING ENGINE ----------------

def cleanup_database(db, collection_name):
    print(f"\n--- Processing Collection: {collection_name} ---")
    docs = db.collection(collection_name).stream()
    
    dirty_docs = [] # List of (doc_id, video_id)
    
    for doc in docs:
        if doc.id == ALL_IDS_DOC: continue
        
        data = doc.to_dict()
        # Check if any required field is missing
        is_incomplete = any(field not in data or data[field] is None for field in REQUIRED_FIELDS)
        
        if is_incomplete:
            url = data.get("url")
            if url:
                vid = extract_video_id(url)
                if vid:
                    dirty_docs.append((doc.id, vid))

    if not dirty_docs:
        print("✅ No incomplete documents found.")
        return

    print(f"🔍 Found {len(dirty_docs)} incomplete documents. Fetching data...")

    # Extract just the video IDs for the API call
    vids_to_fetch = list(set([item[1] for item in dirty_docs]))
    api_data = fetch_youtube_details(vids_to_fetch)

    updated_count = 0
    for doc_id, vid in dirty_docs:
        if vid in api_data:
            info = api_data[vid]
            logo = fetch_channel_logo(info["channel_id"])
            img = get_working_image_url(vid)
            
            # Prepare the full 12-field payload
            update_payload = {
                "viewCount": info["viewCount"],
                "duration": info["duration"],
                "channelName": info["channelName"],
                "url": f"https://www.youtube.com/watch?v={vid}",
                "imageUrl": img,
                "title": info["title"],
                "titleLowercase": info["title"].lower(),
                "timeAgo": info["timeAgo"],
                "channel_id": info["channel_id"],
                "isLive": info["isLive"],
                "channelLogoUrl": logo,
                "timestamp": str(int(time.time() * 1000))
            }
            
            db.collection(collection_name).document(doc_id).update(update_payload)
            updated_count += 1
            if updated_count % 10 == 0:
                print(f"✔ Updated {updated_count} documents...")

    print(f"🎉 Cleanup Complete! Updated {updated_count} docs in {collection_name}.")

# ---------------- EXECUTION ----------------
if __name__ == "__main__":
    print("🚀 Starting Database Cleanup...")
    
    # Run for Gurbani App
    cleanup_database(db_gurbani, COLLECTION_GURBANI)
    
    # Run for Harmandir App
    cleanup_database(db_harmandir, COLLECTION_HARMANDIR)
    
    print("\n✅ All databases are now consistent and up to date.")
