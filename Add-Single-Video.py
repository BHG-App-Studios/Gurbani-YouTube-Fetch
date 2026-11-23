#!/usr/bin/env python3
"""
Add-Single-Video.py
Fetches metadata for a single YouTube video URL and saves a new document
with the exact required fields to the selected Firestore collection across
the configured Firebase databases, while strictly skipping Search_Collection.
"""

import os
import sys
import json
import re
import time
import argparse
import random
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter

# ---------------- SERVICE ACCOUNT & API CONFIG ----------------
SERVICE_ACCOUNT_GURBANI = os.environ.get("FIREBASE_SERVICE_ACCOUNT_GURBANI")
SERVICE_ACCOUNT_HARMANDIR = os.environ.get("FIREBASE_SERVICE_ACCOUNT_HARMANDIR")
SERVICE_ACCOUNT_HUKAMNAMA = os.environ.get("FIREBASE_SERVICE_ACCOUNT_HUKAMNAMA")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")

# Mapping of collection names for Harmandir app if they differ from Gurbani/Hukamnama
HARMANDIR_COLLECTION_MAP = {
    "Listen_Kirtans_Videos_New": "Kirtan-Youtube-Videos",
    "liveStreams": "Live-Gurdwaras-YouTube"
}

# ---------------- HELPER METHODS ----------------
def extract_video_id(url_or_id):
    """
    Extracts the 11-character YouTube video ID from various URL formats
    or returns the input if already an 11-character ID.
    """
    if not url_or_id:
        return None
    url_or_id = url_or_id.strip()
    
    # Direct 11-character ID
    if re.fullmatch(r'[a-zA-Z0-9_-]{11}', url_or_id):
        return url_or_id
        
    patterns = [
        r'(?:v=|\/v\/|\/embed\/|\/live\/|\/shorts\/|youtu\.be\/)([a-zA-Z0-9_-]{11})',
        r'[?&]v=([a-zA-Z0-9_-]{11})'
    ]
    for pattern in patterns:
        match = re.search(pattern, url_or_id)
        if match:
            return match.group(1)
    return None

def fetch_channel_logo(channel_id):
    """
    Scrapes the channel HTML for the logo (og:image) with fallback to YouTube Data API.
    """
    if not channel_id:
        return ""
        
    channel_url = f"https://www.youtube.com/channel/{channel_id}"
    print(f"🖼️ Scraping channel logo from: {channel_url}...")
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9'
    }
    try:
        response = requests.get(channel_url, headers=headers, timeout=15)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            meta_image = soup.find('meta', property='og:image')
            if meta_image and meta_image.get('content'):
                logo_url = meta_image['content']
                print(f"✅ Logo found via scraping: {logo_url}")
                return logo_url
    except Exception as e:
        print(f"⚠️ Channel scraping encountered an error: {e}")

    # Fallback to YouTube API channel snippet
    if YOUTUBE_API_KEY and channel_id:
        try:
            print("🔄 Fetching channel logo via YouTube API fallback...")
            api_url = f"https://www.googleapis.com/youtube/v3/channels?part=snippet&id={channel_id}&key={YOUTUBE_API_KEY}"
            resp = requests.get(api_url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("items", [])
                if items:
                    thumbs = items[0]["snippet"].get("thumbnails", {})
                    logo = (
                        thumbs.get("high", {}).get("url") or
                        thumbs.get("medium", {}).get("url") or
                        thumbs.get("default", {}).get("url")
                    )
                    if logo:
                        print(f"✅ Logo found via YouTube API: {logo}")
                        return logo
        except Exception as e:
            print(f"⚠️ YouTube API channel lookup error: {e}")

    return ""

def get_working_image_url(video_id):
    """
    Checks if maxres is available, otherwise falls back to hqdefault_live or hqdefault.
    """
    maxres_url = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
    hq_live_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault_live.jpg"
    hq_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    
    try:
        if requests.head(maxres_url, timeout=5).status_code == 200:
            return maxres_url
    except Exception:
        pass

    try:
        if requests.head(hq_live_url, timeout=5).status_code == 200:
            return hq_live_url
    except Exception:
        pass

    return hq_url

def fetch_video_details(video_id):
    """
    Fetches video metadata from YouTube Data API v3.
    """
    if not YOUTUBE_API_KEY:
        print("❌ YOUTUBE_API_KEY is missing from environment.")
        sys.exit(1)

    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "part": "snippet,statistics,contentDetails,liveStreamingDetails",
        "id": video_id,
        "key": YOUTUBE_API_KEY
    }

    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        if not items:
            print(f"❌ Video not found on YouTube for ID: {video_id}")
            return None
        return items[0]
    except Exception as e:
        print(f"❌ YouTube API Request failed: {e}")
        return None

