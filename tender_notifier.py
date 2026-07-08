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

# ================== CONFIGURATION ==================
EVOLUTION_BASE = os.environ.get("EVOLUTION_BASE", "https://medmetric-evolution.onrender.com")
INSTANCE_NAME = os.environ.get("INSTANCE_NAME", "Tender-Notifier.")
GLOBAL_API_KEY = os.environ.get("GLOBAL_API_KEY", "143EC4F4C954-4014-BCCD-FC294B1A5609")
WHATSAPP_NUMBERS = [
    n.strip() for n in os.environ.get("WHATSAPP_NUMBERS", "251901748874, 251930098777, 251946260018").split(",") if n.strip()
]

MERKATO_USER = os.environ.get("MERKATO_USER")
MERKATO_PASS = os.environ.get("MERKATO_PASS")

# CORRECT domain — the working tenders app, not www.2merkato.com
MERKATO_BASE = "https://tender.2merkato.com"
MERKATO_LOGIN_URL = f"{MERKATO_BASE}/login"
MERKATO_TENDERS_URL = f"{MERKATO_BASE}/tenders"

# Any ONE of these alone is specific enough to trigger a match
STRONG_KEYWORDS = [
    "biomedical", "hemodialysis", "dialysis", "b.braun", "dialog+",
    "x-ray", "xray", "ultrasound", "ventilator", "autoclave", "sterilizer",
    "diagnostic equipment", "medical equipment", "hospital equipment",
    "laboratory equipment", "medical device", "የህክምና", "ጥገና"
]

# Generic medical-adjacent words — only count if paired with an equipment/
# procurement-type word in the same title (avoids matching HR/insurance/
# consulting tenders that merely mention "health")
MEDICAL_CONTEXT = ["medical", "health", "hospital", "biomedical", "clinical", "laboratory"]
EQUIPMENT_CONTEXT = ["equipment", "supplies", "supply", "device", "machine",
                     "instrument", "apparatus", "maintenance", "repair", "procurement"]

# If any of these appear, skip regardless of other matches — these are the
# recurring false-positive categories (vehicle maintenance, insurance, consulting)
EXCLUDE_TERMS = [
    "vehicle", "toyota", "car ", "motorbike", "insurance", "life insurance",
    "term life", "gpa", "consultancy services", "consulting firm"
]


def is_relevant_tender(title):
    title_lower = title.lower()

    if any(term in title_lower for term in EXCLUDE_TERMS):
        return False

    if any(term in title_lower for term in STRONG_KEYWORDS):
        return True

    has_medical_context = any(term in title_lower for term in MEDICAL_CONTEXT)
    has_equipment_context = any(term in title_lower for term in EQUIPMENT_CONTEXT)

    return has_medical_context and has_equipment_context

NOTIFIED_TENDERS = set()


def send_whatsapp(message):
    url = f"{EVOLUTION_BASE}/message/sendText/{INSTANCE_NAME}"
    headers = {"Content-Type": "application/json", "apikey": GLOBAL_API_KEY}
    for number in WHATSAPP_NUMBERS:
        payload = {"number": number, "text": message}
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=10)
            print(f"✅ WhatsApp Status ({number}): {r.status_code}", flush=True)
        except Exception as e:
            print(f"❌ Send error ({number}): {e}", flush=True)


