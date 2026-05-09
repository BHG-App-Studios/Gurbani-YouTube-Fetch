#!/usr/bin/env python3
import firebase_admin
from firebase_admin import credentials, firestore
import json
import os
import sys

# ---------------- CONFIGURATION ----------------
COLLECTION_GURBANI = "Listen_Kirtans_Videos_New"
COLLECTION_HARMANDIR = "Kirtan-Youtube-Videos"

# Env variables
SERVICE_ACCOUNT_GURBANI = os.environ.get("FIREBASE_SERVICE_ACCOUNT_GURBANI")
SERVICE_ACCOUNT_HARMANDIR = os.environ.get("FIREBASE_SERVICE_ACCOUNT_HARMANDIR")

if not SERVICE_ACCOUNT_GURBANI or not SERVICE_ACCOUNT_HARMANDIR:
    print("❌ Missing required Environment Variables.")
    sys.exit(1)

# ---------------- FIREBASE INIT ----------------
print("🔌 Initializing Firebase Connections...")
cred_gurbani = credentials.Certificate(json.loads(SERVICE_ACCOUNT_GURBANI))
app_gurbani = firebase_admin.initialize_app(cred_gurbani, name='gurbani_app_fixer')
db_gurbani = firestore.client(app=app_gurbani)

cred_harmandir = credentials.Certificate(json.loads(SERVICE_ACCOUNT_HARMANDIR))
app_harmandir = firebase_admin.initialize_app(cred_harmandir, name='harmandir_app_fixer')
db_harmandir = firestore.client(app=app_harmandir)

# ---------------- HELPER METHOD ----------------
def fix_timestamps_in_collection(db, collection_name, app_label):
    """
    Streams all documents, reads 'timeAgo', and overwrites 'timestamp'
    using Firestore Batch Writes for speed and safety.
    """
    print(f"\n🔍 Scanning {app_label} database...")
    docs = db.collection(collection_name).stream()
    
    batch = db.batch()
    operations_in_batch = 0
    total_updated = 0
    skipped = 0

    for doc in docs:
        data = doc.to_dict()
        time_ago = data.get("timeAgo")
        
        # Only update if timeAgo actually exists in this document
        if time_ago:
            # .update() modifies only the specified field. 
            # We are setting timestamp exactly equal to timeAgo.
            batch.update(doc.reference, {"timestamp": time_ago})
            operations_in_batch += 1
            total_updated += 1
        else:
            skipped += 1
            print(f"⚠️ Skipped {doc.id} - No 'timeAgo' field found.")

        # Firestore allows a maximum of 500 writes per batch. 
        # We commit at 400 to be perfectly safe.
        if operations_in_batch >= 400:
            print(f"💾 Committing batch of {operations_in_batch} updates...")
            batch.commit()
            batch = db.batch() # Start a new batch
            operations_in_batch = 0

    # Commit any remaining operations in the final batch
    if operations_in_batch > 0:
        print(f"💾 Committing final batch of {operations_in_batch} updates...")
        batch.commit()

    print(f"✅ Finished {app_label}: {total_updated} fixed, {skipped} skipped.")
    return total_updated

# ---------------- MAIN LOGIC ----------------
print("\n🚀 Starting Timestamp Correction...")

# Run for Gurbani App
updated_gurbani = fix_timestamps_in_collection(db_gurbani, COLLECTION_GURBANI, "Gurbani App")

# Run for Harmandir App
updated_harmandir = fix_timestamps_in_collection(db_harmandir, COLLECTION_HARMANDIR, "Harmandir App")

# ---------------- SUMMARY ----------------
print("\n================ SUMMARY ================")
print(f"✅ Gurbani Docs Fixed   : {updated_gurbani}")
print(f"✅ Harmandir Docs Fixed : {updated_harmandir}")
print("========================================")
print("🎉 All sorting timestamps restored successfully!")
