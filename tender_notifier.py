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
    n.strip() for n in os.environ.get("WHATSAPP_NUMBERS", "251901748874").split(",") if n.strip()
]

MERKATO_USER = os.environ.get("MERKATO_USER")
MERKATO_PASS = os.environ.get("MERKATO_PASS")

# CORRECT domain — the working tenders app, not www.2merkato.com
MERKATO_BASE = "https://tender.2merkato.com"
MERKATO_LOGIN_URL = f"{MERKATO_BASE}/login"
MERKATO_TENDERS_URL = f"{MERKATO_BASE}/tenders"
MERKATO_MAX_PAGES = int(os.environ.get("MERKATO_MAX_PAGES", "3"))

EGP_BASE = "https://production.egp.gov.et"
EGP_LOGIN_URL = f"{EGP_BASE}/egp/login"
EGP_BIDS_URL = f"{EGP_BASE}/egp/bids/all"
EGP_USER = os.environ.get("EGP_USER")
EGP_PASS = os.environ.get("EGP_PASS")
EGP_ORG_NAME = os.environ.get("EGP_ORG_NAME", "Medmetric")

# Search terms fed one at a time into eGP's own built-in table search box —
# far more reliable than scraping every row across 13+ pages
EGP_SEARCH_TERMS = [
    "medical", "biomedical", "hemodialysis", "dialysis",
    "laboratory equipment", "hospital equipment", "x-ray", "ultrasound"
]

# Any ONE of these alone is specific enough to trigger a match
STRONG_KEYWORDS = [
    "biomedical", "hemodialysis", "dialysis machine", "dialysis", "b.braun", "dialog+",
    "water treatment", "x-ray", "xray", "ultrasound", "ct scan", "mri machine", "mri scanner",
    "ventilator", "autoclave", "sterilizer", "anesthesia machine", "patient monitor",
    "surgical equipment", "diagnostic imaging", "imaging equipment", "imaging system",
    "diagnostic equipment", "medical equipment", "hospital equipment",
    "medical device", "calibration", "የህክምና", "ጥገና"
]

# Generic medical-adjacent words — only count if paired with an equipment/
# procurement-type word in the same title (avoids matching HR/insurance/
# consulting tenders that merely mention "health"). Laboratory removed —
# lab-only tenders are explicitly out of scope, see EXCLUDE_TERMS below.
MEDICAL_CONTEXT = ["medical", "health", "hospital", "biomedical"]
EQUIPMENT_CONTEXT = ["equipment", "supplies", "supply", "device", "machine",
                     "instrument", "apparatus", "maintenance", "repair", "procurement",
                     "consultancy", "consulting", "calibration"]