# ---------------- INITIALIZE FIREBASE APPS ----------------
def init_firebase_apps():
    apps = {}
    if SERVICE_ACCOUNT_GURBANI:
        try:
            cred_gurbani = credentials.Certificate(json.loads(SERVICE_ACCOUNT_GURBANI))
            app_gurbani = firebase_admin.initialize_app(cred_gurbani, name='gurbani_manual_add')
            apps['gurbani'] = {
                'name': 'Gurbani App',
                'db': firestore.client(app=app_gurbani)
            }
            print("🔌 Connected to Gurbani App Firebase.")
        except Exception as e:
            print(f"⚠️ Failed to connect to Gurbani App: {e}")

    if SERVICE_ACCOUNT_HARMANDIR:
        try:
            cred_harmandir = credentials.Certificate(json.loads(SERVICE_ACCOUNT_HARMANDIR))
            app_harmandir = firebase_admin.initialize_app(cred_harmandir, name='harmandir_manual_add')
            apps['harmandir'] = {
                'name': 'Harmandir App',
                'db': firestore.client(app=app_harmandir)
            }
            print("🔌 Connected to Harmandir App Firebase.")
        except Exception as e:
            print(f"⚠️ Failed to connect to Harmandir App: {e}")

    if SERVICE_ACCOUNT_HUKAMNAMA:
        try:
            cred_hukamnama = credentials.Certificate(json.loads(SERVICE_ACCOUNT_HUKAMNAMA))
            app_hukamnama = firebase_admin.initialize_app(cred_hukamnama, name='hukamnama_manual_add')
            apps['hukamnama'] = {
                'name': 'Hukamnama App',
                'db': firestore.client(app=app_hukamnama)
            }
            print("🔌 Connected to Hukamnama App Firebase.")
        except Exception as e:
            print(f"⚠️ Failed to connect to Hukamnama App: {e}")

    return apps

