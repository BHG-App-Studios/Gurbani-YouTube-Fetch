import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import firebase_admin
from firebase_admin import credentials, firestore
import json
import os

# ---------------- CONFIG ----------------
CHANNEL_ID = "UC2p-B_igjgCNUURYmDeV9Dg"
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

# ---------------- RSS FETCH ----------------
def fetch_latest_stream():
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

        # ✅ FILTER: ਅੱਜ ਇਹ ਬਾਣੀ ਲਗਾ ਕੇ ਰਖੋ ONLY
        if "ਅੱਜ ਇਹ ਬਾਣੀ ਲਗਾ ਕੇ ਰਖੋ" not in title:
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
        "imageUrl": f"https://i.ytimg.com/vi/{latest['video_id']}/hqdefault.jpg",
        "title": latest["title"],
        "url": f"https://www.youtube.com/watch?v={latest['video_id']}"
    }

# ---------------- FIRESTORE UPDATE ----------------
from google.cloud.firestore_v1 import FieldFilter

def update_firestore(data):
    docs = (
        db.collection(COLLECTION_NAME)
        .where(filter=FieldFilter("channel_Id", "==", CHANNEL_ID))
        .limit(1)
        .get()
    )

    if not docs:
        print("❌ No Firestore document found with channel id matching")
        return

    doc = docs[0]
    doc_ref = doc.reference
    existing = doc.to_dict()

    # 🔒 CHANGE-DETECTION
    if existing.get("url") == data["url"]:
        print("⏭ No change detected (same ਅੱਜ ਇਹ ਬਾਣੀ ਲਗਾ ਕੇ ਰਖੋ). Skipping update.")
        return

    # ✅ UPDATE ONLY IF CHANGED
    doc_ref.update({
        "imageUrl": data["imageUrl"],
        "title": data["title"],
        "url": data["url"]
    })

    print("✅ ਅੱਜ ਇਹ ਬਾਣੀ ਲਗਾ ਕੇ ਰਖੋ updated successfully")

# ---------------- MAIN ----------------
if __name__ == "__main__":
    result = fetch_latest_stream()

    if not result:
        print("❌ No ਅੱਜ ਇਹ ਬਾਣੀ ਲਗਾ ਕੇ ਰਖੋ video found")
    else:
        print("🎯 Selected ਅੱਜ ਇਹ ਬਾਣੀ ਲਗਾ ਕੇ ਰਖੋ:")
        print(result)
        update_firestore(result)
