import os
import sys
import json
import re
import requests
from datetime import datetime, timedelta, timezone
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1 import FieldFilter

# ---------------- CONFIG ----------------
CHANNEL_ID = "UCudVHqnOekwcvpzNpY8_ERw"
TARGET_TITLE_FILTER = "Official SGPC LIVE | Katha Hukamnama Sahib"
FIRESTORE_FIELD = "hukamnama_katha_fatehgarh_sahib"

WHERE_TO_FETCH = "streams"

SERVICE_ACCOUNT_GURBANI = os.environ.get("FIREBASE_SERVICE_ACCOUNT_GURBANI")
SERVICE_ACCOUNT_HARMANDIR = os.environ.get("FIREBASE_SERVICE_ACCOUNT_HARMANDIR")

if not SERVICE_ACCOUNT_GURBANI or not SERVICE_ACCOUNT_HARMANDIR:
    print("❌ FIREBASE_SERVICE_ACCOUNT env vars missing for one or both apps")
    sys.exit(1)

# Collection Names
COLLECTION_GURBANI = "liveStreams"
COLLECTION_HARMANDIR = "Live-Gurdwaras-YouTube"

# ---------------- FIREBASE DUAL INIT ----------------
print("🔌 Initializing Firebase Connections...")

cred_gurbani = credentials.Certificate(json.loads(SERVICE_ACCOUNT_GURBANI))
app_gurbani = firebase_admin.initialize_app(cred_gurbani, name='gurbani_app')
db_gurbani = firestore.client(app=app_gurbani)

cred_harmandir = credentials.Certificate(json.loads(SERVICE_ACCOUNT_HARMANDIR))
app_harmandir = firebase_admin.initialize_app(cred_harmandir, name='harmandir_app')
db_harmandir = firestore.client(app=app_harmandir)


# ---------------- HTML SCRAPING & PARSING HELPERS ----------------
def get_working_image_url(video_id):
    maxres_url = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
    fallback_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault_live.jpg"
    try:
        if requests.head(maxres_url, timeout=5).status_code == 200:
            return maxres_url
    except Exception:
        pass
    return fallback_url

def parse_time_text_to_ms(time_text):
    """Converts YouTube 'X hours ago' text into a Unix Timestamp in milliseconds."""
    now = datetime.now(timezone.utc)
    if not time_text:
        return int(now.timestamp() * 1000)
        
    match = re.search(r'(\d+)\s+(second|minute|hour|day|week|month|year)s?', time_text, re.IGNORECASE)
    if not match:
        return int(now.timestamp() * 1000)
        
    amount = int(match.group(1))
    unit = match.group(2).lower()
    
    if 'second' in unit: delta = timedelta(seconds=amount)
    elif 'minute' in unit: delta = timedelta(minutes=amount)
    elif 'hour' in unit: delta = timedelta(hours=amount)
    elif 'day' in unit: delta = timedelta(days=amount)
    elif 'week' in unit: delta = timedelta(weeks=amount)
    elif 'month' in unit: delta = timedelta(days=amount * 30) # approximation
    elif 'year' in unit: delta = timedelta(days=amount * 365) # approximation
    else: delta = timedelta(0)
        
    published_time = now - delta
    return int(published_time.timestamp() * 1000)