# If any of these appear, skip regardless of other matches — these are the
# recurring false-positive categories (vehicle maintenance, insurance, and
# laboratory-only tenders, which are explicitly out of scope). Note:
# "consultancy" is NOT blanket-excluded — Medmetric offers equipment/biomedical
# consultancy as part of its own business, so a consultancy tender is only
# excluded if it lacks any medical/equipment context (handled by the AND logic
# below), not just for containing the word "consultancy".
EXCLUDE_TERMS = [
    "vehicle", "toyota", "car ", "motorbike", "insurance", "life insurance",
    "term life", "gpa", "laboratory", "lab equipment", "lab supplies", "lab-equipment"
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


GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

try:
    from google import genai
    _genai_client = genai.Client() if GEMINI_API_KEY else None
except Exception as e:
    print(f"⚠️ google-genai import/client init failed: {e}", flush=True)
    _genai_client = None


def ai_confirms_relevant(title_text):
    """Final accuracy gate after the cheap keyword filter passes. Only called
    on candidates that already matched is_relevant_tender(), so this stays
    cheap — a handful of calls per scan, well within Gemini's free daily quota."""
    if not _genai_client:
        return True  # AI not configured — trust the keyword filter alone

    prompt = (
        "You screen procurement tender titles for Medmetric Healthcare Service PLC, "
        "an Ethiopian medical equipment supplier and biomedical maintenance company. "
        "They want tenders for supplying, procuring, or maintaining medical/biomedical/"
        "hospital equipment and devices — for example hemodialysis machines, water "
        "treatment systems, imaging equipment (X-ray, ultrasound, CT/MRI), ventilators, "
        "sterilizers/autoclaves, patient monitors, and similar hospital equipment. "
        "Medmetric also offers biomedical/medical-equipment consultancy and "
        "calibration services, so a CONSULTANCY tender counts as YES if it is "
        "specifically about medical/biomedical equipment, installation, maintenance, "
        "or calibration — not generic business consultancy.\n\n"
        "Answer NO for: laboratory-only equipment/supplies (out of scope even though "
        "medical-adjacent), vehicle maintenance, staff medical insurance, generic "
        "business/financial/legal/HR consultancy unrelated to equipment, construction, "
        "IT services, or anything only mentioning a health-related word in passing.\n\n"
        f"Tender title: \"{title_text}\"\n\n"
        "Answer with exactly one word: YES or NO."
    )
    try:
        response = _genai_client.models.generate_content(
            model="gemini-flash-latest",
            contents=prompt,
        )
        answer = response.text.strip().upper()
        print(f"🤖 AI check for '{title_text[:60]}': {answer}", flush=True)
        return answer.startswith("YES")
    except Exception as e:
        print(f"⚠️ AI check failed, defaulting to keyword match: {e}", flush=True)
        return True  # fail open — don't lose a real tender over an API hiccup

NOTIFIED_TENDERS = set()  # fast in-memory cache within a single run
DATABASE_URL = os.environ.get("DATABASE_URL")


def get_db_conn():
    if not DATABASE_URL:
        return None
    try:
        import psycopg2
        return psycopg2.connect(DATABASE_URL, sslmode='require')
    except Exception as e:
        print(f"⚠️ DB connection failed, falling back to in-memory only: {e}", flush=True)
        return None


def init_db():
    conn = get_db_conn()
    if not conn:
        print("⚠️ No DATABASE_URL set — dedup will NOT survive restarts", flush=True)
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS notified_tenders (
                    tender_id TEXT PRIMARY KEY,
                    notified_at TIMESTAMP DEFAULT NOW()
                )
            """)
        conn.commit()
        print("✅ Persistent dedup table ready", flush=True)
    except Exception as e:
        print(f"⚠️ DB init failed: {e}", flush=True)
    finally:
        conn.close()


def is_notified(tender_id):
    if tender_id in NOTIFIED_TENDERS:
        return True
    conn = get_db_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM notified_tenders WHERE tender_id = %s", (tender_id,))
            return cur.fetchone() is not None
    except Exception as e:
        print(f"⚠️ DB check failed: {e}", flush=True)
        return False
    finally:
        conn.close()


def mark_notified(tender_id):
    NOTIFIED_TENDERS.add(tender_id)
    conn = get_db_conn()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO notified_tenders (tender_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (tender_id,)
            )
        conn.commit()
    except Exception as e:
        print(f"⚠️ DB write failed: {e}", flush=True)
    finally:
        conn.close()


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

            # --- Load tenders listing across multiple pages ---
            seen_this_scan = set()
            for page_num in range(1, MERKATO_MAX_PAGES + 1):
                page_url = MERKATO_TENDERS_URL if page_num == 1 else f"{MERKATO_TENDERS_URL}?page={page_num}"
                page.goto(page_url, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)

                links = page.locator("a[href*='/tenders/']").all()
                print(f"Page {page_num}: found {len(links)} raw tender links", flush=True)

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

                        if is_relevant_tender(title_text) and ai_confirms_relevant(title_text):
                            tender_id = f"merkato_{full_link.rstrip('/').split('/')[-1]}"
                            if not is_notified(tender_id):
                                found += 1
                                mark_notified(tender_id)
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

            # --- Login (optional — only if credentials provided) ---
            if EGP_USER and EGP_PASS:
                try:
                    page.goto(EGP_LOGIN_URL, timeout=30000, wait_until="domcontentloaded")
                    page.wait_for_selector("input[type='password']", timeout=20000)

                    field_info = page.eval_on_selector_all(
                        "input",
                        "els => els.map(e => ({type: e.type, name: e.name, id: e.id, placeholder: e.placeholder}))"
                    )
                    print(f"🔎 eGP login page input fields detected: {field_info}", flush=True)

                    user_field = page.locator(
                        "input[type='email'], input[type='text'], input[name*='user' i], "
                        "input[name*='email' i], input[id*='user' i], input[id*='email' i], "
                        "input[placeholder*='user' i], input[placeholder*='email' i]"
                    ).first
                    pass_field = page.locator("input[type='password']").first

                    if user_field.count() > 0 and pass_field.count() > 0:
                        user_field.fill(EGP_USER)
                        pass_field.fill(EGP_PASS)

                        submit_btn = page.locator(
                            "button[type='submit'], button:has-text('Login'), button:has-text('Sign in'), "
                            "button:has-text('Log in'), input[type='submit']"
                        ).first
                        submit_btn.click()

                        page.wait_for_timeout(4000)
                        current_url = page.url
                        print(f"✅ eGP login submitted, current URL: {current_url}", flush=True)

                        if "organization-selector" in current_url:
                            try:
                                # Wait specifically for the org card to render — the
                                # selector page loads the org list asynchronously, and
                                # querying too early only finds the static Logout button
                                page.wait_for_selector(f"text=/{EGP_ORG_NAME}/i", timeout=15000)

                                clickable_info = page.eval_on_selector_all(
                                    "button, a, li, [role='button']",
                                    "els => els.slice(0, 30).map(e => ({tag: e.tagName, text: e.innerText.trim().slice(0,60), class: e.className}))"
                                )
                                print(f"🔎 Organization-selector clickable elements: {clickable_info}", flush=True)

                                org_option = page.locator(f"text=/{EGP_ORG_NAME}/i").first
                                print(f"🔎 Org name match count: {org_option.count()}", flush=True)
                                if org_option.count() == 0:
                                    # Fallback to generic heuristic if name match fails
                                    org_option = page.locator(
                                        "button:has-text('Continue'), button:has-text('Select'), "
                                        "li:has-text('Continue'), a:has-text('Continue'), "
                                        "[class*='organization' i], [class*='org-card' i], li, button"
                                    ).first

                                if org_option.count() > 0:
                                    org_option.click()
                                    page.wait_for_timeout(3000)
                                    print(f"✅ Clicked organization option, now at: {page.url}", flush=True)
                                else:
                                    print("⚠️ No clickable organization option found", flush=True)
                            except Exception as e:
                                print(f"⚠️ Organization-selector handling failed: {e}", flush=True)
                    else:
                        print(f"⚠️ Could not confidently identify eGP username/password field among: {field_info}", flush=True)
                except Exception as e:
                    print(f"⚠️ eGP login attempt failed, continuing without auth: {e}", flush=True)

            # --- Dismiss any blocking modal (this app uses Ant Design/ng-zorro
            # modals — one may pop up after org selection and intercept clicks) ---
            try:
                modal_close = page.locator(
                    ".ant-modal-close, button:has-text('Close'), button:has-text('OK'), "
                    "button:has-text('Got it'), button:has-text('Dismiss')"
                ).first
                if modal_close.count() > 0:
                    modal_close.click(timeout=5000)
                    page.wait_for_timeout(1000)
                    print("✅ Closed a blocking modal dialog", flush=True)
            except Exception:
                pass
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)

            # --- Navigate to Bidding List via the Tenders nav link (client-side
            # routing — direct URL navigation to /egp/bids/all doesn't load the
            # real table, it just bounces back to the dashboard) ---
            try:
                page.locator("text=Tenders").first.click(timeout=15000)
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
                    page.wait_for_timeout(2500)  # let the table filter update

                    actual_value = search_box.input_value()
                    rows = page.locator("table tbody tr").all()
                    first_row_preview = ""
                    if rows:
                        try:
                            first_row_preview = rows[0].inner_text().replace("\n", " | ")[:100]
                        except Exception:
                            pass
                    print(f"eGP search '{term}' | input value now: '{actual_value}' | {len(rows)} rows | first row: {first_row_preview!r}", flush=True)

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

                            if is_relevant_tender(title_text) and ai_confirms_relevant(title_text):
                                tender_id = f"egp_{ref_no or title_text}"
                                if not is_notified(tender_id):
                                    found += 1
                                    mark_notified(tender_id)

                                    # Try a plain anchor href inside the row first —
                                    # cheap, no navigation needed
                                    detail_link = EGP_BIDS_URL
                                    try:
                                        row_anchor = row.locator("a").first
                                        if row_anchor.count() > 0:
                                            href = row_anchor.get_attribute("href")
                                            if href:
                                                detail_link = href if href.startswith("http") else f"{EGP_BASE}{href}"
                                                print(f"🔗 Found row anchor href: {detail_link}", flush=True)
                                    except Exception:
                                        pass

                                    # No plain href — click the row to capture the
                                    # real URL the SPA navigates to, then go back and
                                    # restore the search filter
                                    if detail_link == EGP_BIDS_URL:
                                        try:
                                            row.click(timeout=8000)
                                            page.wait_for_timeout(2500)
                                            detail_link = page.url
                                            print(f"🔗 Captured detail URL via click-through: {detail_link}", flush=True)
                                            page.go_back(timeout=8000)
                                            page.wait_for_timeout(1500)
                                            search_box = page.locator("input[placeholder*='Search' i]").first
                                            search_box.click()
                                            search_box.fill("")
                                            page.wait_for_timeout(200)
                                            search_box.fill(term)
                                            search_box.press("Enter")
                                            page.wait_for_timeout(2500)
                                        except Exception as e:
                                            print(f"⚠️ Click-through for detail link failed: {e}", flush=True)
                                            detail_link = EGP_BIDS_URL

                                    alert = (
                                        f"🔔 *New Medical Tender Found (eGP)!*\n\n"
                                        f"📋 *Title:* {title_text}\n"
                                        f"🧾 *Ref No:* {ref_no}\n"
                                        f"🔗 *Link:* {detail_link}"
                                    )
                                    send_whatsapp(alert)
                                    time.sleep(2)
                        except Exception:
                            continue
                except Exception as e:
                    print(f"⚠️ eGP search for '{term}' failed: {e}", flush=True)
                    continue

            browser.close()

        print(f"eGP engine complete. Discovered {found} active matches.", flush=True)
        return found

    except Exception as e:
        print(f"❌ eGP extraction engine down: {e}", flush=True)
        return 0


# ==================== RUN COORDINATOR ====================
def check_for_tenders():
    print("=================== STARTING SCAN CYCLE ===================", flush=True)
    merkato_total = scrape_2merkato()
    egp_total = scrape_egp()
    total = merkato_total + egp_total
    print(f"=================== SCAN COMPLETE: {total} NEW FOUND ({merkato_total} 2merkato, {egp_total} eGP) ===================", flush=True)

    if total == 0:
        send_whatsapp("🔍 *Tender Monitor Scan Completed.*\nNo new unique medical equipment or maintenance matches found on 2merkato or eGP.")


def monitoring_loop():
    while True:
        check_for_tenders()
        time.sleep(4 * 3600)


# ==================== FLASK ROUTES ====================
@app.route('/')
def home():
    return "Medical Tender Tracking Service Is Online!", 200


@app.route('/create-group')
def create_group():
    from flask import request
    subject = request.args.get("subject", "Medmetric Tender Alerts")
    participants_param = request.args.get("participants", "")
    participants = [p.strip() for p in participants_param.split(",") if p.strip()]
    if not participants:
        return "Provide ?participants=2519...,2519... (comma-separated, no + or 00)", 400

    url = f"{EVOLUTION_BASE}/group/create/{INSTANCE_NAME}"
    payload = {"subject": subject, "description": "Automated medical tender alerts", "participants": participants}
    headers = {"Content-Type": "application/json", "apikey": GLOBAL_API_KEY}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        return f"Status {r.status_code}<br><pre>{r.text}</pre>", 200
    except Exception as e:
        return f"Error: {e}", 500


@app.route('/list-groups')
def list_groups():
    url = f"{EVOLUTION_BASE}/group/fetchAllGroups/{INSTANCE_NAME}"
    headers = {"apikey": GLOBAL_API_KEY}
    try:
        r = requests.get(url, headers=headers, params={"getParticipants": "false"}, timeout=15)
        return f"Status {r.status_code}<br><pre>{r.text}</pre>", 200
    except Exception as e:
        return f"Error: {e}", 500


@app.route('/test-check')
def manual_test():
    threading.Thread(target=check_for_tenders).start()
    return "Scraper sync cycle triggered!", 200


if __name__ == "__main__":
    init_db()
    threading.Thread(target=monitoring_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
