import os
import time
import json
import requests
import threading
import traceback
import psycopg2
from datetime import datetime
from flask import Flask
from playwright.sync_api import sync_playwright
import urllib3
from google import genai
from google.genai import types
from dotenv import load_dotenv

# Load local environment variables (if running locally)
load_dotenv()

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# ================== CONFIGURATION ==================
EVOLUTION_BASE = os.environ.get("EVOLUTION_BASE")
INSTANCE_NAME = os.environ.get("INSTANCE_NAME")
GLOBAL_API_KEY = os.environ.get("GLOBAL_API_KEY")
WHATSAPP_NUMBERS = [
    n.strip() for n in os.environ.get("WHATSAPP_NUMBERS", "").split(",") if n.strip()
]

MERKATO_USER = os.environ.get("MERKATO_USER")
MERKATO_PASS = os.environ.get("MERKATO_PASS")
MERKATO_BASE = "https://tender.2merkato.com"
MERKATO_LOGIN_URL = f"{MERKATO_BASE}/login"
MERKATO_TENDERS_URL = f"{MERKATO_BASE}/tenders"
MERKATO_MAX_PAGES = 4  # Strictly limited to 4 pages

EGP_BASE = "https://production.egp.gov.et"
EGP_LOGIN_URL = f"{EGP_BASE}/egp/login"
EGP_BIDS_URL = f"{EGP_BASE}/egp/bids/all"
EGP_USER = os.environ.get("EGP_USER")
EGP_PASS = os.environ.get("EGP_PASS")
EGP_ORG_NAME = os.environ.get("EGP_ORG_NAME", "Medmetric")
EGP_MAX_PAGES = 4  # Strictly limited to 4 pages

# Focused search terms for eGP engine targeting maintenance, consulting, and ICBs
EGP_SEARCH_TERMS = [
    "medical", "biomedical", "hemodialysis", "dialysis",
    "maintenance", "consulting", "ICB"
]

# Keywords for coarse initial string-matching filter
STRONG_KEYWORDS = [
    "biomedical", "hemodialysis", "dialysis", "b.braun", "dialog+",
    "x-ray", "xray", "ultrasound", "ventilator", "autoclave", "sterilizer",
    "diagnostic equipment", "medical equipment", "hospital equipment",
    "laboratory equipment", "medical device", "የህክምና", "ጥገና", "consulting", "icb"
]

MEDICAL_CONTEXT = ["medical", "health", "hospital", "biomedical", "clinical", "laboratory"]
EQUIPMENT_CONTEXT = ["equipment", "supplies", "supply", "device", "machine",
                     "instrument", "apparatus", "maintenance", "repair", "procurement", "consulting", "icb"]

EXCLUDE_TERMS = [
    "vehicle", "toyota", "car ", "motorbike", "insurance", "life insurance",
    "term life", "gpa"
]


def is_heuristically_relevant(title: str) -> bool:
    """Coarse text filter to avoid sending obviously irrelevant titles to Gemini."""
    title_lower = title.lower()

    if any(term in title_lower for term in EXCLUDE_TERMS):
        return False

    if any(term in title_lower for term in STRONG_KEYWORDS):
        return True

    has_medical_context = any(term in title_lower for term in MEDICAL_CONTEXT)
    has_equipment_context = any(term in title_lower for term in EQUIPMENT_CONTEXT)

    return has_medical_context and has_equipment_context


