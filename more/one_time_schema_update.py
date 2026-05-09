#!/usr/bin/env python3
import firebase_admin
from firebase_admin import credentials, firestore
import json
import os
import sys

# ---------------- CONFIG ----------------
COLLECTION_GURBANI = "Listen_Kirtans_Videos_New"
COLLECTION_HARMANDIR = "Kirtan-Youtube-Videos"
ALL_IDS_DOC = "-All_Videos_Id"

# Env variables for BOTH service accounts
SERVICE_ACCOUNT_GURBANI = os.environ.get("FIREBASE_SERVICE_ACCOUNT_GURBANI")
SERVICE_ACCOUNT_HARMANDIR = os.environ.get("FIREBASE_SERVICE_ACCOUNT_HARMANDIR")

if not SERVICE_ACCOUNT_GURBANI or not SERVICE_ACCOUNT_HARMANDIR:
    print("❌ FIREBASE_SERVICE_ACCOUNT env vars missing for one or both apps")
    sys.exit(1)

# ---------------- FIREBASE DUAL INIT ----------------
print("🔌 Initializing Firebase Connections...")

# App 1: Gurbani
cred_gurbani = credentials.Certificate(json.loads(SERVICE_ACCOUNT_GURBANI))
app_gurbani = firebase_admin.initialize_app(cred_gurbani, name='gurbani_timestamp_fix')
db_gurbani = firestore.client(app=app_gurbani)

# App 2: Harmandir Sahib
cred_harmandir = credentials.Certificate(json.loads(SERVICE_ACCOUNT_HARMANDIR))
app_harmandir = firebase_admin.initialize_app(cred_harmandir, name='harmandir_timestamp_fix')
db_harmandir = firestore.client(app=app_harmandir)


# ---------------- PROCESSING FUNCTION ----------------
def sync_timestamp_from_timeago(db, collection_name):
    print(f"\n--- Scanning Collection: {collection_name} ---")
    
    docs = db.collection(collection_name).stream()
    
    updated_count = 0
    skipped_count = 0
    missing_timeago_count = 0

    for doc in docs:
        # Skip the index document
        if doc.id == ALL_IDS_DOC:
            continue

        data = doc.to_dict()
        time_ago_val = data.get("timeAgo")

        # If timeAgo exists, copy it to timestamp
        if time_ago_val is not None:
            # Smart Check: Only write if the timestamp is missing or different
            if data.get("timestamp") != time_ago_val:
                # SAFE OVERWRITE: update() only touches the specified field
                db.collection(collection_name).document(doc.id).update({
                    "timestamp": str(time_ago_val) # Cast to string just to be safe
                })
                updated_count += 1
                
                if updated_count % 50 == 0:
                    print(f"🔄 Updated {updated_count} documents so far...")
            else:
                # They are already exactly the same, save a database write
                skipped_count += 1
        else:
            missing_timeago_count += 1

    print(f"✅ Finished {collection_name}:")
    print(f"   ➤ Successfully Updated : {updated_count}")
    print(f"   ➤ Skipped (Already OK): {skipped_count}")
    if missing_timeago_count > 0:
        print(f"   ⚠️ Docs missing timeAgo: {missing_timeago_count}")


# ---------------- EXECUTION ----------------
if __name__ == "__main__":
    print("🚀 Starting Timestamp Sync...")
    
    # Process App 1
    sync_timestamp_from_timeago(db_gurbani, COLLECTION_GURBANI)
    
    # Process App 2
    sync_timestamp_from_timeago(db_harmandir, COLLECTION_HARMANDIR)
    
    print("\n🏁 Process Complete!")
