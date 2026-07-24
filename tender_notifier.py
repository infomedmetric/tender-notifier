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
from groq import Groq, RateLimitError, APIError
from dotenv import load_dotenv

load_dotenv()

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# ==================== CONFIGURATION ====================
EVOLUTION_BASE = os.environ.get("EVOLUTION_BASE", "https://medmetric-evolution.onrender.com")
INSTANCE_NAME = os.environ.get("INSTANCE_NAME", "Tender-Notifier.")
GLOBAL_API_KEY = os.environ.get("GLOBAL_API_KEY")

WHATSAPP_NUMBERS = [n.strip() for n in os.environ.get("WHATSAPP_NUMBERS", "").split(",") if n.strip()]
TEST_CHECK_TOKEN = os.environ.get("TEST_CHECK_TOKEN")

MERKATO_USER = os.environ.get("MERKATO_USER")
MERKATO_PASS = os.environ.get("MERKATO_PASS")
MERKATO_BASE = "https://tender.2merkato.com"
MERKATO_LOGIN_URL = f"{MERKATO_BASE}/login"
MERKATO_TENDERS_URL = f"{MERKATO_BASE}/tenders"
MERKATO_MAX_PAGES = int(os.environ.get("MERKATO_MAX_PAGES", "4"))

EGP_BASE = "https://production.egp.gov.et"
EGP_BIDS_URL = f"{EGP_BASE}/egp/bids/all"
EGP_SEARCH_TERMS = ["medical", "biomedical", "hemodialysis", "health", "ጥገና"]


# ==================== KEYWORD FILTERING ====================
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

def is_relevant_tender(title: str) -> bool:
    title_lower = title.lower()
    if any(term in title_lower for term in HARD_EXCLUDE_TERMS):
        return False

    has_medical = any(term in title_lower for term in MEDICAL_CONTEXT)
    has_equip = any(term in title_lower for term in EQUIPMENT_CONTEXT)

    if any(term in title_lower for term in CONTEXTUAL_EXCLUDE_TERMS):
        if not (has_medical and has_equip):
            return False

    if any(term in title_lower for term in STRONG_KEYWORDS):
        return True

    return has_medical and has_equip


# ==================== DATABASE (POSTGRES) ====================
DATABASE_URL = os.environ.get("DATABASE_URL")
_db_conn = None
_MEMORY_FALLBACK = set()

def get_db_connection():
    global _db_conn
    if _db_conn is not None:
        try:
            _db_conn.cursor().execute("SELECT 1")
            return _db_conn
        except Exception:
            _db_conn = None
    if DATABASE_URL:
        _db_conn = psycopg2.connect(DATABASE_URL, sslmode="require", connect_timeout=10)
    return _db_conn

