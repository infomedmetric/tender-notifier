import os
import time
import requests
import threading
import traceback
import hmac
import psycopg2
import json
from datetime import datetime
from flask import Flask, request
from playwright.sync_api import sync_playwright
import urllib3
from google import genai
from google.genai import types

# ==================== GEMINI AI ENGINE (BATCHED) ====================

def _call_gemini_with_retry(client, prompt, config, max_retries=3):
    """
    Handles 429 RESOURCE_EXHAUSTED errors by waiting out the free-tier rate limit delay.
    """
    for attempt in range(1, max_retries + 1):
        try:
            return client.models.generate_content(
                model='gemini-flash-latest',
                contents=prompt,
                config=config,
            )
        except Exception as e:
            err_msg = str(e)
            if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                delay = 25 * attempt
                print(f"⚠️ Gemini Rate Limit / 429 Quota Exceeded. Retrying in {delay}s (Attempt {attempt}/{max_retries})...", flush=True)
                time.sleep(delay)
            else:
                if attempt < max_retries:
                    time.sleep(3 * attempt)
                else:
                    raise e
    raise Exception("Max Gemini retries exceeded.")


def batch_analyze_tenders(tenders_list: list) -> list:
    """
    Processes a list of tenders in a single Gemini API call to conserve free-tier quota.
    """
    if not tenders_list:
        return []

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("⚠️ GEMINI_API_KEY missing. Falling back to rule-based filtering.", flush=True)
        return _rule_based_fallback(tenders_list)

    try:
        client = genai.Client(api_key=api_key)
        
        system_instruction = (
            "You are an expert procurement analyst for medMETRIC Healthcare Service PLC, "
            "an Ethiopian biomedical engineering and technical-services company. Their scope "
            "covers: medical equipment maintenance & service contracts (including hemodialysis "
            "systems like B.Braun Dialog+ and SWS-4000A), RO/water treatment systems, CSSD & "
            "sterilization equipment (e.g. Rivamed), calibration & compliance testing of "
            "diagnostic/therapeutic equipment, medical imaging equipment, spare-parts sourcing, "
            "and general medical equipment supply/consultancy. They do NOT supply medicines, "
            "pharmaceuticals, vaccines, or laboratory-only equipment/reagents. "
            "Analyze the batch of tenders and return a JSON array of objects."
        )

        formatted_input = []
        for idx, item in enumerate(tenders_list):
            formatted_input.append(
                f"--- TENDER INDEX {idx} ---\n"
                f"ID: {item.get('id')}\n"
                f"Title: {item.get('title')}\n"
                f"Ref No: {item.get('ref_no', 'N/A')}\n"
                f"Details: {item.get('raw_text', '')[:2000]}\n"
            )

        prompt = f"""
        Analyze the following tenders for medMETRIC Healthcare.

        For each tender:
        1. Determine if it is RELEVANT to medMETRIC's technical scope (true/false).
           Set to false if it's primarily for medicines, pharmaceuticals, vaccines, or lab reagents.
        2. Assign a Match Score (0-100%) with 1-sentence reasoning.
        3. Extract Key Constraints (bank guarantees, local agent, manufacturer authorizations, etc.).
        4. Extract Deadline / Closing Date in East Africa Time (EAT).

        Return a JSON Array matching this schema exactly:
        [
            {{
                "index": 0,
                "is_relevant": true,
                "match_score": "85% - Relevant equipment service contract.",
                "constraints": "100,000 ETB Bid Bond required.",
                "closing_date": "Aug 15, 2026 at 10:00 AM EAT"
            }}
        ]

        Tenders to process:
        {"".join(formatted_input)}
        """

        response = _call_gemini_with_retry(
            client,
            prompt,
            types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.1,
                response_mime_type="application/json"
            )
        )

        results = json.loads(response.text)
        evaluated = []
        for res in results:
            idx = res.get("index")
            if idx is not None and idx < len(tenders_list):
                t = tenders_list[idx]
                t["is_relevant"] = res.get("is_relevant", True)
                t["match_score"] = res.get("match_score", "Match identified")
                t["constraints"] = res.get("constraints", "Check full listing")
                t["closing_date"] = res.get("closing_date", "Refer to portal")
                evaluated.append(t)
        return evaluated

    except Exception as e:
        print(f"❌ Batch AI analysis failed ({e}). Using rule-based fallback...", flush=True)
        return _rule_based_fallback(tenders_list)


