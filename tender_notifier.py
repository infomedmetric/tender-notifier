import os
import time
import requests
import threading
from datetime import datetime
from flask import Flask
from playwright.sync_api import sync_playwright
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# ================== CONFIG ==================
EVOLUTION_BASE = os.environ.get("EVOLUTION_BASE", "https://medmetric-evolution.onrender.com")
INSTANCE_TOKEN = os.environ.get("INSTANCE_TOKEN", "143EC4F4C954-4014-BCCD-FC294B1A5609")
WHATSAPP_NUMBER = os.environ.get("WHATSAPP_NUMBER", "251901748874")

KEYWORDS = ["medical", "equipment", "maintenance", "repair", "biomedical", "hemodialysis", "ICB", "hospital"]

NOTIFIED = set()

def send_whatsapp(message):
    url = f"{EVOLUTION_BASE}/message/sendText/{INSTANCE_TOKEN}"
    payload = {"number": WHATSAPP_NUMBER, "text": message}
    headers = {"Content-Type": "application/json", "apikey": INSTANCE_TOKEN}
    try:
        requests.post(url, json=payload, headers=headers, timeout=10)
        print("✅ Sent to WhatsApp")
    except Exception as e:
        print(f"❌ Send error: {e}")

# ================== IMPROVED 2MERKATO WITH PAGINATION ==================
def scrape_2merkato():
    print(f"[{datetime.now()}] 🔍 Scraping 2merkato with pagination...")
    found = 0
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page()
            page.goto("https://tender.2merkato.com/tenders", timeout=60000)
            page.wait_for_load_state("networkidle")

            page_num = 1
            while True:
                print(f"Page {page_num}...")
                items = page.locator("a[href*='/tenders/']").all()
                for item in items:
                    title = item.inner_text().strip()
                    link = item.get_attribute("href")
                    if link and any(k.lower() in title.lower() for k in KEYWORDS):
                        full_link = "https://tender.2merkato.com" + link if link.startswith('/') else link
                        uid = full_link
                        if uid not in NOTIFIED:
                            NOTIFIED.add(uid)
                            found += 1
                            send_whatsapp(f"🔔 *New Tender!*\n{title}\n{full_link}")
                # Try next page
                next_btn = page.locator("a[rel='next'], button:has-text('Next'), li.next a")
                if next_btn.count() > 0 and "disabled" not in next_btn.get_attribute("class") or "":
                    next_btn.click()
                    page.wait_for_timeout(3000)
                    page_num += 1
                else:
                    break

            browser.close()
        return found
    except Exception as e:
        print(f"2merkato error: {e}")
        return 0

# ================== eGP SCRAPER ==================
def scrape_egp():
    print(f"[{datetime.now()}] 🔍 Scraping eGP...")
    urls = ["https://production.egp.gov.et/egp/bids/all"]
    found = 0
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page()
            for url in urls:
                page.goto(url, timeout=60000)
                page.wait_for_load_state("networkidle")
                rows = page.locator("tr, div.tender").all()
                for row in rows:
                    text = row.inner_text().strip()
                    if any(k.lower() in text.lower() for k in KEYWORDS):
                        uid = hash(text[:100])
                        if uid not in NOTIFIED:
                            NOTIFIED.add(uid)
                            found += 1
                            send_whatsapp(f"🏛️ *eGP Tender Match!*\n{text[:250]}...\nCheck eGP portal")
            browser.close()
        return found
    except Exception as e:
        print(f"eGP error: {e}")
        return 0

# Run
def check_tenders():
    scrape_2merkato()
    scrape_egp()

if __name__ == "__main__":
    threading.Thread(target=lambda: [time.sleep(4*3600), check_tenders()] * 100, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