# ==================== ENGINE: 2MERKATO (Playwright) ====================
def scrape_2merkato():
    print(f"[{datetime.now()}] 🔍 Running 2merkato Engine (Playwright)...", flush=True)
    found = 0

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            page = context.new_page()

            # --- Login (optional — only if credentials provided) ---
            if MERKATO_USER and MERKATO_PASS:
                try:
                    # domcontentloaded instead of networkidle — SPA sites with
                    # ads/analytics often never go fully idle, causing false timeouts
                    page.goto(MERKATO_LOGIN_URL, timeout=30000, wait_until="domcontentloaded")

                    # Wait specifically for a password field to render, rather than
                    # waiting for the whole network to go quiet
                    page.wait_for_selector("input[type='password']", timeout=20000)

                    # Diagnostic dump — logs every input field's actual attributes so we
                    # can see the real markup if selectors below don't match
                    field_info = page.eval_on_selector_all(
                        "input",
                        "els => els.map(e => ({type: e.type, name: e.name, id: e.id, placeholder: e.placeholder}))"
                    )
                    print(f"🔎 Login page input fields detected: {field_info}", flush=True)

                    email_field = page.locator(
                        "input[type='email'], input[name*='email' i], input[name*='user' i], "
                        "input[id*='email' i], input[id*='user' i], input[placeholder*='email' i]"
                    ).first
                    pass_field = page.locator("input[type='password']").first

                    if email_field.count() > 0 and pass_field.count() > 0:
                        email_field.fill(MERKATO_USER)
                        pass_field.fill(MERKATO_PASS)

                        submit_btn = page.locator(
                            "button[type='submit'], button:has-text('Login'), button:has-text('Sign in'), "
                            "button:has-text('Log in'), input[type='submit']"
                        ).first
                        submit_btn.click()

                        # Give the SPA a moment to process login + redirect
                        page.wait_for_timeout(4000)
                        current_url = page.url
                        print(f"✅ Submitted login, current URL: {current_url}", flush=True)
                    else:
                        print(f"⚠️ Could not confidently identify email/username field among: {field_info}", flush=True)
                except Exception as e:
                    print(f"⚠️ Login attempt failed, continuing without auth: {e}", flush=True)

            # --- Load tenders listing ---
            page.goto(MERKATO_TENDERS_URL, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=20000)
            # Give the SPA a moment to hydrate/render the list
            page.wait_for_timeout(2000)

            # Grab every link that points to an individual tender page
            links = page.locator("a[href*='/tenders/']").all()
            print(f"Found {len(links)} raw tender links on page", flush=True)

            seen_this_scan = set()
            for link in links:
                try:
                    title_text = link.inner_text().strip()
                    href = link.get_attribute("href")
                    if not title_text or not href:
                        continue

                    full_link = href if href.startswith("http") else f"{MERKATO_BASE}{href}"

                    if full_link in seen_this_scan:
                        continue
                    seen_this_scan.add(full_link)

                    if is_relevant_tender(title_text):
                        tender_id = f"merkato_{full_link.rstrip('/').split('/')[-1]}"
                        if tender_id not in NOTIFIED_TENDERS:
                            found += 1
                            NOTIFIED_TENDERS.add(tender_id)
                            alert = f"🔔 *New Medical Tender Found!*\n\n📋 *Title:* {title_text}\n🔗 *Link:* {full_link}"
                            send_whatsapp(alert)
                            time.sleep(2)
                except Exception:
                    continue

            browser.close()

        print(f"2merkato engine complete. Discovered {found} active matches.", flush=True)
        return found

    except Exception as e:
        print(f"❌ 2merkato extraction engine down: {e}", flush=True)
        return 0


# ==================== RUN COORDINATOR ====================
def check_for_tenders():
    print("=================== STARTING SCAN CYCLE ===================", flush=True)
    total = scrape_2merkato()
    print(f"=================== SCAN COMPLETE: {total} NEW FOUND ===================", flush=True)

    if total == 0:
        send_whatsapp("🔍 *Tender Monitor Scan Completed.*\nNo new unique medical equipment or maintenance matches found on 2merkato.")


def monitoring_loop():
    while True:
        check_for_tenders()
        time.sleep(4 * 3600)


# ==================== FLASK ROUTES ====================
@app.route('/')
def home():
    return "Medical Tender Tracking Service Is Online!", 200


@app.route('/test-check')
def manual_test():
    threading.Thread(target=check_for_tenders).start()
    return "Scraper sync cycle triggered!", 200


if __name__ == "__main__":
    threading.Thread(target=monitoring_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