# ==================== AI PRE-SCREENING (Gemini) ====================
def analyze_and_verify_tender_with_ai(title: str, context_details: str = "") -> dict:
    """
    Passes potential match to Gemini API to STRICTLY verify relevance 
    before committing to sending a notification.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    fallback_response = {
        "is_relevant": False,
        "match_score": "0%",
        "reason": "AI API Key missing or call failed.",
        "constraints": "N/A",
        "closing_date": "N/A"
    }

    if not api_key:
        print("Error: GEMINI_API_KEY environment variable is missing.")
        return fallback_response

    try:
        client = genai.Client(api_key=api_key)
        
        system_instruction = (
            "You are an elite procurement AI agent filtering tenders for an Ethiopian medical enterprise. "
            "You only greenlight (is_relevant=True) tenders matching: "
            "1. Medical/biomedical/hospital equipment supply or procurement. "
            "2. Medical/biomedical equipment maintenance, calibration, or servicing. "
            "3. Healthcare or medical equipment consulting services. "
            "4. International Competitive Bidding (ICB) for medical devices.\n"
            "If a tender is for generic consulting (like tax or management), vehicles, structural construction, "
            "or non-medical software/items, reject it (is_relevant=False)."
        )
        
        prompt = f"""
        Analyze this tender:
        Title: {title}
        Extra Details: {context_details}

        Provide your verification response exactly matching this JSON schema:
        {{
            "is_relevant": boolean,
            "match_score": string (e.g., "85%"),
            "reason": string (1-sentence explanation of why it fits or does not fit),
            "constraints": string (bank guarantees, manufacturers authorization requirements, or "None"),
            "closing_date": string (extracted closing date in East Africa Time, or "Unknown")
        }}
        """
        # Update the model string to 'gemini-flash-latest' to automatically stay current
        response = client.models.generate_content(
            model='gemini-flash-latest',
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.1,
                response_mime_type="application/json"
            )
        )

        
        if response.text:
            return json.loads(response.text)
        return fallback_response

    except Exception as e:
        print(f"❌ Gemini AI verification failed: {e}")
        return fallback_response


# ==================== PERSISTENT DEDUP (Postgres) ====================
DATABASE_URL = os.environ.get("DATABASE_URL")

def get_db_connection():
    if not DATABASE_URL:
        return None
    return psycopg2.connect(DATABASE_URL, sslmode="require", connect_timeout=10)


def init_db():
    if not DATABASE_URL:
        print("⚠️ DATABASE_URL not set — falling back to in-memory dedup.", flush=True)
        return
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS notified_tenders (
                        tender_id TEXT PRIMARY KEY,
                        notified_at TIMESTAMP DEFAULT NOW()
                    )
                """)
                conn.commit()
        print("✅ Postgres notified_tenders table verified.", flush=True)
    except Exception as e:
        print(f"❌ Postgres init failed, falling back to in-memory: {e}", flush=True)


_MEMORY_FALLBACK = set()

def is_already_processed(tender_id: str) -> bool:
    if not DATABASE_URL:
        return tender_id in _MEMORY_FALLBACK
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM notified_tenders WHERE tender_id = %s", (tender_id,))
                return cur.fetchone() is not None
    except Exception as e:
        print(f"⚠️ DB check failed, using memory fallback: {e}", flush=True)
        return tender_id in _MEMORY_FALLBACK


def mark_as_processed(tender_id: str):
    if not DATABASE_URL:
        _MEMORY_FALLBACK.add(tender_id)
        return
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO notified_tenders (tender_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    (tender_id,)
                )
                conn.commit()
    except Exception as e:
        print(f"⚠️ DB insert failed, using memory fallback: {e}", flush=True)
        _MEMORY_FALLBACK.add(tender_id)