def init_db():
    if DATABASE_URL:
        try:
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS notified_tenders (
                        tender_id TEXT PRIMARY KEY,
                        notified_at TIMESTAMP DEFAULT NOW()
                    )
                """)
            conn.commit()
            print("✅ Connected to Postgres database.", flush=True)
        except Exception as e:
            print(f"❌ Postgres init failed: {e}", flush=True)

def is_already_notified(tender_id: str) -> bool:
    if not DATABASE_URL:
        return tender_id in _MEMORY_FALLBACK
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM notified_tenders WHERE tender_id = %s", (tender_id,))
            return cur.fetchone() is not None
    except Exception:
        return tender_id in _MEMORY_FALLBACK

def mark_as_notified(tender_id: str):
    if not DATABASE_URL:
        _MEMORY_FALLBACK.add(tender_id)
        return
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO notified_tenders (tender_id) VALUES (%s) ON CONFLICT DO NOTHING", (tender_id,))
        conn.commit()
    except Exception:
        _MEMORY_FALLBACK.add(tender_id)


# ==================== WHATSAPP NOTIFICATION ====================
def send_whatsapp(message: str):
    if not GLOBAL_API_KEY or not WHATSAPP_NUMBERS:
        return
    url = f"{EVOLUTION_BASE}/message/sendText/{INSTANCE_NAME}"
    headers = {"Content-Type": "application/json", "apikey": GLOBAL_API_KEY}
    for number in WHATSAPP_NUMBERS:
        try:
            requests.post(url, json={"number": number, "text": message}, headers=headers, timeout=10)
        except Exception as e:
            print(f"❌ WhatsApp send error: {e}", flush=True)


# ==================== GROQ AI ENGINE ====================
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

def batch_analyze_tenders_groq(tenders_list: list) -> list:
    if not tenders_list:
        return []

    groq_api_key = os.environ.get("GROQ_API_KEY")
    if not groq_api_key:
        print("⚠️ GROQ_API_KEY missing. Falling back to rule-based filtering.", flush=True)
        return _rule_based_fallback(tenders_list)

    try:
        client = Groq(api_key=groq_api_key)
        system_instruction = (
            "You are an expert procurement analyst for medMETRIC Healthcare Service PLC, "
            "an Ethiopian biomedical engineering company. Your scope: medical equipment maintenance "
            "(hemodialysis, B.Braun Dialog+, SWS-4000A), RO/water treatment systems, CSSD/sterilization "
            "(Rivamed), calibration, medical imaging, and spare-parts. "
            "Exclude: medicines, pharmaceuticals, vaccines, or lab-only reagents. "
            "Analyze the tenders and return a JSON array."
        )

        formatted_input = []
        for idx, item in enumerate(tenders_list):
            details = item.get('raw_text', '')[:1000].replace('\n', ' ')
            formatted_input.append(f"--- INDEX {idx} ---\nID: {item.get('id')}\nTitle: {item.get('title')}\nDetails: {details}\n")

        prompt = f"""
        Analyze these tenders. Return strictly a JSON array with this structure:
        [{{ "index": 0, "is_relevant": true, "match_score": "85% - Reason", "constraints": "Bond req", "closing_date": "Aug 15" }}]
        
        Tenders:
        {"".join(formatted_input)}
        """

        for attempt in range(1, 4):
            try:
                chat = client.chat.completions.create(
                    messages=[{"role": "system", "content": system_instruction}, {"role": "user", "content": prompt}],
                    model=GROQ_MODEL,
                    response_format={"type": "json_object"} if "llama-3" in GROQ_MODEL else None,
                    temperature=0.1, max_tokens=1500,
                )
                response_text = chat.choices[0].message.content
                
                data = json.loads(response_text)
                results = data.get("tenders", data) if isinstance(data, dict) else data
                results = results if isinstance(results, list) else [data]

                evaluated = []
                for res in results:
                    idx = res.get("index")
                    if idx is not None and idx < len(tenders_list):
                        tender = tenders_list[idx]
                        tender["is_relevant"] = res.get("is_relevant", True)
                        tender["match_score"] = res.get("match_score", "Match identified")
                        tender["constraints"] = res.get("constraints", "None identified")
                        tender["closing_date"] = res.get("closing_date", "Refer to portal")
                        evaluated.append(tender)
                return evaluated if evaluated else _rule_based_fallback(tenders_list)

            except RateLimitError:
                time.sleep(5 * (2 ** attempt))
            except APIError:
                break

        return _rule_based_fallback(tenders_list)
    except Exception:
        return _rule_based_fallback(tenders_list)

def _rule_based_fallback(tenders_list):
    for t in tenders_list:
        t.update({"is_relevant": True, "match_score": "Keyword Match (AI Fallback)", "constraints": "Check site", "closing_date": "Check site"})
    return tenders_list


# ==================== SCRAPING ENGINES WITH DEBUG LOGS ====================
def process_and_notify(batch, source_name):
    if not batch:
        print(f"ℹ️ No relevant pending tenders found for {source_name}.", flush=True)
        return 0
    print(f"🤖 Batch analyzing {len(batch)} tenders from {source_name} with Groq...", flush=True)
    analyzed = batch_analyze_tenders_groq(batch)
    found = 0
    for item in analyzed:
        mark_as_notified(item["id"])
        if item.get("is_relevant"):
            found += 1
            alert = (
                f"🔔 *New Medical Tender Found! ({source_name})*\n\n"
                f"📋 *Title:* {item['title']}\n"
                f"🎯 *Match:* {item['match_score']}\n"
                f"⚠️ *Constraints:* {item['constraints']}\n"
                f"📅 *Deadline:* {item['closing_date']}\n\n"
                f"🔗 *Link:* {item['link']}"
            )
            send_whatsapp(alert)
            time.sleep(2)
    return found


def scrape_2merkato(context):
    print(f"[{datetime.now()}] 🔍 Running 2merkato Engine...", flush=True)
    pending = []
    page = context.new_page()
    page.route("**/*.{png,jpg,jpeg,svg,css,woff,woff2,gif}", lambda route: route.abort())

    try:
        print(f"🔑 2merkato credentials check: User set = {bool(MERKATO_USER)}, Pass set = {bool(MERKATO_PASS)}", flush=True)
        if MERKATO_USER and MERKATO_PASS:
            try:
                page.goto(MERKATO_LOGIN_URL, timeout=30000, wait_until="domcontentloaded")
                print("🌐 Navigated to 2merkato login page.", flush=True)
                
                email_input = page.locator("input[type='email'], input[name*='email' i], input[name*='username' i], input[name*='user' i]").first
                print(f"🔍 2merkato email/user input found = {email_input.count() > 0}", flush=True)
                if email_input.count() > 0:
                    email_input.fill(MERKATO_USER)
                
                pass_input = page.locator("input[type='password'], input[name*='pass' i]").first
                print(f"🔍 2merkato password input found = {pass_input.count() > 0}", flush=True)
                if pass_input.count() > 0:
                    pass_input.fill(MERKATO_PASS)
                
                submit_btn = page.locator("button[type='submit'], input[type='submit'], button:has-text('Login'), button:has-text('Sign')").first
                print(f"🔍 2merkato submit button found = {submit_btn.count() > 0}", flush=True)
                if submit_btn.count() > 0:
                    submit_btn.click()
                    page.wait_for_timeout(5000)
                    print("✅ 2merkato login submitted.", flush=True)
            except Exception as e:
                print(f"⚠️ 2merkato login error: {e}", flush=True)

        seen = set()
        for page_num in range(1, MERKATO_MAX_PAGES + 1):
            url = MERKATO_TENDERS_URL if page_num == 1 else f"{MERKATO_TENDERS_URL}?page={page_num}"
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            
            links = page.locator("a.tender-title, h3 a, h4 a, .card-title a, a[href*='/tenders/']").all()
            print(f"🔍 2merkato Page {page_num}: Found {len(links)} tender links.", flush=True)
            
            for link in links:
                try:
                    title = link.inner_text().strip()
                    href = link.get_attribute("href")
                    if not title or not href: continue

                    full_link = href if href.startswith("http") else f"{MERKATO_BASE}{href}"
                    if full_link in seen: continue
                    seen.add(full_link)

                    if is_relevant_tender(title):
                        print(f"🎯 Match found (2merkato): {title[:60]}...", flush=True)
                        tender_id = f"merkato_{full_link.rstrip('/').split('/')[-1]}"
                        if not is_already_notified(tender_id):
                            pending.append({
                                "id": tender_id, "title": title, "ref_no": "N/A",
                                "link": full_link, "raw_text": title
                            })
                except Exception:
                    continue
    except Exception as e:
        print(f"❌ 2merkato error: {e}", flush=True)
    finally:
        page.close()
    
    return process_and_notify(pending, "2merkato")


def scrape_egp(context):
    print(f"[{datetime.now()}] 🔍 Running eGP Engine...", flush=True)
    pending = []
    page = context.new_page()
    page.route("**/*.{png,jpg,jpeg,svg,css,woff,woff2,gif}", lambda route: route.abort())

    seen = set()
    try:
        page.goto(EGP_BIDS_URL, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        for term in EGP_SEARCH_TERMS:
            try:
                search_box = page.locator("input#search, input[name*='search' i], input[placeholder*='Search' i], input[type='search']").first
                if search_box.count() == 0:
                    search_box = page.locator("input.form-control").first

                print(f"🔍 eGP Search term '{term}': Input box found = {search_box.count() > 0}", flush=True)

                if search_box.count() > 0:
                    search_box.click()
                    search_box.fill("")
                    search_box.fill(term)
                    search_box.press("Enter")
                    page.wait_for_timeout(4000)

                rows = page.locator("table tbody tr, .table tr, tr[role='row']").all()
                print(f"🔍 eGP Search term '{term}': Extracted {len(rows)} table rows.", flush=True)

                for row in rows:
                    cells = row.locator("td").all_inner_texts()
                    if not cells or len(cells) < 2: continue
                    
                    row_text = " | ".join([c.strip() for c in cells if c.strip()])
                    ref_no = cells[0].strip() if len(cells) > 0 else "N/A"
                    
                    potential_titles = [c.strip() for c in cells if len(c.strip()) > 10]
                    title = max(potential_titles, key=len) if potential_titles else row_text

                    if is_relevant_tender(title) or is_relevant_tender(row_text):
                        print(f"🎯 Match found (eGP): {title[:60]}...", flush=True)
                        tender_id = f"egp_{ref_no if ref_no != 'N/A' else abs(hash(title))}"
                        if tender_id not in seen:
                            seen.add(tender_id)
                            if not is_already_notified(tender_id):
                                pending.append({
                                    "id": tender_id, "title": title, "ref_no": ref_no,
                                    "link": EGP_BIDS_URL, "raw_text": row_text
                                })
            except Exception as e:
                print(f"⚠️ eGP search error for term '{term}': {e}", flush=True)
                continue
    except Exception as e:
        print(f"❌ eGP fatal error: {e}", flush=True)
    finally:
        page.close()

    return process_and_notify(pending, "eGP")


# ==================== RUN COORDINATOR ====================
def check_for_tenders():
    print(f"\n[{datetime.now()}] =================== STARTING SCAN CYCLE ===================", flush=True)
    m_total, e_total = 0, 0

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--single-process", "--no-zygote"]
            )
            context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")

            try:
                m_total = scrape_2merkato(context)
            except Exception as e:
                print(f"❌ 2merkato loop crash: {e}", flush=True)

            try:
                e_total = scrape_egp(context)
            except Exception as e:
                print(f"❌ eGP loop crash: {e}", flush=True)

            browser.close()
    except Exception as e:
        print(f"❌ Playwright fatal error: {e}", flush=True)

    total = m_total + e_total
    print(f"[{datetime.now()}] =================== SCAN COMPLETE: {total} NEW FOUND ===================\n", flush=True)


def monitoring_loop():
    try: 
        interval_hours = float(os.environ.get("SCAN_INTERVAL_HOURS", "6"))
    except ValueError: 
        interval_hours = 6
        
    while True:
        try: 
            check_for_tenders()
        except Exception: 
            print("❌ Fatal loop error:", traceback.format_exc(), flush=True)
        time.sleep(interval_hours * 3600)


# ==================== FLASK SERVER ====================
@app.route('/')
def home():
    return "Medical Tender Tracking Service Is Online!", 200


@app.route('/test-check')
def manual_test():
    if not TEST_CHECK_TOKEN: 
        return "Manual trigger disabled: TEST_CHECK_TOKEN is not configured.", 503
    provided_token = request.args.get("token", "")
    if not hmac.compare_digest(provided_token, TEST_CHECK_TOKEN): 
        return "Unauthorized", 401
    
    threading.Thread(target=check_for_tenders).start()
    return "Scraper sync cycle triggered in background!", 200


if __name__ == "__main__":
    init_db()
    threading.Thread(target=monitoring_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