def _rule_based_fallback(tenders_list):
    """Fallback if Gemini API fails or runs out of quota so alerts still trigger."""
    evaluated = []
    for t in tenders_list:
        t["is_relevant"] = True  # Already passed is_relevant_tender filter
        t["match_score"] = "Keyword Match (AI Unavailable)"
        t["constraints"] = "Review portal listing directly"
        t["closing_date"] = "Check portal"
        evaluated.append(t)
    return evaluated


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# ================== CONFIGURATION ==================
EVOLUTION_BASE = os.environ.get("EVOLUTION_BASE", "https://medmetric-evolution.onrender.com")
INSTANCE_NAME = os.environ.get("INSTANCE_NAME", "Tender-Notifier.")
GLOBAL_API_KEY = os.environ.get("GLOBAL_API_KEY")

if not GLOBAL_API_KEY:
    print("⚠️ GLOBAL_API_KEY is not set.", flush=True)

WHATSAPP_NUMBERS = [
    n.strip() for n in os.environ.get("WHATSAPP_NUMBERS", "").split(",") if n.strip()
]

TEST_CHECK_TOKEN = os.environ.get("TEST_CHECK_TOKEN")
MERKATO_USER = os.environ.get("MERKATO_USER")
MERKATO_PASS = os.environ.get("MERKATO_PASS")

MERKATO_BASE = "https://tender.2merkato.com"
MERKATO_LOGIN_URL = f"{MERKATO_BASE}/login"
MERKATO_TENDERS_URL = f"{MERKATO_BASE}/tenders"
MERKATO_MAX_PAGES = int(os.environ.get("MERKATO_MAX_PAGES", "4"))

EGP_BASE = "https://production.egp.gov.et"
EGP_LOGIN_URL = f"{EGP_BASE}/egp/login"
EGP_BIDS_URL = f"{EGP_BASE}/egp/bids/all"
EGP_USER = os.environ.get("EGP_USER")
EGP_PASS = os.environ.get("EGP_PASS")
EGP_ORG_NAME = os.environ.get("EGP_ORG_NAME", "Medmetric")

EGP_SEARCH_TERMS = [
    "medical equipment", "biomedical", "hemodialysis", "dialysis",
    "calibration", "sterilization", "water treatment", "hospital equipment",
    "x-ray", "ultrasound", "medical imaging", "spare parts"
]

STRONG_KEYWORDS = [
    "biomedical", "hemodialysis", "dialysis", "b.braun", "dialog+", "sws-4000a",
    "x-ray", "xray", "ultrasound", "ventilator", "autoclave", "sterilizer",
    "sterilization", "sterile processing", "cssd", "rivamed",
    "reverse osmosis", "ro system", "water treatment", "spare parts",
    "biomedical engineering", "medical imaging", "calibration",
    "diagnostic equipment", "medical equipment", "hospital equipment",
    "medical device", "የህክምና", "ጥገና"
]

MEDICAL_CONTEXT = ["medical", "health", "hospital", "biomedical", "clinical"]
EQUIPMENT_CONTEXT = ["equipment", "supplies", "supply", "device", "machine",
                     "instrument", "apparatus", "maintenance", "repair", "procurement",
                     "calibration", "installation", "servicing", "spare parts",
                     "consulting", "consultancy", "icb"]

HARD_EXCLUDE_TERMS = [
    "vehicle", "toyota", "car ", "motorbike", "insurance", "life insurance",
    "term life", "gpa", "laboratory", "lab reagent", "reagent", "lab equipment",
    "medicine", "medicines", "pharmaceutical", "pharmaceuticals",
    "drug", "drugs", "vaccine", "vaccines", "rdf medicine", "rdf medicines"
]

CONTEXTUAL_EXCLUDE_TERMS = ["consultancy services", "consulting firm"]


def is_relevant_tender(title):
    title_lower = title.lower()

    if any(term in title_lower for term in HARD_EXCLUDE_TERMS):
        return False

    has_medical_context = any(term in title_lower for term in MEDICAL_CONTEXT)
    has_equipment_context = any(term in title_lower for term in EQUIPMENT_CONTEXT)

    if any(term in title_lower for term in CONTEXTUAL_EXCLUDE_TERMS):
        if not (has_medical_context and has_equipment_context):
            return False

    if any(term in title_lower for term in STRONG_KEYWORDS):
        return True

    return has_medical_context and has_equipment_context


# ==================== PERSISTENT DEDUP (Postgres) ====================
DATABASE_URL = os.environ.get("DATABASE_URL")
_db_conn = None

def get_db_connection():
    global _db_conn
    if _db_conn is not None:
        try:
            cur = _db_conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
            return _db_conn
        except Exception:
            try:
                _db_conn.close()
            except Exception:
                pass
            _db_conn = None

    _db_conn = psycopg2.connect(DATABASE_URL, sslmode="require", connect_timeout=10)
    return _db_conn

