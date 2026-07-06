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

# Expanded, resilient verification keywords
KEYWORDS = [
    "the", "supply", "Hemodialysis", "Dialysis", "medical equipment maintenance", 
    "water treatment", "b.braun", "dialog+", "biomedical", "የህክምና", "ጥገና"
]

# Tracks already notified tenders globally across both engines
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
    print(f"[{datetime.now()}] 🔍 Running eGP API Engine...", flush=True)
    
    # Layer 1: Target the public bids endpoint layer directly to avoid empty JS HTML shells
    primary_url = "https://production.egp.gov.et/api/v1/public/bids?page=0&size=40"
    fallback_url = "https://production.egp.gov.et/api/public/bids?page=0&size=40"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json"
    }
    
    res = None
    try:
        res = requests.get(primary_url, headers=headers, timeout=15, verify=False)
        if res.status_code != 200:
            print(f"⚠️ Primary URL status {res.status_code}. Attempting fallback route...", flush=True)
            res = requests.get(fallback_url, headers=headers, timeout=15, verify=False)
    except Exception as net_err:
        print(f"⚠️ Primary route connection failed ({net_err}). Attempting fallback...", flush=True)
        try:
            res = requests.get(fallback_url, headers=headers, timeout=15, verify=False)
        except Exception as e:
            print(f"❌ Both eGP API routes failed: {e}", flush=True)
            return 0

    if not res or res.status_code != 200:
        print(f"❌ eGP API unreachable. Status Code: {res.status_code if res else 'No Response'}", flush=True)
        return 0

    try:
        data = res.json()
        # Parse data out of the standard API wrapper wrappers safely
        tenders = data.get("content", []) or data.get("data", []) or data.get("bids", []) or []
        found = 0
        
        print(f"📡 eGP API returned {len(tenders)} recent records.", flush=True)
        
        for tender in tenders:
            # Resilient key lookup to match variable internal database schemas
            title = tender.get("title", "") or tender.get("bidDescription", "") or tender.get("tenderTitle", "") or tender.get("description", "") or ""
            org = tender.get("organizationName", "") or tender.get("buyerName", "") or tender.get("procuringEntity", "") or ""
            bid_id = tender.get("id", "") or tender.get("bidNumber", "") or tender.get("tenderReferenceNumber", "")
            
            if not title and not org:
                continue
                
            combined_text = f"{title} {org}".lower()
            
            if any(kw.lower() in combined_text for kw in KEYWORDS):
                # Generate unique hash fingerprint based on metadata signature if ID fields are missing
                unique_sig = bid_id if bid_id else str(hash(title + org))
                tender_id = f"egp_prod_{unique_sig}"
                
                if tender_id not in NOTIFIED_TENDERS:
                    NOTIFIED_TENDERS.add(tender_id)
                    found += 1
                    
                    link = f"https://production.egp.gov.et/egp/bids/view/{bid_id}" if bid_id else "https://production.egp.gov.et/egp/bids/all"
                    
                    alert = f"🏛️ *New Production eGP Match!*\n\n" \
                            f"🏢 *Entity:* {org if org else 'Not Specified'}\n" \
                            f"📋 *Notice:* {title}\n" \
                            f"🔗 *Link:* {link}"
                    send_whatsapp(alert)
                    time.sleep(2)
                    
        print(f"eGP backend processing complete. Found {found} matches.", flush=True)
        return found
    except Exception as parse_err:
        print(f"❌ Error decoding or parsing eGP JSON structural payload: {parse_err}", flush=True)
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
