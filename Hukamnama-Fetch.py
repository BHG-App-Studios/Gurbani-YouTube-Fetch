import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import firebase_admin
from firebase_admin import credentials, firestore
import json
import os
import sys
from google.cloud.firestore_v1 import FieldFilter

# ---------------- CONFIG ----------------
CHANNEL_ID = "UCD5a5KBLu4t7uZdoJQzEppg"
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

# ---------------- RSS FETCH ----------------
def fetch_latest_hukamnama_katha():
    response = requests.get(RSS_URL, timeout=15)
    response.raise_for_status()

    root = ET.fromstring(response.text)
    matches = []

    for entry in root.findall("atom:entry", NS):
        title_el = entry.find("atom:title", NS)
        video_id_el = entry.find("yt:videoId", NS)
        published_el = entry.find("atom:published", NS)

        if title_el is None or video_id_el is None or published_el is None:
            continue

        title = title_el.text.strip()

        # ✅ FILTER: Today Hukamnama Sri Darbar Sahib ONLY
        if "Today Hukamnama Sri Darbar Sahib(" not in title:
            continue

        published = datetime.fromisoformat(
            published_el.text.replace("Z", "+00:00")
        ).astimezone(timezone.utc)

        matches.append({
            "video_id": video_id_el.text.strip(),
            "title": title,
            "published": published
        })

    if not matches:
        return None

    # ✅ LATEST ONLY
    latest = max(matches, key=lambda x: x["published"])

    return {
        # 👇 CHANGED: Now uses the helper function to check the image URL
        "imageUrl": get_working_image_url(latest['video_id']),
        "title": latest["title"],
        "url": f"https://www.youtube.com/watch?v={latest['video_id']}"
    }

# ---------------- FIRESTORE UPDATE DUAL DATABASES ----------------
def update_firestore_dual(data):
    print("\n📝 Updating Databases...")
    
    # --- Prepare Data for Gurbani App (Basic Fields Only) ---
    gurbani_update_payload = {
        "imageUrl": data["imageUrl"],
        "title": data["title"],
        "url": data["url"]
    }

    # --- Prepare Data for Harmandir Sahib App (With lowercase, NO timestamp edit) ---
    harmandir_update_payload = {
        "imageUrl": data["imageUrl"],
        "title": data["title"],
        "url": data["url"],
        "titleLowercase": data["title"].lower()
    }

    # helper function to execute update
    def do_update(db_client, collection_name, app_name, payload):
        docs = (
            db_client.collection(collection_name)
            # ❗ Important: Kept the specific query field from your original script
            .where(filter=FieldFilter("hukamnama", "==", CHANNEL_ID))
            .limit(1)
            .get()
        )

        if not docs:
            print(f"❌ No document found for channel ID in {app_name} (Collection: {collection_name})")
            return

        doc = docs[0]
        existing = doc.to_dict()

        # 🔒 CHANGE-DETECTION
        if existing.get("url") == payload["url"]:
            print(f"⏭ No change detected for {app_name}. Skipping update.")
            return

        # ✅ UPDATE ONLY IF CHANGED
        doc.reference.update(payload)
        print(f"✅ {app_name} updated successfully")

    # Run for Gurbani
    do_update(db_gurbani, COLLECTION_GURBANI, "Gurbani App", gurbani_update_payload)
    
    # Run for Harmandir Sahib
    do_update(db_harmandir, COLLECTION_HARMANDIR, "Harmandir App", harmandir_update_payload)


# ---------------- MAIN ----------------
if __name__ == "__main__":
    result = fetch_latest_hukamnama_katha()

    if not result:
        print("❌ No Today Hukamnama Sri Darbar Sahib video found")
    else:
        print("🎯 Selected Today Hukamnama Sri Darbar Sahib:")
        print(f"Title: {result['title']}")
        print(f"URL: {result['url']}")
        update_firestore_dual(result)