def send_whatsapp(message):
    if not EVOLUTION_BASE or not INSTANCE_NAME or not GLOBAL_API_KEY:
        print("⚠️ WhatsApp credentials missing. Cannot send message.")
        return
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

            # Optional Authenticated Login
            if MERKATO_USER and MERKATO_PASS:
                try:
                    page.goto(MERKATO_LOGIN_URL, timeout=30000, wait_until="domcontentloaded")
                    page.wait_for_selector("input[type='password']", timeout=20000)

                    email_field = page.locator(
                        "input[type='email'], input[name*='email' i], input[name*='user' i], "
                        "input[id*='email' i], input[id*='user' i], input[placeholder*='email' i]"
                    ).first
                    pass_field = page.locator("input[type='password']").first

                    if email_field.count() > 0 and pass_field.count() > 0:
                        email_field.fill(MERKATO_USER)
                        pass_field.fill(MERKATO_PASS)
                        submit_btn = page.locator(
                            "button[type='submit'], button:has-text('Login'), button:has-text('Sign in'), input[type='submit']"
                        ).first
                        submit_btn.click()
                        page.wait_for_timeout(4000)
                except Exception as e:
                    print(f"⚠️ Login attempt failed, continuing without auth: {e}", flush=True)

            seen_this_scan = set()
            for page_num in range(1, MERKATO_MAX_PAGES + 1):
                page_url = MERKATO_TENDERS_URL if page_num == 1 else f"{MERKATO_TENDERS_URL}?page={page_num}"
                print(f"➡️ Loading 2merkato page {page_num}: {page_url}", flush=True)
                page.goto(page_url, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)

                links = page.locator("a[href*='/tenders/']").all()

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

                        tender_id = f"merkato_{full_link.rstrip('/').split('/')[-1]}"
                        
                        # Step 1: Check Database to avoid repeat evaluations
                        if is_already_processed(tender_id):
                            continue

                        # Step 2: Heuristic Fast Filter
                        if is_heuristically_relevant(title_text):
                            print(f"🤖 [2merkato] Evaluating with AI: {title_text}", flush=True)
                            
                            # Step 3: Deep Verification with AI
                            ai_eval = analyze_and_verify_tender_with_ai(title_text)
                            
                            # Log processed ID immediately so we don't spam AI token limits
                            mark_as_processed(tender_id)

                            if ai_eval.get("is_relevant"):
                                found += 1
                                alert = (
                                    f"🔔 *New Medical Tender Found!*\n\n"
                                    f"📋 *Title:* {title_text}\n"
                                    f"🎯 *Match Score:* {ai_eval.get('match_score')}\n"
                                    f"💡 *Reason:* {ai_eval.get('reason')}\n"
                                    f"⚠️ *Constraints:* {ai_eval.get('constraints')}\n"
                                    f"📅 *Deadline:* {ai_eval.get('closing_date')}\n\n"
                                    f"🔗 *Link:* {full_link}"
                                )
                                send_whatsapp(alert)
                                time.sleep(2)
                    except Exception:
                        continue

            browser.close()
        print(f"2merkato engine complete. Discovered {found} verified matches.", flush=True)
        return found
    except Exception as e:
        print(f"❌ 2merkato extraction engine down: {e}", flush=True)
        return 0


