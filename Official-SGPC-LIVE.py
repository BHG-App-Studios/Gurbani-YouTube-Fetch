import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import firebase_admin
from firebase_admin import credentials, firestore

# ---------------- CONFIG ----------------
CHANNEL_ID = "UCYn6UEtQ771a_OWSiNBoG8w"
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

SERVICE_ACCOUNT_FILE = r"C:\Users\Gurpreet\Downloads\gurbani-app-firebase-adminsdk.json"
COLLECTION_NAME = "liveStreams"

# --------------------------------------

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015"
}

# ---------------- FIREBASE INIT ----------------
if not firebase_admin._apps:
    cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
    firebase_admin.initialize_app(cred)

db = firestore.client()

# ---------------- RSS FETCH + FILTER ----------------
def fetch_latest_official_sgpc_live():
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

        # Only Official SGPC LIVE streams
        if not title.startswith("Official SGPC LIVE"):
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

    # Sort latest first
    matches.sort(key=lambda x: x["published"], reverse=True)

    # Take latest 2
    latest_two = matches[:2]

    # Prefer Audio if present
    selected = None
    for video in latest_two:
        if "LIVE (Audio)" in video["title"]:
            selected = video
            break

    if selected is None:
        selected = latest_two[0]

    return {
        "imageUrl": f"https://i.ytimg.com/vi/{selected['video_id']}/hqdefault_live.jpg",
        "title": selected["title"],
        "url": f"https://www.youtube.com/watch?v={selected['video_id']}"
    }

# ---------------- FIRESTORE UPDATE ----------------
from google.cloud.firestore_v1 import FieldFilter

def update_firestore_live_stream(data):
    docs = (
        db.collection(COLLECTION_NAME)
        .where(filter=FieldFilter("channel_Id", "==", CHANNEL_ID))
        .limit(1)
        .get()
    )

    if not docs:
        print("❌ No Firestore document found with this channel_Id")
        return

    doc_ref = docs[0].reference

    doc_ref.update({
        "imageUrl": data["imageUrl"],
        "title": data["title"],
        "url": data["url"]
    })

    print("✅ Firestore updated successfully")

# ---------------- MAIN ----------------
if __name__ == "__main__":
    result = fetch_latest_official_sgpc_live()

    if not result:
        print("❌ No matching Official SGPC LIVE video found")
    else:
        print("🎯 Selected Video:")
        print(result)
        update_firestore_live_stream(result)