# ---------------- MAIN EXECUTION ----------------
def main():
    parser = argparse.ArgumentParser(description="Add a single YouTube video to a Firestore collection")
    parser.add_argument("--url", help="YouTube Video URL or Video ID")
    parser.add_argument("--collection", help="Target Firestore collection")
    parser.add_argument("--custom-collection", help="Custom Firestore collection name if selected")
    parser.add_argument("--is-live", choices=["auto", "true", "false"], default="auto", help="Live status (auto, true, false)")
    parser.add_argument("--target-db", choices=["all", "gurbani", "harmandir", "hukamnama"], default="all", help="Target DB")

    args = parser.parse_args()

    # Read from CLI arguments or environment variables
    video_input = args.url or os.environ.get("INPUT_VIDEO_URL", "").strip()
    selected_collection = args.collection or os.environ.get("INPUT_COLLECTION", "").strip()
    custom_collection = args.custom_collection or os.environ.get("INPUT_CUSTOM_COLLECTION", "").strip()
    is_live_setting = (args.is_live or os.environ.get("INPUT_IS_LIVE", "auto")).strip().lower()
    target_db_setting = (args.target_db or os.environ.get("INPUT_TARGET_DB", "all")).strip().lower()

    if not video_input:
        print("❌ Error: No YouTube video URL or ID provided.")
        sys.exit(1)

    # Determine collection name
    if selected_collection in ["Custom Collection", "custom"] and custom_collection:
        collection_name = custom_collection
    elif selected_collection:
        collection_name = selected_collection
    else:
        collection_name = "Listen_Kirtans_Videos_New"

    video_id = extract_video_id(video_input)
    if not video_id:
        print(f"❌ Error: Could not extract a valid 11-character video ID from: {video_input}")
        sys.exit(1)

    print("\n" + "=" * 50)
    print(f"🎯 Target Video ID: {video_id}")
    print(f"📁 Target Collection: {collection_name}")
    print(f"🔴 Live Mode Setting: {is_live_setting}")
    print(f"🗄️ Target Database(s): {target_db_setting}")
    print("=" * 50 + "\n")

    # 1. Fetch metadata from YouTube
    print("🔍 Fetching video details from YouTube Data API...")
    yt_item = fetch_video_details(video_id)
    if not yt_item:
        sys.exit(1)

    snippet = yt_item.get("snippet", {})
    stats = yt_item.get("statistics", {})

    title = snippet.get("title", "")
    channel_name = snippet.get("channelTitle", "")
    channel_id = snippet.get("channelId", "")
    published_at = snippet.get("publishedAt", "")
    live_broadcast = snippet.get("liveBroadcastContent", "none")
    view_count = int(stats.get("viewCount", 0))

    # Published timestamp in milliseconds as string for 'timeAgo'
    try:
        published_dt = datetime.fromisoformat(published_at.replace("Z", "+00:00")).astimezone(timezone.utc)
        time_ago_str = str(int(published_dt.timestamp() * 1000))
    except Exception:
        time_ago_str = str(int(datetime.now(timezone.utc).timestamp() * 1000))

    # Current UTC timestamp in milliseconds as string for 'timestamp'
    current_timestamp_str = str(int(datetime.now(timezone.utc).timestamp() * 1000))

    # Working thumbnail image URL
    image_url = get_working_image_url(video_id)

    # Channel logo URL
    channel_logo_url = fetch_channel_logo(channel_id)

    # Determine isLive boolean
    if is_live_setting == "true":
        is_live = True
    elif is_live_setting == "false":
        is_live = False
    else:
        # 'auto' mode
        if live_broadcast == "live":
            is_live = True
        elif collection_name in ["liveStreams", "liveStreams_More"]:
            is_live = True
        else:
            is_live = False

    canonical_url = f"https://www.youtube.com/watch?v={video_id}"

    # 2. Build the exact document payload requested
    # Fields:
    #   channelLogoUrl (string)
    #   channelName (string)
    #   imageUrl (string)
    #   isLive (boolean)
    #   timeAgo (string)
    #   timestamp (string)
    #   title (string)
    #   titleLowercase (string)
    #   url (string)
    #   viewCount (int64)
    doc_payload = {
        "channelLogoUrl": channel_logo_url,
        "channelName": channel_name,
        "imageUrl": image_url,
        "isLive": is_live,
        "timeAgo": time_ago_str,
        "timestamp": current_timestamp_str,
        "title": title,
        "titleLowercase": title.lower(),
        "url": canonical_url,
        "viewCount": view_count
    }

    print("\n📦 Final Document Payload to Insert:")
    print(json.dumps(doc_payload, indent=2, ensure_ascii=False))

    # 3. Connect to Firebase
    print("\n🔌 Initializing Firebase connections...")
    apps = init_firebase_apps()
    if not apps:
        print("❌ Error: No Firebase databases could be initialized. Please check service account secrets.")
        sys.exit(1)

    # Determine which databases to update
    targets = []
    if target_db_setting == "all":
        targets = list(apps.keys())
    elif target_db_setting in apps:
        targets = [target_db_setting]
    else:
        print(f"⚠️ Target DB '{target_db_setting}' is not initialized; updating all available ({list(apps.keys())}).")
        targets = list(apps.keys())

    # 4. Insert or update in each database
    for app_key in targets:
        app_info = apps[app_key]
        app_name = app_info['name']
        db = app_info['db']

        # Determine collection name for this specific app (Harmandir mappings)
        target_col = collection_name
        if app_key == 'harmandir' and collection_name in HARMANDIR_COLLECTION_MAP:
            target_col = HARMANDIR_COLLECTION_MAP[collection_name]
            print(f"ℹ️ [{app_name}] Mapping collection '{collection_name}' -> '{target_col}'")

        print(f"\n📝 [{app_name}] Processing collection: {target_col} ...")

        try:
            col_ref = db.collection(target_col)

            # Check if document with this URL already exists
            existing_docs = list(col_ref.where(filter=FieldFilter("url", "==", canonical_url)).limit(1).stream())
            
            app_payload = doc_payload.copy()

            if existing_docs:
                existing_doc = existing_docs[0]
                doc_id = existing_doc.id
                existing_data = existing_doc.to_dict() or {}
                # Preserve original timestamp if already exists
                if "timestamp" in existing_data:
                    app_payload["timestamp"] = existing_data["timestamp"]
                existing_doc.reference.set(app_payload, merge=True)
                print(f"   ✅ [{app_name}] Updated existing document! (ID: {doc_id})")
            else:
                # Create a new document with an auto-generated ID
                new_doc_ref = col_ref.document()
                doc_id = new_doc_ref.id
                new_doc_ref.set(app_payload)
                print(f"   ✅ [{app_name}] Created new document! (ID: {doc_id})")

            # ---------------- SKIP SEARCH_COLLECTION ----------------
            print(f"   ⏩ [{app_name}] Search_Collection indexing explicitly SKIPPED as requested.")

            # ---------------- UPDATE INDEX DOCUMENTS IF APPLICABLE ----------------
            # Handle -All_Videos_Id for Kirtan collections
            if target_col in ["Listen_Kirtans_Videos_New", "Kirtan-Youtube-Videos"]:
                all_ids_doc_ref = col_ref.document("-All_Videos_Id")
                all_ids_snap = all_ids_doc_ref.get()
                current_ids = all_ids_snap.to_dict().get("video_id", []) if all_ids_snap.exists else []
                if isinstance(current_ids, list):
                    if video_id not in current_ids:
                        current_ids.append(video_id)
                        all_ids_doc_ref.set({
                            "video_id": current_ids,
                            "total_count": len(current_ids)
                        }, merge=True)
                        print(f"   💾 [{app_name}] Updated -All_Videos_Id index (Count: {len(current_ids)})")

                # Update App-Setup reload trigger
                random_trigger = random.randint(100000000, 999999999)
                try:
                    db.collection("App-Setup").document("App-Setup").set({
                        "kirtan_videos_fetch": random_trigger
                    }, merge=True)
                    print(f"   🔄 [{app_name}] Updated App-Setup trigger: {random_trigger}")
                except Exception as ex:
                    print(f"   ⚠️ Could not update App-Setup trigger: {ex}")

            # Handle -All_Live_Videos_Id for liveStreams_More
            elif target_col == "liveStreams_More":
                all_ids_doc_ref = col_ref.document("-All_Live_Videos_Id")
                all_ids_snap = all_ids_doc_ref.get()
                current_ids = all_ids_snap.to_dict().get("video_id", []) if all_ids_snap.exists else []
                if isinstance(current_ids, list):
                    if video_id not in current_ids:
                        current_ids.append(video_id)
                        all_ids_doc_ref.set({
                            "video_id": current_ids,
                            "total_count": len(current_ids)
                        }, merge=True)
                        print(f"   💾 [{app_name}] Updated -All_Live_Videos_Id index (Count: {len(current_ids)})")

        except Exception as e:
            print(f"   ❌ [{app_name}] Failed to save to {target_col}: {e}")

    print("\n" + "=" * 50)
    print("🎉 Done! Successfully processed video into Firestore.")
    print("=" * 50 + "\n")

if __name__ == "__main__":
    main()