def init_db():
    if not DATABASE_URL:
        return
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS notified_tenders (
                tender_id TEXT PRIMARY KEY,
                notified_at TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"❌ Postgres init failed: {e}", flush=True)

_MEMORY_FALLBACK = set()

def is_already_notified(tender_id: str) -> bool:
    if not DATABASE_URL:
        return tender_id in _MEMORY_FALLBACK
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM notified_tenders WHERE tender_id = %s", (tender_id,))
        result = cur.fetchone()
        cur.close()
        return result is not None
    except Exception:
        return tender_id in _MEMORY_FALLBACK

def mark_as_notified(tender_id: str):
    if not DATABASE_URL:
        _MEMORY_FALLBACK.add(tender_id)
        return
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO notified_tenders (tender_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (tender_id,)
        )
        conn.commit()
        cur.close()
    except Exception:
        _MEMORY_FALLBACK.add(tender_id)

def send_whatsapp(message):
    if not GLOBAL_API_KEY or not WHATSAPP_NUMBERS:
        return

    url = f"{EVOLUTION_BASE}/message/sendText/{INSTANCE_NAME}"
    headers = {"Content-Type": "application/json", "apikey": GLOBAL_API_KEY}
    for number in WHATSAPP_NUMBERS:
        payload = {"number": number, "text": message}
        try:
            requests.post(url, json=payload, headers=headers, timeout=10)
        except Exception as e:
            print(f"❌ Send error: {e}", flush=True)


# ==================== ENGINE: 2MERKATO ====================
def scrape_2merkato():
    print(f"[{datetime.now()}] 🔍 Running 2merkato Engine...", flush=True)
    pending_batch = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            context = browser.new_context()
            page = context.new_page()

            if MERKATO_USER and MERKATO_PASS:
                try:
                    page.goto(MERKATO_LOGIN_URL, timeout=20000, wait_until="domcontentloaded")
                    email_field = page.locator("input[type='email'], input[name*='user' i]").first
                    pass_field = page.locator("input[type='password']").first

                    if email_field.count() > 0 and pass_field.count() > 0:
                        email_field.fill(MERKATO_USER)
                        pass_field.fill(MERKATO_PASS)
                        page.locator("button[type='submit']").first.click()
                        page.wait_for_timeout(3000)
                except Exception as e:
                    print(f"⚠️ 2merkato optional login bypassed: {e}", flush=True)

            seen_this_scan = set()
            for page_num in range(1, MERKATO_MAX_PAGES + 1):
                page_url = MERKATO_TENDERS_URL if page_num == 1 else f"{MERKATO_TENDERS_URL}?page={page_num}"
                page.goto(page_url, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)

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

                        if is_relevant_tender(title_text):
                            tender_id = f"merkato_{full_link.rstrip('/').split('/')[-1]}"
                            if not is_already_notified(tender_id):
                                detail_text = ""
                                detail_page = None
                                try:
                                    detail_page = context.new_page()
                                    detail_page.goto(full_link, timeout=15000, wait_until="domcontentloaded")
                                    detail_text = detail_page.locator("body").inner_text(timeout=3000)[:3000]
                                except Exception:
                                    pass
                                finally:
                                    if detail_page: 
                                        detail_page.close()

                                pending_batch.append({
                                    "id": tender_id,
                                    "title": title_text,
                                    "ref_no": "N/A",
                                    "link": full_link,
                                    "raw_text": f"{title_text}\n{detail_text}"
                                })
                    except Exception:
                        continue

            browser.close()

        # Batch analyze all candidates at once
        if pending_batch:
            print(f"🤖 Batch analyzing {len(pending_batch)} tenders from 2merkato...", flush=True)
            analyzed_batch = batch_analyze_tenders(pending_batch)
            
            found_count = 0
            for item in analyzed_batch:
                mark_as_notified(item["id"])
                if item.get("is_relevant"):
                    found_count += 1
                    alert = (
                        f"🔔 *New Medical Tender Found! (2merkato)*\n\n"
                        f"📋 *Title:* {item['title']}\n"
                        f"🎯 *Match Score:* {item['match_score']}\n"
                        f"⚠️ *Constraints:* {item['constraints']}\n"
                        f"📅 *Closing Date:* {item['closing_date']}\n\n"
                        f"🔗 *Link:* {item['link']}"
                    )
                    send_whatsapp(alert)
                    time.sleep(1)
            return found_count

        return 0

    except Exception as e:
        print(f"❌ 2merkato engine error: {e}", flush=True)
        return 0