# ==================== ENGINE: eGP (egp.gov.et) ====================
def scrape_egp():
    print(f"[{datetime.now()}] 🔍 Running eGP Engine (Playwright)...", flush=True)
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

            if EGP_USER and EGP_PASS:
                try:
                    page.goto(EGP_LOGIN_URL, timeout=30000, wait_until="domcontentloaded")
                    page.wait_for_selector("input[type='password']", timeout=20000)

                    user_field = page.locator(
                        "input[type='email'], input[type='text'], input[name*='user' i], input[id*='user' i]"
                    ).first
                    pass_field = page.locator("input[type='password']").first

                    if user_field.count() > 0 and pass_field.count() > 0:
                        user_field.fill(EGP_USER)
                        pass_field.fill(EGP_PASS)
                        submit_btn = page.locator("button[type='submit'], button:has-text('Login')").first
                        submit_btn.click()
                        page.wait_for_timeout(4000)

                        current_url = page.url
                        if "organization-selector" in current_url:
                            page.wait_for_selector(f"text=/{EGP_ORG_NAME}/i", timeout=15000)
                            org_option = page.locator(f"text=/{EGP_ORG_NAME}/i").first
                            if org_option.count() > 0:
                                org_option.click()
                                page.wait_for_timeout(3000)
                except Exception as e:
                    print(f"⚠️ eGP login attempt failed, continuing without auth: {e}", flush=True)

            # Dismiss modal dialogs
            for _ in range(3):
                try:
                    modal_close = page.locator(".ant-modal-close, .nz-modal-close, button:has-text('Close')").first
                    if modal_close.count() > 0:
                        modal_close.click(timeout=2000)
                except Exception:
                    pass
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)

            # Navigate to Bidding List
            try:
                tenders_link = page.locator("text=Tenders").first
                tenders_link.click(timeout=10000, force=True)
                page.wait_for_selector("text=/Bidding List/i", timeout=15000)
                print("✅ Reached Bidding List view", flush=True)
            except Exception as e:
                print(f"❌ Could not reach Bidding List view: {e}", flush=True)
                browser.close()
                return 0

            search_box = page.locator("input[placeholder*='Search' i]").first
            seen_this_scan = set()

            for term in EGP_SEARCH_TERMS:
                try:
                    search_box.click()
                    search_box.fill("")
                    page.wait_for_timeout(300)
                    search_box.fill(term)
                    search_box.press("Enter")
                    page.wait_for_timeout(2500)

                    # Iterate up to 4 pages on eGP if pagination links exist
                    for page_idx in range(1, EGP_MAX_PAGES + 1):
                        rows = page.locator("table tbody tr").all()

                        for row in rows:
                            try:
                                cells = row.locator("td").all_inner_texts()
                                if not cells:
                                    continue
                                ref_no = cells[0].strip() if len(cells) > 0 else ""
                                title_text = cells[2].strip() if len(cells) > 2 else " | ".join(cells)

                                row_key = ref_no or title_text
                                if row_key in seen_this_scan:
                                    continue
                                seen_this_scan.add(row_key)

                                tender_id = f"egp_{ref_no or title_text}"

                                if is_already_processed(tender_id):
                                    continue

                                if is_heuristically_relevant(title_text):
                                    print(f"🤖 [eGP] Evaluating with AI: {title_text}", flush=True)
                                    
                                    # Fetch detail page URL dynamically
                                    detail_link = EGP_BIDS_URL
                                    try:
                                        row_anchor = row.locator("a").first
                                        if row_anchor.count() > 0:
                                            href = row_anchor.get_attribute("href")
                                            if href:
                                                detail_link = href if href.startswith("http") else f"{EGP_BASE}{href}"
                                    except Exception:
                                        pass

                                    # Perform AI validation pre-delivery
                                    ai_eval = analyze_and_verify_tender_with_ai(title_text, f"Ref No: {ref_no}")
                                    mark_as_processed(tender_id)

                                    if ai_eval.get("is_relevant"):
                                        found += 1
                                        alert = (
                                            f"🔔 *New Medical Tender Found (eGP)!*\n\n"
                                            f"📋 *Title:* {title_text}\n"
                                            f"📄 *Ref No:* {ref_no}\n"
                                            f"🎯 *Match Score:* {ai_eval.get('match_score')}\n"
                                            f"💡 *Reason:* {ai_eval.get('reason')}\n"
                                            f"⚠️ *Constraints:* {ai_eval.get('constraints')}\n"
                                            f"📅 *Deadline:* {ai_eval.get('closing_date')}\n\n"
                                            f"🔗 *Link:* {detail_link}"
                                        )
                                        send_whatsapp(alert)
                                        time.sleep(2)
                            except Exception:
                                continue

                        # Try to go to next page of eGP table search results up to 4 pages
                        try:
                            next_btn = page.locator("li.ant-pagination-next:not(.ant-pagination-disabled), li.nz-pagination-next:not(.nz-pagination-disabled)").first
                            if next_btn.count() > 0 and page_idx < EGP_MAX_PAGES:
                                next_btn.click()
                                page.wait_for_timeout(2500)
                            else:
                                break
                        except Exception:
                            break

                except Exception as e:
                    print(f"⚠️ eGP search for '{term}' failed: {e}", flush=True)
                    continue

            browser.close()
        print(f"eGP engine complete. Discovered {found} verified matches.", flush=True)
        return found
    except Exception as e:
        print(f"❌ eGP extraction engine down: {e}", flush=True)
        return 0


# ==================== RUN COORDINATOR ====================
def check_for_tenders():
    print("=================== STARTING SCAN CYCLE ===================", flush=True)
    try:
        merkato_total = scrape_2merkato()
    except Exception:
        print("❌ 2merkato engine crashed:")
        print(traceback.format_exc(), flush=True)
        merkato_total = 0

    try:
        egp_total = scrape_egp()
    except Exception:
        print("❌ eGP engine crashed:")
        print(traceback.format_exc(), flush=True)
        egp_total = 0

    total = merkato_total + egp_total
    print(f"=================== SCAN COMPLETE: {total} NEW FOUND ===================", flush=True)


def monitoring_loop():
    while True:
        try:
            check_for_tenders()
        except Exception:
            print("❌ check_for_tenders crashed:")
            print(traceback.format_exc(), flush=True)
        time.sleep(4 * 3600)  # Run every 4 hours


# ==================== FLASK ROUTES ====================
@app.route('/')
def home():
    return "Medical Tender AI-Vetted Verification Service Online!", 200


@app.route('/test-check')
def manual_test():
    threading.Thread(target=check_for_tenders).start()
    return "AI-vetted Scraper sync cycle triggered manually!", 200


# Run Database init on module load
init_db()

if __name__ == "__main__":
    # Standard Flask production loop setup without Gunicorn requirement
    threading.Thread(target=monitoring_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, use_reloader=False)