def fetch_channel_data_from_source(channel_id):
    """
    Scrapes the targeted page and extracts ALL data from ytInitialData.
    Cost: 0 API Quota. 1 Request.
    """
    # ✅ FIX: Now uses the WHERE_TO_FETCH variable dynamically
    url = f"https://www.youtube.com/channel/{channel_id}/{WHERE_TO_FETCH}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9'
    }
    
    print(f"🔄 Scraping {url}")
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        html = response.text
        
        # 1. Extract the JSON object
        match = re.search(r'var ytInitialData = (\{.*?\});<\/script>', html)
        if not match:
            print("❌ Could not find ytInitialData in page source.")
            return None
            
        data = json.loads(match.group(1))
        
        # 2. Extract Channel Logo
        logo_url = "https://yt3.googleusercontent.com/Dt394Emnx-gzw6Exnrx7RHwK4lp_Y0nJo7UvrqzX1ri8lPL_k1DgE79soW0U2cCQ3aosNq3m=s900-c-k-c0x00ffffff-no-rj"
        try:
            logo_url = data['metadata']['channelMetadataRenderer']['avatar']['thumbnails'][0]['url']
        except Exception:
            pass
        
        # 3. Recursive helper to find ALL matching videos
        def find_all_videos(obj, matches_list):
            if isinstance(obj, dict):
                if 'videoRenderer' in obj:
                    title = obj['videoRenderer'].get("title", {}).get("runs", [{}])[0].get("text", "")
                    if TARGET_TITLE_FILTER.lower() in title.lower():
                        matches_list.append(obj['videoRenderer'])
                for k, v in obj.items():
                    find_all_videos(v, matches_list)
            elif isinstance(obj, list):
                for item in obj:
                    find_all_videos(item, matches_list)
                    
        matches = []
        find_all_videos(data, matches)
        
        if not matches:
            print(f"❌ No matching '{TARGET_TITLE_FILTER}' video found on the {WHERE_TO_FETCH} page.")
            return None

        # ✅ Guarantee we pick the absolutely newest/latest video mathematically
        target_video = max(
            matches, 
            key=lambda v: parse_time_text_to_ms(v.get("publishedTimeText", {}).get("simpleText", ""))
        )

        # 4. Extract target data
        video_id = target_video.get("videoId", "")
        title = target_video.get("title", {}).get("runs", [{}])[0].get("text", "")
        channel_name = target_video.get("shortBylineText", {}).get("runs", [{}])[0].get("text", "Sachkhand Sri Hazur Sahib")
        
        # Views
        view_text = target_video.get("viewCountText", {}).get("simpleText", "0")
        view_count = int(re.sub(r'\D', '', view_text)) if view_text else 0
        
        # Duration
        duration = target_video.get("lengthText", {}).get("simpleText", "00:00")
        
        # Timestamps
        time_text = target_video.get("publishedTimeText", {}).get("simpleText", "")
        published_timestamp_ms = parse_time_text_to_ms(time_text)
        current_timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        return {
            "channelLogoUrl": logo_url.replace("s200", "s900").replace("s72", "s900"),
            "channelName": channel_name,
            "duration": duration,
            FIRESTORE_FIELD: CHANNEL_ID,
            "imageUrl": get_working_image_url(video_id),
            "isLive": False,
            "timeAgo": str(published_timestamp_ms),  
            "timestamp": str(current_timestamp_ms), 
            "title": title,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "viewCount": view_count
        }

    except Exception as e:
        print(f"❌ Error scraping page source: {e}")
        return None


# ---------------- FIRESTORE LOGIC ----------------
def process_and_update_firestore():
    base_payload = fetch_channel_data_from_source(CHANNEL_ID)

    if not base_payload:
        return

    # ---------------- 1. READ FIREBASE (Exactly 2 Reads) ----------------
    print("\n🔍 Reading existing data from Firestore...")
    
    gurbani_docs = db_gurbani.collection(COLLECTION_GURBANI).where(
        filter=FieldFilter(FIRESTORE_FIELD, "==", CHANNEL_ID)
    ).limit(1).get()
    
    harmandir_docs = db_harmandir.collection(COLLECTION_HARMANDIR).where(
        filter=FieldFilter(FIRESTORE_FIELD, "==", CHANNEL_ID)
    ).limit(1).get()

    gurbani_doc = gurbani_docs[0] if gurbani_docs else None
    harmandir_doc = harmandir_docs[0] if harmandir_docs else None

    print("\n🎯 Final Payload Assembled:")
    print(json.dumps(base_payload, indent=2))

    # ---------------- 2. CONDITIONAL WRITE FIREBASE (Max 2 Writes) ----------------
    def safe_update(doc_snapshot, payload, app_name):
        if not doc_snapshot:
            print(f"❌ Document missing in {app_name}, cannot update.")
            return

        existing = doc_snapshot.to_dict()
        
        # Check if an update is genuinely needed
        if (existing.get("url") == payload["url"] and 
            existing.get("viewCount") == payload["viewCount"] and 
            existing.get("title") == payload["title"] and
            existing.get("duration") == payload["duration"]):
            print(f"⏭ No data changed for {app_name} (Views still {payload['viewCount']}). Write skipped.")
            return

        doc_snapshot.reference.update(payload)
        print(f"✅ {app_name} updated successfully with {payload['viewCount']} views and duration {payload['duration']}!")

    print("\n📝 Pushing updates to Databases...")
    
    # Gurbani Update
    safe_update(gurbani_doc, base_payload, "Gurbani App")

    # Harmandir Update (Requires titleLowercase)
    harmandir_payload = base_payload.copy()
    harmandir_payload["titleLowercase"] = base_payload["title"].lower()
    safe_update(harmandir_doc, harmandir_payload, "Harmandir App")


# ---------------- MAIN ----------------
if __name__ == "__main__":
    process_and_update_firestore()
