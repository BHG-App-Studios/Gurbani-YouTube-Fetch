import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import firebase_admin
from firebase_admin import credentials, firestore
import json
import os

# ---------------- CONFIG ----------------
CHANNEL_ID = "UCSx5035_us8h8DOp_YhQDaw"
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
from google.cloud.firestore_v1 import FieldFilter

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
        "url": data["url"],
        "updatedAt": firestore.SERVER_TIMESTAMP
    })

    print("✅ Takht Sri Kesgarh Sahib video updated successfully")

# ---------------- MAIN ----------------
if __name__ == "__main__":
    result = fetch_latest_video()

    if not result:
        print("❌ No video found in RSS feed")
    else:
        print("🎯 Selected Latest Video:")
        print(result)
        update_firestore(result)
