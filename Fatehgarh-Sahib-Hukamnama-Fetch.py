import os
import sys
import json
import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1 import FieldFilter

# ---------------- CONFIG ----------------
# Using the exact Channel ID from your requested output
CHANNEL_ID = "UCudVHqnOekwcvpzNpY8_ERw"
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

# Env variables for BOTH service accounts
SERVICE_ACCOUNT_GURBANI = os.environ.get("FIREBASE_SERVICE_ACCOUNT_GURBANI")
SERVICE_ACCOUNT_HARMANDIR = os.environ.get("FIREBASE_SERVICE_ACCOUNT_HARMANDIR")

if not SERVICE_ACCOUNT_GURBANI or not SERVICE_ACCOUNT_HARMANDIR:
    print("❌ FIREBASE_SERVICE_ACCOUNT env vars missing for one or both apps")
    sys.exit(1)

# Collection Names
COLLECTION_GURBANI = "liveStreams"
COLLECTION_HARMANDIR = "Live-Gurdwaras-YouTube"

# Important: Added "media" namespace to fetch views from RSS
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/"
}

# ---------------- FIREBASE DUAL INIT ----------------
print("🔌 Initializing Firebase Connections...")

cred_gurbani = credentials.Certificate(json.loads(SERVICE_ACCOUNT_GURBANI))
app_gurbani = firebase_admin.initialize_app(cred_gurbani, name='gurbani_app')
db_gurbani = firestore.client(app=app_gurbani)

cred_harmandir = credentials.Certificate(json.loads(SERVICE_ACCOUNT_HARMANDIR))
app_harmandir = firebase_admin.initialize_app(cred_harmandir, name='harmandir_app')
db_harmandir = firestore.client(app=app_harmandir)

# ---------------- SCRAPING HELPERS ----------------
def fetch_channel_logo(channel_id):
    channel_url = f"https://www.youtube.com/channel/{channel_id}"
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
            return meta_image['content']
    except Exception as e:
        print(f"❌ Error scraping logo: {e}")
    return ""

def get_working_image_url(video_id):
    maxres_url = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
    fallback_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault_live.jpg"
    try:
        if requests.head(maxres_url, timeout=5).status_code == 200:
            return maxres_url
    except Exception:
        pass
    return fallback_url

# ---------------- RSS FETCH (NO API REQUIRED) ----------------
def fetch_latest_stream():
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    response = requests.get(RSS_URL, headers=headers, timeout=15)
    response.raise_for_status()

    root = ET.fromstring(response.text)
    matches = []

    for entry in root.findall("atom:entry", NS):
        title_el = entry.find("atom:title", NS)
        video_id_el = entry.find("yt:videoId", NS)
        published_el = entry.find("atom:published", NS)
        author_name_el = entry.find("atom:author/atom:name", NS)
        stats_el = entry.find("media:group/media:community/media:statistics", NS)

        if title_el is None or video_id_el is None or published_el is None:
            continue

        title = title_el.text.strip()

        # ✅ FILTER: Official SGPC LIVE | Katha Hukamnama Sahib ONLY
        if "Official SGPC LIVE | Katha Hukamnama Sahib" not in title:
            continue

        published = datetime.fromisoformat(
            published_el.text.replace("Z", "+00:00")
        ).astimezone(timezone.utc)

        # Extract views from RSS
        view_count = int(stats_el.attrib.get("views", 0)) if stats_el is not None else 0
        channel_name = author_name_el.text if author_name_el is not None else ""

        matches.append({
            "video_id": video_id_el.text.strip(),
            "title": title,
            "published": published,
            "viewCount": view_count,
            "channelName": channel_name
        })

    if not matches:
        return None

    # Get the newest video
    latest = max(matches, key=lambda x: x["published"])
    
    # Process Timestamps
    time_ago_ms = str(int(latest["published"].timestamp() * 1000))
    current_timestamp_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    
    logo_url = fetch_channel_logo(CHANNEL_ID)

    # Build exact requested output
    return {
        "channelName": latest["channelName"],
        "channelLogoUrl": logo_url,
        "channel_id": CHANNEL_ID,
        "duration": "00:00", # RSS does not provide duration, defaulting to 00:00
        "hukamnama": CHANNEL_ID,
        "imageUrl": get_working_image_url(latest['video_id']),
        "isLive": False, # Assuming false as requested in output schema
        "timeAgo": time_ago_ms,
        "timestamp": current_timestamp_ms,
        "title": latest["title"],
        "url": f"https://www.youtube.com/watch?v={latest['video_id']}",
        "viewCount": latest["viewCount"]
    }

# ---------------- FIRESTORE SAFE UPDATE ----------------
def update_firestore_dual(data):
    print("\n📝 Updating Databases...")
    
    harmandir_payload = data.copy()
    harmandir_payload["titleLowercase"] = data["title"].lower()

    def do_update(db_client, collection_name, app_name, payload):
        docs = (
            db_client.collection(collection_name)
            .where(filter=FieldFilter("hukamnama_katha_fatehgarh_sahib", "==", CHANNEL_ID))
            .limit(1)
            .get()
        )

        if not docs:
            print(f"❌ No document found for channel ID in {app_name} (Collection: {collection_name})")
            return

        doc = docs[0]
        existing = doc.to_dict()

        # 🔒 SAFTEY CHECK: Compare views, url, and title to save Firebase Writes
        url_matches = existing.get("url") == payload["url"]
        views_match = existing.get("viewCount") == payload["viewCount"]
        title_matches = existing.get("title") == payload["title"]

        if url_matches and views_match and title_matches:
            print(f"⏭ No data changed for {app_name} (Views still {payload['viewCount']}). Skipping write to save limits.")
            return

        # ✅ Update only if something changed
        doc.reference.update(payload)
        print(f"✅ {app_name} updated successfully with {payload['viewCount']} views!")

    do_update(db_gurbani, COLLECTION_GURBANI, "Gurbani App", data)
    do_update(db_harmandir, COLLECTION_HARMANDIR, "Harmandir App", harmandir_payload)

# ---------------- MAIN ----------------
if __name__ == "__main__":
    print("🔄 Fetching latest stream data from RSS (Zero API Quota)...")
    result = fetch_latest_stream()

    if not result:
        print("❌ No Official SGPC LIVE | Katha Hukamnama Sahib video found")
    else:
        print("\n🎯 Final Payload to Save:")
        print(json.dumps(result, indent=2))
        update_firestore_dual(result)