# ==================== ENGINE: eGP ====================
def scrape_egp():
    print(f"[{datetime.now()}] 🔍 Running eGP Engine...", flush=True)
    pending_batch = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            context = browser.new_context()
            page = context.new_page()

            # Optional Login Attempt - Bypassed gracefully on timeout
            if EGP_USER and EGP_PASS:
                try:
                    page.goto(EGP_LOGIN_URL, timeout=15000, wait_until="domcontentloaded")
                    user_field = page.locator("input[type='email'], input[type='text']").first
                    pass_field = page.locator("input[type='password']").first

                    if user_field.count() > 0 and pass_field.count() > 0:
                        user_field.fill(EGP_USER)
                        pass_field.fill(EGP_PASS)
                        page.locator("button[type='submit']").first.click()
                        page.wait_for_timeout(2000)
                except Exception as e:
                    print(f"⚠️ eGP optional login bypassed/timed out: {e}", flush=True)

            # Directly load the public bids view
            try:
                page.goto(EGP_BIDS_URL, timeout=25000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
            except Exception as e:
                print(f"❌ Could not reach eGP public bids portal: {e}", flush=True)
                browser.close()
                return 0

            search_box = page.locator("input[placeholder*='Search' i]").first
            seen_this_scan = set()

            if search_box.count() > 0:
                for term in EGP_SEARCH_TERMS:
                    try:
                        search_box.click()
                        search_box.fill("")
                        search_box.fill(term)
                        search_box.press("Enter")
                        page.wait_for_timeout(2000)

                        rows = page.locator("table tbody tr").all()
                        for row in rows:
                            cells = row.locator("td").all_inner_texts()
                            if not cells: 
                                continue
                            ref_no = cells[0].strip() if len(cells) > 0 else ""
                            title_text = cells[2].strip() if len(cells) > 2 else " ".join(cells)

                            row_key = ref_no or title_text
                            if row_key in seen_this_scan: 
                                continue
                            seen_this_scan.add(row_key)

                            if is_relevant_tender(title_text):
                                tender_id = f"egp_{ref_no or title_text}"
                                if not is_already_notified(tender_id):
                                    pending_batch.append({
                                        "id": tender_id,
                                        "title": title_text,
                                        "ref_no": ref_no,
                                        "link": EGP_BIDS_URL,
                                        "raw_text": f"Title: {title_text}, Ref: {ref_no}"
                                    })
                    except Exception:
                        continue

            browser.close()

        # Batch analyze eGP items
        if pending_batch:
            print(f"🤖 Batch analyzing {len(pending_batch)} tenders from eGP...", flush=True)
            analyzed_batch = batch_analyze_tenders(pending_batch)
            
            found_count = 0
            for item in analyzed_batch:
                mark_as_notified(item["id"])
                if item.get("is_relevant"):
                    found_count += 1
                    alert = (
                        f"🔔 *New Medical Tender Found! (eGP)*\n\n"
                        f"📋 *Title:* {item['title']}\n"
                        f"📄 *Ref No:* {item['ref_no']}\n"
                        f"🎯 *Match Score:* {item['match_score']}\n"
                        f"⚠️ *Constraints:* {item['constraints']}\n"
                        f"📅 *Closing Date:* {item['closing_date']}\n\n"
                        f"🔗 *Link:* {item['link']}"
                    )
                    send_whatsapp(alert)
                    time.sleep(1)
            return found_count

        return 0

    except Exception as e:
        print(f"❌ eGP engine error: {e}", flush=True)
        return 0


# ==================== RUN COORDINATOR ====================
def check_for_tenders():
    print("=================== STARTING SCAN CYCLE ===================", flush=True)
    merkato_total = scrape_2merkato()
    egp_total = scrape_egp()

    total = merkato_total + egp_total
    print(f"=================== SCAN COMPLETE: {total} NEW FOUND ===================", flush=True)

    if total == 0:
        send_whatsapp("🔍 *Tender Monitor Scan Completed.*\nNo new unique medical equipment or maintenance matches found.")


def monitoring_loop():
    try:
        interval_hours = float(os.environ.get("SCAN_INTERVAL_HOURS", "6"))
    except ValueError:
        interval_hours = 6
    while True:
        try:
            check_for_tenders()
        except Exception:
            print(traceback.format_exc(), flush=True)
        time.sleep(interval_hours * 3600)


@app.route('/')
def home():
    return "Medical Tender Tracking Service Is Online!", 200


@app.route('/test-check')
def manual_test():
    if not TEST_CHECK_TOKEN:
        return "Manual trigger disabled", 503

    provided_token = request.args.get("token", "")
    if not hmac.compare_digest(provided_token, TEST_CHECK_TOKEN):
        return "Unauthorized", 401

    threading.Thread(target=check_for_tenders).start()
    return "Scraper sync cycle triggered!", 200


init_db()

if __name__ == "__main__":
    threading.Thread(target=monitoring_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
