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
                page.locator("text=Tenders").first.click(timeout=8000, force=True)
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
                    page.wait_for_timeout(3000)

                    def _read_rows():
                        rows_ = page.locator("table tbody tr").all()
                        parsed_ = []
                        for r in rows_:
                            try:
                                parsed_.append(r.locator("td").all_inner_texts())
                            except Exception:
                                parsed_.append([])
                        return rows_, parsed_

                    rows, parsed_rows = _read_rows()
                    # If cell text hasn't populated yet (rows exist but all cells are
                    # blank/whitespace), give it one more moment and re-read once
                    if rows and all(not "".join(c).strip() for c in parsed_rows):
                        page.wait_for_timeout(2000)
                        rows, parsed_rows = _read_rows()

                    actual_value = search_box.input_value()
                    first_row_preview = parsed_rows[0] if parsed_rows else []
                    print(f"eGP search '{term}' | input value now: '{actual_value}' | {len(rows)} rows | first row cells: {first_row_preview!r}", flush=True)

                    for cells in parsed_rows:
                        try:
                            if not cells or not "".join(cells).strip():
                                continue
                            ref_no = cells[0].strip() if len(cells) > 0 else ""
                            title_text = cells[2].strip() if len(cells) > 2 else " | ".join(cells)
                            if not title_text:
                                continue

                            row_key = ref_no or title_text
                            if row_key in seen_this_scan:
                                continue
                            seen_this_scan.add(row_key)

                            if is_relevant_tender(title_text):
                                tender_id = f"egp_{ref_no or title_text}"
                                if tender_id not in NOTIFIED_TENDERS:
                                    found += 1
                                    NOTIFIED_TENDERS.add(tender_id)
                                    alert = (
                                        f"🔔 *New Medical Tender Found (eGP)!*\n\n"
                                        f"📋 *Title:* {title_text}\n"
                                        f"🧾 *Ref No:* {ref_no}\n"
                                        f"🔗 *Portal:* {EGP_BIDS_URL}"
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


@app.route('/test-check')
def manual_test():
    threading.Thread(target=check_for_tenders).start()
    return "Scraper sync cycle triggered!", 200


if __name__ == "__main__":
    threading.Thread(target=monitoring_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
