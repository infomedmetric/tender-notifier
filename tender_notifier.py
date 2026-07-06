import requests
import time
from datetime import datetime
import threading
import os
from flask import Flask

# Initialize Flask for Render web port binding
app = Flask(__name__)

# ================== CONFIG ==================
EVOLUTION_BASE = "https://medmetric-evolution.onrender.com"
INSTANCE_TOKEN = "143EC4F4C954-4014-BCCD-FC294B1A5609"
WHATSAPP_NUMBER = "251901748874"

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
        r = requests.post(url, json=payload, headers={"Content-Type": "application/json"})
        print(f"✅ Message sent: {r.status_code}")
    except Exception as e:
        print(f"❌ Send error: {e}")

def check_for_tenders():
    print(f"[{datetime.now()}] 🔍 Checking for tenders...")
    try:
        response = requests.get("https://example-tender-api.com/search?keywords=medical+maintenance")
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
    except:
        send_whatsapp("🔍 No new matching tenders in this check.\nKeywords monitored: Medical maintenance, ICB, Hemodialysis.\nNext check in 4 hours.")

# Continuous monitoring loop running in a separate thread
def monitoring_loop():
    while True:
        check_for_tenders()
        time.sleep(4 * 3600)  # Sleep for 4 hours

# Flask health check route for Render
@app.route('/')
def home():
    return "Tender Notifier is running!", 200

if __name__ == "__main__":
    # Start the tender loop in the background
    threading.Thread(target=monitoring_loop, daemon=True).start()
    
    # Get port assigned by Render or default to 5000 locally
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
    
@app.route('/test-check')
def manual_test():
    check_for_tenders()
    return "Manual tender check triggered! Check your WhatsApp and Render logs.", 200

