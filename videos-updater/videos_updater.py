#!/usr/bin/env python3
import requests
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter
import json
import os
import sys

# ---------------- CONFIGURATION ----------------

# ⚙️ Adjustable value: How many of the latest videos to update?
# It grabs from the bottom of the array (latest added). Set to 500 as requested.
VIDEOS_TO_UPDATE_COUNT = 1000  

# Database Collections
COLLECTION_GURBANI = "Listen_Kirtans_Videos_New"
COLLECTION_HARMANDIR = "Kirtan-Youtube-Videos"
ALL_IDS_DOC = "-All_Videos_Id"

# Env variables for BOTH service accounts & YouTube API
SERVICE_ACCOUNT_GURBANI = os.environ.get("FIREBASE_SERVICE_ACCOUNT_GURBANI")
SERVICE_ACCOUNT_HARMANDIR = os.environ.get("FIREBASE_SERVICE_ACCOUNT_HARMANDIR")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")

if not SERVICE_ACCOUNT_GURBANI or not SERVICE_ACCOUNT_HARMANDIR:
    print("❌ FIREBASE_SERVICE_ACCOUNT env vars missing for one or both apps")
    sys.exit(1)

if not YOUTUBE_API_KEY:
    print("❌ YOUTUBE_API_KEY env var missing")
    sys.exit(1)

# ---------------- FIREBASE DUAL INIT ----------------
print("🔌 Initializing Firebase Connections...")

# App 1: Gurbani
cred_gurbani = credentials.Certificate(json.loads(SERVICE_ACCOUNT_GURBANI))
app_gurbani = firebase_admin.initialize_app(cred_gurbani, name='gurbani_app_updater')
db_gurbani = firestore.client(app=app_gurbani)

# App 2: Harmandir Sahib
cred_harmandir = credentials.Certificate(json.loads(SERVICE_ACCOUNT_HARMANDIR))
app_harmandir = firebase_admin.initialize_app(cred_harmandir, name='harmandir_app_updater')
db_harmandir = firestore.client(app=app_harmandir)

# ---------------- HELPER METHODS ----------------
def chunk_list(data, chunk_size):
    """Yield successive chunks from a list."""
    for i in range(0, len(data), chunk_size):
        yield data[i:i + chunk_size]

def fetch_youtube_views_batch(video_ids):
    """Fetches ONLY view statistics for up to 50 videos in a SINGLE API call."""
    views_map = {}
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "part": "statistics",
        "id": ",".join(video_ids),
        "key": YOUTUBE_API_KEY
    }
    
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        
        for item in data.get("items", []):
            vid = item["id"]
            # Safely get view count, default to 0 if missing
            view_count = int(item["statistics"].get("viewCount", 0))
            views_map[vid] = view_count
            
        return views_map, True # API Success
            
    except Exception as e:
        print(f"⚠️ Error fetching video stats from YouTube: {e}")
        return views_map, False # API Failed

# ---------------- MAIN LOGIC ----------------

print(f"\n📖 Fetching the global ID list from Harmandir App...")
# Read Harmandir DB to get the array of all video IDs
doc_harmandir = db_harmandir.collection(COLLECTION_HARMANDIR).document(ALL_IDS_DOC).get()

if not doc_harmandir.exists:
    print(f"❌ Could not find document {ALL_IDS_DOC} in {COLLECTION_HARMANDIR}")
    sys.exit(1)

all_video_ids = doc_harmandir.to_dict().get("video_id", [])
total_ids_available = len(all_video_ids)

print(f"📦 Total IDs found in database: {total_ids_available}")

# Slice the array to get the latest 'N' videos (from bottom to top)
# Using python list slicing: list[-N:] gets the last N elements
target_ids = all_video_ids[-VIDEOS_TO_UPDATE_COUNT:] if total_ids_available > VIDEOS_TO_UPDATE_COUNT else all_video_ids

print(f"🎯 Selected the latest {len(target_ids)} videos for view count updates.")

if not target_ids:
    print("✅ No IDs to update.")
    sys.exit(0)

# Counters
total_updated_gurbani = 0
total_updated_harmandir = 0
total_deleted_videos = 0

print("\n🚀 Starting YouTube API Fetch & Firestore Updates...")

# Process in chunks of 50
for chunk_index, id_chunk in enumerate(chunk_list(target_ids, 50)):
    print(f"\n🔄 Processing batch {chunk_index + 1} (IDs: {len(id_chunk)})...")
    
    # 1. Get updated view counts from YouTube
    views_map, api_success = fetch_youtube_views_batch(id_chunk)
    
    if not api_success:
        print("⚠️ Skipping this batch due to API error (protecting against accidental deletes).")
        continue
    
    # 2. Update Firebase Databases (Existing Videos)
    for vid, new_view_count in views_map.items():
        target_url = f"https://www.youtube.com/watch?v={vid}"
        
        # --- Update Gurbani App DB ---
        gurbani_docs = db_gurbani.collection(COLLECTION_GURBANI).where(filter=FieldFilter("url", "==", target_url)).stream()
        for doc in gurbani_docs:
            doc.reference.update({"viewCount": new_view_count})
            total_updated_gurbani += 1
            
        # --- Update Harmandir Sahib App DB ---
        harmandir_docs = db_harmandir.collection(COLLECTION_HARMANDIR).where(filter=FieldFilter("url", "==", target_url)).stream()
        for doc in harmandir_docs:
            doc.reference.update({"viewCount": new_view_count})
            total_updated_harmandir += 1
            
        print(f"👁️ Updated {vid} -> {new_view_count} views")

    # 3. Clean Up Deleted/Private Videos
    # Any ID in our chunk that did not get returned by the API is no longer available.
    missing_ids = set(id_chunk) - set(views_map.keys())
    
    for vid in missing_ids:
        target_url = f"https://www.youtube.com/watch?v={vid}"
        print(f"🗑️ Video {vid} is missing/private. Deleting from databases...")
        
        # --- Delete from Gurbani DB ---
        gurbani_docs = db_gurbani.collection(COLLECTION_GURBANI).where(filter=FieldFilter("url", "==", target_url)).stream()
        for doc in gurbani_docs:
            doc.reference.delete()
        
        # Remove from Gurbani array index
        db_gurbani.collection(COLLECTION_GURBANI).document(ALL_IDS_DOC).update({
            "video_id": firestore.ArrayRemove([vid]),
            "total_count": firestore.Increment(-1)
        })

        # --- Delete from Harmandir DB ---
        harmandir_docs = db_harmandir.collection(COLLECTION_HARMANDIR).where(filter=FieldFilter("url", "==", target_url)).stream()
        for doc in harmandir_docs:
            doc.reference.delete()
            
        # Remove from Harmandir array index
        db_harmandir.collection(COLLECTION_HARMANDIR).document(ALL_IDS_DOC).update({
            "video_id": firestore.ArrayRemove([vid]),
            "total_count": firestore.Increment(-1)
        })
        
        total_deleted_videos += 1

# ---------------- SUMMARY ----------------
print("\n================ SUMMARY ================")
print(f"🎯 Target Videos to Update : {len(target_ids)}")
print(f"✅ Docs Updated (Gurbani)  : {total_updated_gurbani}")
print(f"✅ Docs Updated (Harmandir): {total_updated_harmandir}")
print(f"🗑️  Videos Deleted         : {total_deleted_videos}")
print("========================================")
print("🎉 View counts updated and cleanup finished successfully!")
