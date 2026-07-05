import requests
import time
from datetime import datetime

# ================== CONFIG ==================
EVOLUTION_BASE = "https://medmetric-evolution.onrender.com"
INSTANCE_TOKEN = "143EC4F4C954-4014-BCCD-FC294B1A5609"
WHATSAPP_NUMBER = "251901748874"   # Your number

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
    
    # TODO: Replace with real fetching (eGP aggregator or scraping)
    # Example using a public aggregator API (add your choice)
    try:
        # Placeholder - replace with real call
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
        # Fallback message
        send_whatsapp("🔍 No new matching tenders in this check.\nKeywords monitored: Medical maintenance, ICB, Hemodialysis.\nNext check in 4 hours.")

# Main loop - every 4 hours
while True:
    check_for_tenders()
    time.sleep(4 * 3600)   # 4 hours
