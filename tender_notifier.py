import os
import time
import requests
import threading
from datetime import datetime
from flask import Flask
from bs4 import BeautifulSoup

app = Flask(__name__)

# ================== CONFIGURATION ==================
EVOLUTION_BASE = os.environ.get("EVOLUTION_BASE", "https://medmetric-evolution.onrender.com")
INSTANCE_NAME = os.environ.get("INSTANCE_NAME", "Tender-Notifier.")
GLOBAL_API_KEY = os.environ.get("GLOBAL_API_KEY", "143EC4F4C954-4014-BCCD-FC294B1A5609")
WHATSAPP_NUMBER = os.environ.get("WHATSAPP_NUMBER", "251901748874")

# Secure 2merkato Logins pulled from your environment variables
MERKATO_USER = os.environ.get("MERKATO_USER")
MERKATO_PASS = os.environ.get("MERKATO_PASS")

KEYWORDS = [
    "ICB TENDER FOR MEDICAL EQUIPMENT", "medical equipment maintenance",
    "preventive and Corrective Maintenance", "Hemodialysis", 
    "water treatment Hemodialysis", "biomedical", "maintenance and repair"
]

# Keeps track of already notified tenders to prevent spamming your WhatsApp
NOTIFIED_TENDERS = set()

def send_whatsapp(message):
    url = f"{EVOLUTION_BASE}/message/sendText/{INSTANCE_NAME}"
    payload = {
        "number": WHATSAPP_NUMBER,
        "text": message
    }
    headers = {
        "Content-Type": "application/json",
        "apikey": GLOBAL_API_KEY
    }
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        print(f"✅ WhatsApp Status: {r.status_code}", flush=True)
    except Exception as e:
        print(f"❌ Send error: {e}", flush=True)

def check_for_tenders():
    print(f"[{datetime.now()}] 🔍 Initiating 2merkato Scraper Engine...", flush=True)
    
    if not MERKATO_USER or not MERKATO_PASS:
        print("⚠️ Missing 2merkato credentials in environment variables!", flush=True)
        return

    session = requests.Session()
    
    # 2merkato standard login endpoints and payload headers
    login_url = "https://www.2merkato.com/index.php?option=com_users&task=user.login"
    login_data = {
        "username": MERKATO_USER,
        "password": MERKATO_PASS,
        "return": "aHR0cHM6Ly93d3cuMm1lcmthdG8uY29tL3RlbmRlcnM=" # Redirect to tenders board base
    }
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    try:
        # Step 1: Login to establish authenticated session cookie
        login_res = session.post(login_url, data=login_data, headers=headers, timeout=15)
        print(f"🔑 Login Session Handshake: {login_res.status_code}", flush=True)
        
        # Step 2: Fetch the latest medical & operational tender notice categories
        # This points to the specialized medical/hospital supplies categories list
        tenders_url = "https://www.2merkato.com/tenders/category/25-medical-equipment-and-supplies"
        res = session.get(tenders_url, headers=headers, timeout=15)
        
        soup = BeautifulSoup(res.text, 'html.parser')
        
        # Find all tender list item blocks (common layout class selector for 2merkato listings)
        items = soup.find_all('div', class_='tender-block') or soup.find_all('tr', class_='tender-row')
        
        found_matches = 0
        
        for item in items:
            # Extract Text Details safely
            title_el = item.find('a')
            if not title_el:
                continue
                
            title_text = title_el.get_text().strip()
            link = "https://www.2merkato.com" + title_el['href'] if title_el['href'].startswith('/') else title_el['href']
            
            # Extract closing details/entity if available in sub-elements
            meta_text = item.get_text()
            
            # Match keywords against text patterns
            if any(kw.lower() in title_text.lower() for kw in KEYWORDS):
                tender_id = link.split('/')[-1] # Unique slug to identify item uniqueness
                
                if tender_id not in NOTIFIED_TENDERS:
                    found_matches += 1
                    NOTIFIED_TENDERS.add(tender_id)
                    
                    alert_msg = f"""🔔 *New Biomedical Tender Scraped!*

📋 *Title:* {title_text}
🔗 *Source Link:* {link}

_Please log in to your 2merkato portal panel to view full bid bonds and documentation specifications._"""
                    
                    send_whatsapp(alert_msg)
                    time.sleep(2) # Avoid aggressive rapid API firing
                    
        print(f"🎯 Scan Complete. Discovered {found_matches} new relevant listings.", flush=True)
        
        if found_matches == 0:
            send_whatsapp("🔍 Live 2merkato scan complete. No new medical or hemodialysis maintenance tenders found matching your keywords right now.\nNext auto-check in 4 hours.")

    except Exception as e:
        print(f"❌ Scraper loop encountered an exception: {e}", flush=True)
        send_whatsapp(f"⚠️ Scraper Engine Error: System failed to fetch data from source ({e}). Check server logs.")

def monitoring_loop():
    while True:
        check_for_tenders()
        time.sleep(4 * 3600) # Check every 4 hours

# ==================== FLASK ROUTES ====================
@app.route('/')
def home():
    return "Tender Notifier Live Engine running!", 200

@app.route('/test-check')
def manual_test():
    threading.Thread(target=check_for_tenders).start()
    return "Manual live 2merkato scraper sequence triggered! Check WhatsApp and logs.", 200

if __name__ == "__main__":
    threading.Thread(target=monitoring_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
