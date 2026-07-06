import os
import time
import requests
import threading
from datetime import datetime
from flask import Flask

# Initialize Flask for Render web port binding
app = Flask(__name__)

# ================== CONFIGURATION ==================
# Pulls variables safely from Render's Environment Variables.
# If they aren't set in Render, it defaults to your provided strings.
EVOLUTION_BASE = os.environ.get("EVOLUTION_BASE", "https://medmetric-evolution.onrender.com")
INSTANCE_TOKEN = os.environ.get("INSTANCE_TOKEN", "143EC4F4C954-4014-BCCD-FC294B1A5609")
WHATSAPP_NUMBER = os.environ.get("WHATSAPP_NUMBER", "251901748874")

KEYWORDS = [
    "ICB TENDER FOR MEDICAL EQUIPMENT", "medical equipment maintenance",
    "preventive and Corrective Maintenance", "Hemodialysis", 
    "water treatment Hemodialysis", "biomedical", "maintenance and repair"
]

def send_whatsapp(message):
    url = f"{EVOLUTION_BASE}/message/sendText/{INSTANCE_TOKEN}"
    payload = {
        "number": WHATSAPP_NUMBER,
        "textMessage": {"text": message}
    }
    try:
        # Added timeout=10 to prevent the requests library from hanging indefinitely
        r = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=10)
        print(f"✅ Message sent: {r.status_code}", flush=True)
    except Exception as e:
        print(f"❌ Send error: {e}", flush=True)

def check_for_tenders():
    print(f"[{datetime.now()}] 🔍 Checking for tenders...", flush=True)
    
    try:
        # Placeholder endpoint - change this to your actual eGP/Scraping API when ready
        response = requests.get("https://example-tender-api.com/search?keywords=medical+maintenance", timeout=10)
        data = response.json()
        
        for tender in data.get("tenders", []):
            title = tender.get("title", "").lower()
            if any(kw.lower() in title for kw in KEYWORDS):
                msg = f"""🔔 *New Relevant Tender Found!*

Title: {tender.get('title')}
Entity: {tender.get('entity')}
Deadline: {tender.get('deadline')}
Link: {tender.get('link')}

Check eGP and bid quickly!"""
                send_whatsapp(msg)
    except Exception as e:
        # Fallback message fires if the placeholder API fails
        print(f"⚠️ Fetch failed ({e}). Running fallback notification...", flush=True)
        send_whatsapp("🔍 No new matching tenders in this check.\nKeywords monitored: Medical maintenance, ICB, Hemodialysis.\nNext check in 4 hours.")

# Background monitoring loop running in a separate thread
def monitoring_loop():
    while True:
        check_for_tenders()
        time.sleep(4 * 3600)   # Sleep for 4 hours

# ==================== FLASK ROUTES ====================

@app.route('/')
def home():
    return "Tender Notifier is running!", 200

@app.route('/test-check')
def manual_test():
    # Force an immediate check when you visit this URL
    check_for_tenders()
    return "Manual tender check triggered! Check your WhatsApp and Render logs.", 200

# ==================== START APPLICATION ====================
# This block must always remain at the absolute bottom of the script
if __name__ == "__main__":
    # Start the continuous 4-hour background loop thread
    threading.Thread(target=monitoring_loop, daemon=True).start()
    
    # Get port assigned by Render dynamically, or default to 5000 locally
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
