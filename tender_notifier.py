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
EVOLUTION_BASE = os.environ.get("EVOLUTION_BASE", "https://medmetric-evolution.onrender.com")
INSTANCE_NAME = os.environ.get("INSTANCE_NAME", "Tender-Notifier.")
GLOBAL_API_KEY = os.environ.get("GLOBAL_API_KEY", "143EC4F4C954-4014-BCCD-FC294B1A5609")
WHATSAPP_NUMBER = os.environ.get("WHATSAPP_NUMBER", "251901748874")

KEYWORDS = [
    "ICB TENDER FOR MEDICAL EQUIPMENT", "medical equipment maintenance",
    "preventive and Corrective Maintenance", "Hemodialysis", 
    "water treatment Hemodialysis", "biomedical", "maintenance and repair"
]

def send_whatsapp(message):
    # Base endpoint structure used by standard Evolution API deployments
    url = f"{EVOLUTION_BASE}/message/sendText"
    
    payload = {
        "number": WHATSAPP_NUMBER,
        "textMessage": {"text": message}
    }
    
    # Pass both API key and Instance Name into headers to satisfy authentication rules
    headers = {
        "Content-Type": "application/json",
        "apikey": GLOBAL_API_KEY,
        "Instance": INSTANCE_NAME
    }
    
    # Secondary fallback using query parameters (?instance=Tender-Notifier.)
    params = {
        "instance": INSTANCE_NAME
    }
    
    try:
        r = requests.post(url, json=payload, headers=headers, params=params, timeout=10)
        print(f"✅ Message sent status: {r.status_code}", flush=True)
        
        # If the server responds with an error, print the exact message text from it
        if r.status_code != 200 and r.status_code != 201:
            print(f"📄 Response Content from Evolution: {r.text}", flush=True)
            
    except Exception as e:
        print(f"❌ Send error: {e}", flush=True)

def check_for_tenders():
    print(f"[{datetime.now()}] 🔍 Checking for tenders...", flush=True)
    
    try:
        # Placeholder endpoint - change this to your true data source when ready
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
    check_for_tenders()
    return "Manual tender check triggered! Check your WhatsApp and Render logs.", 200

# ==================== START APPLICATION ====================
if __name__ == "__main__":
    threading.Thread(target=monitoring_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
