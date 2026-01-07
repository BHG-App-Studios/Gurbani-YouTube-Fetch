import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

CHANNEL_ID = "UCYn6UEtQ771a_OWSiNBoG8w"
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015"
}

def fetch_latest_official_sgpc_live():
    response = requests.get(RSS_URL, timeout=15)
    response.raise_for_status()

    root = ET.fromstring(response.text)
    matches = []

    for entry in root.findall("atom:entry", NS):
        title_el = entry.find("atom:title", NS)
        video_id_el = entry.find("yt:videoId", NS)
        published_el = entry.find("atom:published", NS)

        # ✅ SAFE checks
        if title_el is None or video_id_el is None or published_el is None:
            continue

        title = title_el.text.strip()

        # ✅ FILTER: ONLY Official SGPC LIVE streams
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

    # ✅ SORT BY TIME (LATEST FIRST)
    matches.sort(key=lambda x: x["published"], reverse=True)

    # ✅ TAKE LATEST 2 ONLY
    latest_two = matches[:2]

    # ✅ PREFER AUDIO IF PRESENT
    selected = None
    for video in latest_two:
        if "LIVE (Audio)" in video["title"]:
            selected = video
            break

    # ❌ If no Audio, take latest by time
    if selected is None:
        selected = latest_two[0]

    return {
        "imageUrl": f"https://i.ytimg.com/vi/{selected['video_id']}/hqdefault_live.jpg",
        "title": selected["title"],
        "url": f"https://www.youtube.com/watch?v={selected['video_id']}"
    }


if __name__ == "__main__":
    result = fetch_latest_official_sgpc_live()

    if result:
        print(result)
    else:
        print("❌ No matching Official SGPC LIVE video found")
