import os
import time
import requests
import threading
from datetime import datetime
from flask import Flask
from bs4 import BeautifulSoup
import urllib3

# Suppress insecure connection warnings due to verify=False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# ================== CONFIGURATION ==================
EVOLUTION_BASE = os.environ.get("EVOLUTION_BASE", "https://medmetric-evolution.onrender.com")
INSTANCE_NAME = os.environ.get("INSTANCE_NAME", "Tender-Notifier.")
GLOBAL_API_KEY = os.environ.get("GLOBAL_API_KEY", "143EC4F4C954-4014-BCCD-FC294B1A5609")
WHATSAPP_NUMBER = os.environ.get("WHATSAPP_NUMBER", "251901748874")

# Secure 2merkato Logins
MERKATO_USER = os.environ.get("MERKATO_USER")
MERKATO_PASS = os.environ.get("MERKATO_PASS")

# Direct testing keywords alongside targeted medical keywords
KEYWORDS = [
    "the", "supply", "Hemodialysis", "Dialysis", "medical equipment maintenance", 
    "water treatment", "b.braun", "dialog+", "biomedical", "የህክምና", "ጥገና"
]

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

# ==================== ENGINE 1: 2MERKATO ====================
def scrape_2merkato():
    print(f"[{datetime.now()}] 🔍 Running 2merkato Engine...", flush=True)
    if not MERKATO_USER or not MERKATO_PASS:
        print("⚠️ Missing 2merkato credentials!", flush=True)
        return 0

    session = requests.Session()
    login_url = "https://www.2merkato.com/index.php?option=com_users&task=user.login"
    login_data = {
        "username": MERKATO_USER,
        "password": MERKATO_PASS,
        "return": "aHR0cHM6Ly93d3cuMm1lcmthdG8uY29tL3RlbmRlcnM="
    }
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    try:
        session.post(login_url, data=login_data, headers=headers, timeout=15)
        tenders_url = "https://www.2merkato.com/tenders/category/25-medical-equipment-and-supplies"
        res = session.get(tenders_url, headers=headers, timeout=15)
        soup = BeautifulSoup(res.text, 'html.parser')
        
        items = soup.find_all('div', class_='tender-block') or soup.find_all('tr', class_='tender-row')
        found = 0
        
        for item in items:
            title_el = item.find('a')
            if not title_el: 
                continue
            
            title_text = title_el.get_text().strip()
            link = "https://www.2merkato.com" + title_el['href'] if title_el['href'].startswith('/') else title_el['href']
            
            if any(kw.lower() in title_text.lower() for kw in KEYWORDS):
                tender_id = f"merkato_{link.split('/')[-1]}"
                if tender_id not in NOTIFIED_TENDERS:
                    found += 1
                    NOTIFIED_TENDERS.add(tender_id)
                    
                    alert = f"🔔 *New 2merkato Tender Found!*\n\n📋 *Title:* {title_text}\n🔗 *Link:* {link}"
                    send_whatsapp(alert)
                    time.sleep(2)
        return found
    except Exception as e:
        print(f"❌ 2merkato engine error: {e}", flush=True)
        return 0

# ==================== ENGINE 2: ETHIOPIAN eGP PORTAL ====================
def scrape_egp():
    print(f"[{datetime.now()}] 🔍 Running eGP Public Portal Web Directory Scraper...", flush=True)
    
    # Scrapes the static web archives meant for public lookups instead of protected JSON endpoints
    url = "https://production.egp.gov.et/egp/bids/all"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    
    try:
        res = requests.get(url, headers=headers, timeout=15, verify=False)
        if res.status_code != 200:
            print(f"❌ eGP Server returned non-200 status block: {res.status_code}", flush=True)
            return 0
            
        soup = BeautifulSoup(res.text, 'html.parser')
        found = 0
        
        # Scrape rows, table elements, lists, or blocks that hold raw display layout strings
        elements = soup.find_all(['td', 'tr', 'div', 'p', 'a'])
        
        for element in elements:
            text = element.get_text().strip()
            # Avoid processing short strings or empty styling blocks
            if len(text) < 15 or len(text) > 400:
                continue
                
            if any(kw.lower() in text.lower() for kw in KEYWORDS):
                # Hash the visible string snippet to uniquely trace notifications
                tender_uid = str(hash(text))
                tender_id = f"egp_web_{tender_uid}"
                
                if tender_id not in NOTIFIED_TENDERS:
                    NOTIFIED_TENDERS.add(tender_id)
                    found += 1
                    
                    alert = f"🏛️ *New eGP Portal Match!*\n\n📋 *Details Extracted:*\n{text[:300]}...\n\n🔗 *Link:* {url}"
                    send_whatsapp(alert)
                    time.sleep(2)
                    
        print(f"eGP Web processing complete. Found {found} matches.", flush=True)
        return found
    except Exception as e:
        print(f"❌ eGP Web processing failure channel exception: {e}", flush=True)
        return 0

# ==================== RUN COORDINATOR ====================
def check_for_tenders():
    print(f"=================== STARTING SCAN CYCLE ===================", flush=True)
    m_count = scrape_2merkato()
    e_count = scrape_egp()
    total = m_count + e_count
    print(f"=================== SCAN COMPLETE: {total} NEW FOUND ===================", flush=True)
    
    if total == 0:
        send_whatsapp("🔍 *Side-by-Side Scan Completed.*\nNo new unique matches discovered on 2merkato or the eGP Portal.\nNext automatic sync in 4 hours.")

def monitoring_loop():
    while True:
        check_for_tenders()
        time.sleep(4 * 3600)

# ==================== FLASK ROUTES ====================
@app.route('/')
def home():
    return "Dual-Engine Tender Notifier is running smoothly!", 200

@app.route('/test-check')
def manual_test():
    threading.Thread(target=check_for_tenders).start()
    return "Side-by-side dynamic scraper check initiated! Watch WhatsApp and logs.", 200

if __name__ == "__main__":
    threading.Thread(target=monitoring_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

