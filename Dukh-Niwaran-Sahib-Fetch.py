import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import firebase_admin
from firebase_admin import credentials, firestore
import json
import os
from google.cloud.firestore_v1 import FieldFilter

# ---------------- CONFIG ----------------
CHANNEL_ID = "UCPKPN4bzM8Ja-F_kIEZoAhA"
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

SERVICE_ACCOUNT_JSON = os.environ["FIREBASE_SERVICE_ACCOUNT"]
COLLECTION_NAME = "liveStreams"
# --------------------------------------

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015"
}

# ---------------- FIREBASE INIT ----------------
if not firebase_admin._apps:
    service_account_info = json.loads(SERVICE_ACCOUNT_JSON)
    cred = credentials.Certificate(service_account_info)
    firebase_admin.initialize_app(cred)

db = firestore.client()

# ---------------- LIVE CHECK (HTML, NO API) ----------------
import re

def is_video_live(video_url):
    try:
        print("🔗 Checking URL:", video_url)

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }

        r = requests.get(video_url, headers=headers, timeout=20)

        print("🌐 HTTP status:", r.status_code)
        print("📦 Response size (chars):", len(r.text))

        if r.status_code != 200:
            print("❌ Non-200 response")
            return False

        html = r.text

        # 🔍 DEBUG: check if ANY live keywords exist
        print("🔎 Contains 'isLiveNow'?", "isLiveNow" in html)
        print("🔎 Contains 'liveBroadcastDetails'?", "liveBroadcastDetails" in html)
        print("🔎 Contains 'status\":\"LIVE\"'?", '"status":"LIVE"' in html)

        # 🔍 DEBUG: print a small surrounding snippet if found
        idx = html.find("isLiveNow")
        if idx != -1:
            print("🧩 Snippet around isLiveNow:")
            print(html[idx-100:idx+100])

        # ✅ ACTUAL CHECK
        if re.search(r'"isLiveNow"\s*:\s*true', html):
            print("✅ MATCHED isLiveNow:true")
            return True

        if '"status":"LIVE"' in html:
            print("✅ MATCHED status:LIVE")
            return True

        if '"liveBroadcastDetails"' in html:
            print("⚠ Found liveBroadcastDetails (weak signal)")
            return True

        print("❌ No live signals detected")
        return False

    except Exception as e:
        print("⚠ Live check exception:", e)
        return False


# ---------------- GET CURRENT FIREBASE URL ----------------
def get_current_firebase_url():
    docs = (
        db.collection(COLLECTION_NAME)
        .where(filter=FieldFilter("channel_Id", "==", CHANNEL_ID))
        .limit(1)
        .get()
    )

    if not docs:
        return None

    return docs[0].to_dict().get("url")

# ---------------- RSS FETCH (LATEST VIDEO ONLY) ----------------
def fetch_latest_video():
    response = requests.get(RSS_URL, timeout=15)
    response.raise_for_status()

    root = ET.fromstring(response.text)
    videos = []

    for entry in root.findall("atom:entry", NS):
        title_el = entry.find("atom:title", NS)
        video_id_el = entry.find("yt:videoId", NS)
        published_el = entry.find("atom:published", NS)

        if title_el is None or video_id_el is None or published_el is None:
            continue

        published = datetime.fromisoformat(
            published_el.text.replace("Z", "+00:00")
        ).astimezone(timezone.utc)

        videos.append({
            "video_id": video_id_el.text.strip(),
            "title": title_el.text.strip(),
            "published": published
        })

    if not videos:
        return None

    # ✅ LATEST VIDEO BY TIME
    latest = max(videos, key=lambda x: x["published"])

    return {
        "imageUrl": f"https://i.ytimg.com/vi/{latest['video_id']}/hqdefault.jpg",
        "title": latest["title"],
        "url": f"https://www.youtube.com/watch?v={latest['video_id']}"
    }

# ---------------- FIRESTORE UPDATE ----------------
def update_firestore(data):
    docs = (
        db.collection(COLLECTION_NAME)
        .where(filter=FieldFilter("channel_Id", "==", CHANNEL_ID))
        .limit(1)
        .get()
    )

    if not docs:
        print("❌ No Firestore document found with this channel_Id")
        return

    doc = docs[0]
    doc_ref = doc.reference
    existing = doc.to_dict()

    # 🔒 CHANGE-DETECTION
    if existing.get("url") == data["url"]:
        print("⏭ No change detected (same latest video). Skipping update.")
        return

    doc_ref.update({
        "imageUrl": data["imageUrl"],
        "title": data["title"],
        "url": data["url"]
    })

    print("✅ Dukh Niwaran Sahib Ludhiana video updated successfully")

# ---------------- MAIN ----------------
if __name__ == "__main__":

    # 🔎 STEP 1: Check current Firebase URL first
    current_url = get_current_firebase_url()
    print("📄 Firebase current_url =", current_url)


    if current_url:
        print("🔍 Checking current Firebase URL live status...")
        if is_video_live(current_url):
            print("🔴 Stream is currently LIVE. Exiting without changes.")
            exit(0)
        else:
            print("⏹ Existing stream is NOT live. Continuing...")

    # 🔄 STEP 2: Existing logic (UNCHANGED)
    result = fetch_latest_video()

    if not result:
        print("❌ No video found in RSS feed")
    else:
        print("🎯 Selected Latest Video:")
        print(result)
        update_firestore(result)
