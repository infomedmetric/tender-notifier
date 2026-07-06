import os
import time
import requests
import threading
from datetime import datetime
from flask import Flask
from bs4 import BeautifulSoup
import urllib3

# Suppress insecure request warnings from using verify=False
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

KEYWORDS = [
    "ICB TENDER FOR MEDICAL EQUIPMENT", "medical equipment maintenance",
    "preventive and Corrective Maintenance", "Hemodialysis", 
    "water treatment Hemodialysis", "biomedical", "maintenance and repair"
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
            if not title_el: continue
            
            title_text = title_el.get_text().strip()
            link = "https://www.2merkato.com" + title_el['href'] if title_el['href'].startswith('/') else title_el['href']
            
            if any(kw.lower() in title_text.lower() for kw in KEYWORDS):
                tender_id = f"merkato_{link.split('/')[-1]}"
                if tender_id not in NOTIFIED_TENDERS:
                    found += 1
                    NOTLIMIT = NOTIFIED_TENDERS.add(tender_id)
                    
                    alert = f"🔔 *New 2merkato Tender Found!*\n\n📋 *Title:* {title_text}\n🔗 *Link:* {link}"
                    send_whatsapp(alert)
                    time.sleep(2)
        return found
    except Exception as e:
        print(f"❌ 2merkato engine error: {e}", flush=True)
        return 0

# ==================== ENGINE 2: ETHIOPIAN eGP RSS FEED ====================
def scrape_egp():
    print(f"[{datetime.now()}] 🔍 Running eGP Syndicated Feed Engine...", flush=True)
    
    # Using the portal's dedicated open feed endpoint instead of dynamic web routes
    url = "https://egp.ppa.gov.et/egp/bidding/tender/rss/published"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/rss+xml, application/xml, text/xml"
    }
    
    try:
        res = requests.get(url, headers=headers, verify=False, timeout=15)
        found = 0
        
        if res.status_code == 200:
            # Parse the incoming XML payload using xml-parser configuration
            soup = BeautifulSoup(res.text, 'xml')
            items = soup.find_all('item')
            
            for item in items:
                title_text = item.find('title').get_text().strip() if item.find('title') else ""
                link_text = item.find('link').get_text().strip() if item.find('link') else ""
                desc_text = item.find('description').get_text().strip() if item.find('description') else ""
                
                # Combine parameters to search for matches across descriptions and fields
                search_scope = f"{title_text} {desc_text}"
                
                if any(kw.lower() in search_scope.lower() for kw in KEYWORDS):
                    # Use the link identifier token as the lookup signature
                    tender_uid = link_text.split('/')[-1] if '/' in link_text else str(hash(title_text))
                    tender_id = f"egp_feed_{tender_uid}"
                    
                    if tender_id not in NOTIFIED_TENDERS:
                        found += 1
                        NOTIFIED_TENDERS.add(tender_id)
                        
                        alert = f"🏛️ *New Government eGP Tender!*\n\n" \
                                f"📋 *Procurement:* {title_text}\n\n" \
                                f"📝 *Context Summary:* {desc_text[:280]}...\n\n" \
                                f"🔗 *Portal Link:* {link_text}"
                        
                        send_whatsapp(alert)
                        time.sleep(2)
        else:
            print(f"⚠️ eGP Feed Engine fallback status: {res.status_code}", flush=True)
        return found
    except Exception as e:
        print(f"❌ eGP XML Feed parser runtime discrepancy: {e}", flush=True)
        return 0

# ==================== RUN COORDINATOR ====================
def check_for_tenders():
    print(f"=================== STARTING SCAN CYCLE ===================", flush=True)
    m_count = scrape_2merkato()
    e_count = scrape_egp()
    total = m_count + e_count
    print(f"=================== SCAN COMPLETE: {total} NEW FOUND ===================", flush=True)
    
    if total == 0:
        send_whatsapp("🔍 *Side-by-Side Scan Completed.*\nNo new updates detected on 2merkato or the eGP Portal.\nNext automatic sync in 4 hours.")

def monitoring_loop():
    while True:
        check_for_tenders()
        time.sleep(4 * 3600)

# ==================== FLASK ROUTES ====================
@app.route('/')
def home():
    return "Dual-Engine Tender Notifier is running!", 200

@app.route('/test-check')
def manual_test():
    threading.Thread(target=check_for_tenders).start()
    return "Side-by-side dynamic scraper check initiated! Watch WhatsApp and logs.", 200

if __name__ == "__main__":
    threading.Thread(target=monitoring_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

