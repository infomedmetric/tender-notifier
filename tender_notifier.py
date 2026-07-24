import os
import time
import json
import requests
import threading
import traceback
import hmac
import psycopg2
from datetime import datetime
from flask import Flask, request
from playwright.sync_api import sync_playwright
import urllib3
from google import genai
from google.genai import types

def _call_gemini_with_retry(client, prompt, config, max_retries=3, base_delay=2):
    """
    Wraps a Gemini generate_content call with retries + exponential backoff.
    Most "connection error" fallbacks and irrelevant-tenders-slipping-through
    cases were caused by a SINGLE transient network blip immediately giving
    up — Render's free tier + Gemini's free tier both see occasional timeouts
    that succeed on a second attempt.
    """
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            return client.models.generate_content(
                model='gemini-flash-latest',
                contents=prompt,
                config=config,
            )
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                delay = base_delay * attempt  # 2s, 4s, 6s...
                print(f"⚠️ Gemini call failed (attempt {attempt}/{max_retries}): {e} — retrying in {delay}s", flush=True)
                time.sleep(delay)
    raise last_error


def batch_analyze_tenders(candidates: list):
    """
    Analyzes ALL candidate tenders from a scan cycle in a SINGLE Gemini call,
    instead of the old approach of one (or two) calls per tender. The free
    tier quota is only 20 requests/day — with even a handful of matching
    tenders per scan, the old per-tender approach could burn the entire
    day's quota in one cycle (see the 429 RESOURCE_EXHAUSTED errors in the
    logs). Batching means a scan with 15 candidate tenders costs 1 request
    instead of up to 30.

    Returns a dict {index: {relevant, match_score, reason, constraints,
    closing_date}} on success, or None if the AI call failed entirely (even
    after retries) — the caller should then fall back to sending all
    candidates flagged as unverified rather than silently dropping them.
    """
    if not candidates:
        return {}

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None

    entries = []
    for i, c in enumerate(candidates):
        entries.append(
            f"[{i}] Source: {c['source']} | Title: {c['title']} | Ref No: {c.get('ref_no', '')}\n"
            f"Detail: {c.get('detail_text', '')[:2000]}"
        )
    joined_entries = "\n\n".join(entries)

    system_instruction = (
        "You are an expert procurement analyst for medMETRIC Healthcare Service PLC, "
        "an Ethiopian biomedical engineering and technical-services company. Their scope "
        "covers: medical equipment maintenance & service contracts (including hemodialysis "
        "systems like B.Braun Dialog+ and SWS-4000A), RO/water treatment systems, CSSD & "
        "sterilization equipment (e.g. Rivamed), calibration & compliance testing of "
        "diagnostic/therapeutic equipment, medical imaging equipment, spare-parts sourcing, "
        "and general medical equipment supply/consultancy. They do NOT supply medicines, "
        "pharmaceuticals, vaccines, or laboratory-only equipment/reagents/consumables — mark "
        "those NOT relevant even if they mention \"medical\" in passing. "
        "You will be given a numbered list of tenders. Analyze EACH ONE independently and "
        "return a single JSON array — one object per tender, covering every index given, "
        "with EXACTLY this shape and nothing else (no markdown fences, no commentary):\n"
        '[{"index": 0, "relevant": true, "match_score": 85, '
        '"reason": "short 1-sentence reason", "constraints": "None identified", '
        '"closing_date": "Not specified in provided text"}, ...]'
    )

    prompt = f"Tenders to analyze:\n\n{joined_entries}"

    try:
        client = genai.Client(api_key=api_key)
        response = _call_gemini_with_retry(
            client,
            prompt,
            types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.2,
            ),
        )
        raw = (response.text or "").strip()
        # Strip markdown code fences if the model added them anyway
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        parsed = json.loads(raw)
        results = {}
        for item in parsed:
            idx = item.get("index")
            if idx is not None:
                results[idx] = item
        return results
    except Exception as e:
        print(f"⚠️ Batch AI analysis failed after retries: {e}", flush=True)
        return None


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# ================== CONFIGURATION ==================
EVOLUTION_BASE = os.environ.get("EVOLUTION_BASE", "https://medmetric-evolution.onrender.com")
INSTANCE_NAME = os.environ.get("INSTANCE_NAME", "Tender-Notifier.")

# SECURITY: no hardcoded fallback — a real API key must never live in source
# code, especially in a public repo. If it's missing, we log a clear warning
# at startup rather than silently sending requests with an empty key.
GLOBAL_API_KEY = os.environ.get("GLOBAL_API_KEY")
if not GLOBAL_API_KEY:
    print("⚠️ GLOBAL_API_KEY is not set — WhatsApp sends will fail until it's configured in Render's environment variables.", flush=True)

# SECURITY/PRIVACY: no hardcoded phone number — recipients must be supplied
# via the WHATSAPP_NUMBERS env var (comma-separated).
WHATSAPP_NUMBERS = [
    n.strip() for n in os.environ.get("WHATSAPP_NUMBERS", "").split(",") if n.strip()
]
if not WHATSAPP_NUMBERS:
    print("⚠️ WHATSAPP_NUMBERS is not set — no recipients configured.", flush=True)

# Shared secret required to trigger a manual scan via /test-check. Without
# this, anyone who discovers the public Render URL could trigger scans on
# demand, burning your Gemini quota and spamming your WhatsApp numbers.
TEST_CHECK_TOKEN = os.environ.get("TEST_CHECK_TOKEN")

MERKATO_USER = os.environ.get("MERKATO_USER")
MERKATO_PASS = os.environ.get("MERKATO_PASS")

# CORRECT domain — the working tenders app, not www.2merkato.com
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

# Search terms fed one at a time into eGP's own built-in table search box —
# far more reliable than scraping every row across 13+ pages.
# Rebuilt around medMETRIC's actual service lines (medmetrichealthcare.com):
# maintenance/service contracts, RO water treatment, CSSD/sterilization,
# and calibration — not just dialysis. "laboratory" intentionally dropped —
# lab-only tenders are explicitly out of scope.
EGP_SEARCH_TERMS = [
    "medical equipment", "biomedical", "hemodialysis", "dialysis",
    "calibration", "sterilization", "water treatment", "hospital equipment",
    "x-ray", "ultrasound", "medical imaging", "spare parts"
]

# Any ONE of these alone is specific enough to trigger a match.
# Includes medMETRIC's actual named service lines (CSSD, RO/water treatment,
# calibration, spare parts, biomedical engineering) so tenders aren't
# under-scored just because they're not dialysis-specific.
STRONG_KEYWORDS = [
    "biomedical", "hemodialysis", "dialysis", "b.braun", "dialog+", "sws-4000a",
    "x-ray", "xray", "ultrasound", "ventilator", "autoclave", "sterilizer",
    "sterilization", "sterile processing", "cssd", "rivamed",
    "reverse osmosis", "ro system", "water treatment", "spare parts",
    "biomedical engineering", "medical imaging", "calibration",
    "diagnostic equipment", "medical equipment", "hospital equipment",
    "medical device", "የህክምና", "የህክምና መሣሪያ ጥገና"
]

# Generic medical-adjacent words — only count if paired with an equipment/
# procurement-type word in the same title (avoids matching HR/insurance/
# consulting tenders that merely mention "health"). "laboratory" removed —
# lab-only tenders are handled by HARD_EXCLUDE_TERMS below instead.
MEDICAL_CONTEXT = ["medical", "health", "hospital", "biomedical", "clinical"]
EQUIPMENT_CONTEXT = ["equipment", "supplies", "supply", "device", "machine",
                     "instrument", "apparatus", "maintenance", "repair", "procurement",
                     "calibration", "installation", "servicing", "spare parts",
                     "consulting", "consultancy", "icb"]

# Always excluded regardless of context — these categories are never
# relevant to Medmetric no matter what else appears in the title.
# Laboratory-only tenders and pure medicine/pharmaceutical supply tenders
# (e.g. "RDF Medicines...") are explicitly out of scope — medMETRIC is a
# biomedical engineering/technical-service company, not a drug supplier.
HARD_EXCLUDE_TERMS = [
    "vehicle", "toyota", "car ", "motorbike", "insurance", "life insurance",
    "term life", "gpa",
    "laboratory", "lab reagent", "reagent", "lab equipment",
    "medicine", "medicines", "pharmaceutical", "pharmaceuticals",
    "drug", "drugs", "vaccine", "vaccines", "rdf medicine", "rdf medicines"
]

# Excluded UNLESS the title also shows clear medical + equipment context —
# generic "consultancy services" for HR/finance/etc. should be skipped, but
# "medical equipment consultancy services" should NOT be, since Medmetric
# offers exactly that
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
        # else: it's medical-equipment consultancy — fall through, don't exclude

    if any(term in title_lower for term in STRONG_KEYWORDS):
        return True

    return has_medical_context and has_equipment_context


# ==================== PERSISTENT DEDUP (Postgres) ====================
DATABASE_URL = os.environ.get("DATABASE_URL")

# Reuse a single connection across the whole scan instead of opening/closing
# a new one per tender — repeated SSL handshakes were adding real memory/CPU
# overhead on Render's free tier and were the likely cause of the scan
# stalling or getting silently killed partway through page 1.
_db_conn = None


def get_db_connection():
    global _db_conn
    if _db_conn is not None:
        try:
            # Cheap liveness check — reuse the connection if it's still good
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
    """Run once at startup to ensure the dedup table exists."""
    if not DATABASE_URL:
        print("⚠️ DATABASE_URL not set — falling back to in-memory dedup (will reset on restart)", flush=True)
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
        print("✅ Connected to Postgres and verified notified_tenders table", flush=True)
    except Exception as e:
        print(f"❌ Postgres init failed, falling back to in-memory dedup: {e}", flush=True)


# In-memory fallback used only if DATABASE_URL is missing or unreachable —
# keeps the app functional (just without persistence) rather than crashing
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
    except Exception as e:
        print(f"⚠️ DB check failed for {tender_id}, using memory fallback: {e}", flush=True)
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
    except Exception as e:
        print(f"⚠️ DB insert failed for {tender_id}, using memory fallback: {e}", flush=True)
        _MEMORY_FALLBACK.add(tender_id)


def send_whatsapp(message):
    if not GLOBAL_API_KEY or not WHATSAPP_NUMBERS:
        print("❌ Cannot send WhatsApp message: GLOBAL_API_KEY or WHATSAPP_NUMBERS is not configured.", flush=True)
        return

    url = f"{EVOLUTION_BASE}/message/sendText/{INSTANCE_NAME}"
    headers = {"Content-Type": "application/json", "apikey": GLOBAL_API_KEY}
    for number in WHATSAPP_NUMBERS:
        payload = {"number": number, "text": message}
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=10)
            # PRIVACY: mask most of the phone number in logs rather than
            # printing it in full — logs can end up shared/exported more
            # widely than intended
            masked = number[:5] + "***" + number[-2:] if len(number) > 7 else "***"
            print(f"✅ WhatsApp Status ({masked}): {r.status_code}", flush=True)
        except Exception as e:
            print(f"❌ Send error: {e}", flush=True)


# ==================== ENGINE: 2MERKATO (Playwright) ====================
def scrape_2merkato(candidates: list):
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
                print(f"➡️ Loading 2merkato page {page_num}: {page_url}", flush=True)
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
                            if not is_already_notified(tender_id):
                                # Mark as notified immediately regardless of the eventual
                                # AI verdict — prevents re-checking (and re-spending
                                # Gemini quota on) the same tender on every scan cycle
                                mark_as_notified(tender_id)

                                found += 1

                                # Fetch the actual detail page — the listing title alone
                                # never contains the submission deadline, which is why
                                # closing date kept coming back "not specified." Open it
                                # in a SEPARATE tab so we don't disturb pagination state
                                # on the main listing page. This is a Playwright call, not
                                # a Gemini call, so it doesn't touch the AI quota.
                                detail_text = ""
                                detail_page = None
                                try:
                                    detail_page = context.new_page()
                                    detail_page.goto(full_link, timeout=20000, wait_until="domcontentloaded")
                                    try:
                                        detail_page.wait_for_load_state("networkidle", timeout=10000)
                                    except Exception:
                                        pass
                                    for poll_attempt in range(5):
                                        detail_page.wait_for_timeout(1000)
                                        try:
                                            candidate_text = detail_page.locator("body").inner_text(timeout=5000)
                                        except Exception:
                                            candidate_text = ""
                                        if len(candidate_text) > 300:
                                            detail_text = candidate_text
                                            break
                                        detail_text = candidate_text
                                    detail_text = detail_text[:6000]
                                    print(f"📄 Captured {len(detail_text)} chars from 2merkato detail page", flush=True)
                                except Exception as e:
                                    print(f"⚠️ Could not read 2merkato detail page text: {e}", flush=True)
                                finally:
                                    if detail_page is not None:
                                        try:
                                            detail_page.close()
                                        except Exception:
                                            pass

                                # Collect for the single end-of-scan batch AI call rather
                                # than calling Gemini right here per-tender
                                candidates.append({
                                    "id": tender_id,
                                    "source": "2merkato",
                                    "title": title_text,
                                    "ref_no": "",
                                    "detail_text": detail_text,
                                    "link": full_link,
                                })
                    except Exception:
                        continue

            browser.close()

        print(f"2merkato engine complete. Collected {found} candidate matches for AI review.", flush=True)
        return found

    except Exception as e:
        print(f"❌ 2merkato extraction engine down: {e}", flush=True)
        return 0


# ==================== ENGINE: eGP (egp.gov.et) ====================
def scrape_egp(candidates: list):
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

                        if "/login" in current_url:
                            # Still on the login page — the login itself
                            # failed (bad credentials, site-side validation
                            # error, etc). Surface whatever error message the
                            # page is showing and abort this scan cleanly
                            # rather than timing out repeatedly on a
                            # "Tenders" link that will never appear.
                            try:
                                error_text = page.locator(
                                    "[class*='error' i], [class*='alert' i], [role='alert'], "
                                    "text=/invalid/i, text=/incorrect/i, text=/failed/i"
                                ).first.inner_text(timeout=3000)
                            except Exception:
                                error_text = "(no visible error message found on page)"
                            print(f"❌ eGP login failed — still on login page. On-page message: {error_text}", flush=True)
                            browser.close()
                            return 0

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
            # modals — one may pop up after org selection and intercept clicks).
            # Logs showed an nz-modal-container sitting inside a
            # cdk-overlay-container that our old .ant-modal-close selector
            # didn't match, so we now target that structure directly too and
            # retry Escape a few times since one press isn't always enough. ---
            for attempt in range(3):
                try:
                    modal_close = page.locator(
                        ".ant-modal-close, .nz-modal-close, [class*='modal-close' i], "
                        "button:has-text('Close'), button:has-text('OK'), "
                        "button:has-text('Got it'), button:has-text('Dismiss')"
                    ).first
                    if modal_close.count() > 0:
                        modal_close.click(timeout=3000)
                        page.wait_for_timeout(800)
                        print(f"✅ Closed a blocking modal dialog (attempt {attempt + 1})", flush=True)
                except Exception:
                    pass

                page.keyboard.press("Escape")
                page.wait_for_timeout(500)

                # If an overlay/modal container is still present, click its
                # backdrop to force it closed rather than waiting on a
                # specific button that may not exist for this modal type
                try:
                    overlay = page.locator(".cdk-overlay-backdrop, nz-modal-container").first
                    if overlay.count() == 0:
                        break  # nothing left blocking — stop retrying
                    backdrop = page.locator(".cdk-overlay-backdrop").first
                    if backdrop.count() > 0:
                        backdrop.click(timeout=2000, force=True)
                        page.wait_for_timeout(500)
                        print(f"✅ Clicked overlay backdrop to dismiss modal (attempt {attempt + 1})", flush=True)
                except Exception:
                    pass

            # --- Navigate to Bidding List via the Tenders nav link (client-side
            # routing — direct URL navigation to /egp/bids/all doesn't load the
            # real table, it just bounces back to the dashboard) ---
            try:
                tenders_link = page.locator("text=Tenders").first
                try:
                    tenders_link.click(timeout=15000)
                except Exception as click_err:
                    # A leftover overlay can still intercept the click even
                    # after the dismissal attempts above — force it through
                    # rather than giving up the whole scan
                    print(f"⚠️ Normal click on Tenders failed ({click_err}), forcing click", flush=True)
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

                            if is_relevant_tender(title_text):
                                tender_id = f"egp_{ref_no or title_text}"
                                if not is_already_notified(tender_id):
                                    # Mark as notified immediately regardless of the
                                    # eventual AI verdict — prevents re-checking (and
                                    # re-spending Gemini quota on) the same tender
                                    # on every scan cycle
                                    mark_as_notified(tender_id)

                                    found += 1

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

                                    # Fetch the actual detail page text — the title/ref_no
                                    # alone never contain closing date, bid bond, or
                                    # eligibility info, which is why those fields were
                                    # always coming back "not specified." We open the
                                    # detail page in a SEPARATE tab so we never disturb
                                    # the main page's search box / filtered results.
                                    # This is a Playwright call, not a Gemini call, so it
                                    # doesn't touch the AI quota.
                                    detail_text = ""
                                    if detail_link and detail_link != EGP_BIDS_URL:
                                        detail_page = None
                                        try:
                                            detail_page = context.new_page()
                                            detail_page.goto(detail_link, timeout=20000, wait_until="domcontentloaded")
                                            # Angular SPA detail views render fields async — a
                                            # fixed short wait was catching the loading skeleton
                                            # (both captures logged an identical, suspiciously
                                            # small 144 chars). Wait for network activity to
                                            # settle, then poll for the text to actually grow
                                            # past a "still loading" size before giving up.
                                            try:
                                                detail_page.wait_for_load_state("networkidle", timeout=10000)
                                            except Exception:
                                                pass  # some SPAs never go fully idle — fine, we still poll below

                                            detail_text = ""
                                            for poll_attempt in range(5):
                                                detail_page.wait_for_timeout(1500)
                                                try:
                                                    candidate_text = detail_page.locator("body").inner_text(timeout=5000)
                                                except Exception:
                                                    candidate_text = ""
                                                # Real detail content (title, ref, dates, scope,
                                                # eligibility) runs to many hundreds of chars —
                                                # treat anything under ~300 as still loading
                                                if len(candidate_text) > 300:
                                                    detail_text = candidate_text
                                                    break
                                                detail_text = candidate_text  # keep best-effort fallback

                                            # Trim to a sane size — some detail pages include
                                            # long boilerplate/nav text we don't need to send
                                            # to Gemini, and it wastes tokens/quota
                                            detail_text = detail_text[:6000]
                                            print(f"📄 Captured {len(detail_text)} chars from eGP detail page", flush=True)
                                        except Exception as e:
                                            print(f"⚠️ Could not read eGP detail page text: {e}", flush=True)
                                        finally:
                                            if detail_page is not None:
                                                try:
                                                    detail_page.close()
                                                except Exception:
                                                    pass

                                    # Collect for the single end-of-scan batch AI call
                                    # rather than calling Gemini right here per-tender
                                    candidates.append({
                                        "id": tender_id,
                                        "source": "egp",
                                        "title": title_text,
                                        "ref_no": ref_no,
                                        "detail_text": detail_text,
                                        "link": detail_link,
                                    })
                        except Exception:
                            continue

                except Exception as e:
                    print(f"⚠️ eGP search for '{term}' failed: {e}", flush=True)
                    continue

            browser.close()

        print(f"eGP engine complete. Collected {found} candidate matches for AI review.", flush=True)
        return found

    except Exception as e:
        print(f"❌ eGP extraction engine down: {e}", flush=True)
        return 0


# ==================== RUN COORDINATOR ====================
def check_for_tenders():
    print("=================== STARTING SCAN CYCLE ===================", flush=True)

    candidates = []  # populated by both scrapers; analyzed in ONE batch AI call below

    try:
        merkato_found = scrape_2merkato(candidates)
    except Exception:
        print("❌ 2merkato engine crashed with an unhandled exception:", flush=True)
        print(traceback.format_exc(), flush=True)
        merkato_found = 0

    try:
        egp_found = scrape_egp(candidates)
    except Exception:
        print("❌ eGP engine crashed with an unhandled exception:", flush=True)
        print(traceback.format_exc(), flush=True)
        egp_found = 0

    print(f"🧮 {len(candidates)} total candidate tenders collected ({merkato_found} 2merkato, {egp_found} eGP) — running one batch AI review", flush=True)

    total_sent = 0
    if candidates:
        # ONE Gemini call for the entire scan cycle, covering every candidate
        # from both engines — instead of the old per-tender approach that
        # could burn the whole day's free-tier quota (20 requests/day) in a
        # single scan.
        batch_results = batch_analyze_tenders(candidates)

        for i, c in enumerate(candidates):
            result = batch_results.get(i) if batch_results is not None else None

            if batch_results is None:
                # The whole batch call failed even after retries (e.g. quota
                # exhausted). Fall back to sending everything that passed the
                # keyword filter, clearly flagged as unverified, rather than
                # silently dropping legitimate tenders.
                is_relevant = True
                match_score = "?"
                reason = "AI batch analysis unavailable — keyword match only"
                constraints = "Unknown — AI unavailable"
                closing_date = "Unknown — AI unavailable"
                ai_verified = False
            elif result is None:
                # Batch call succeeded but this particular index was missing
                # from the response — same fallback, just for one item
                is_relevant = True
                match_score = "?"
                reason = "Missing from AI batch response — keyword match only"
                constraints = "Unknown — AI unavailable"
                closing_date = "Unknown — AI unavailable"
                ai_verified = False
            else:
                is_relevant = bool(result.get("relevant", True))
                match_score = result.get("match_score", "?")
                reason = result.get("reason", "")
                constraints = result.get("constraints", "None identified")
                closing_date = result.get("closing_date", "Not specified in provided text")
                ai_verified = True

            if not is_relevant:
                print(f"🤖 AI rejected as not relevant, skipping alert: {c['title']}", flush=True)
                continue

            verification_note = "" if ai_verified else "⚠️ *Unverified* (AI review failed — keyword match only)\n\n"
            ref_line = f"📄 *Ref No:* {c['ref_no']}\n\n" if c.get("ref_no") else ""
            label = "New Medical Tender Found (eGP)!" if c["source"] == "egp" else "New Medical Tender Found!"

            alert = (
                f"🔔 *{label}*\n\n"
                f"{verification_note}"
                f"📋 *Title:* {c['title']}\n"
                f"{ref_line}"
                f"🤖 *AI Analysis:*\n"
                f"🎯 *Match Score:* {match_score}% - {reason}\n"
                f"⚠️ *Constraints:* {constraints}\n"
                f"📅 *Closing Date:* {closing_date}\n\n"
                f"🔗 *Link:* {c['link']}"
            )
            send_whatsapp(alert)
            total_sent += 1
            time.sleep(2)

    total = total_sent
    print(f"=================== SCAN COMPLETE: {total} ALERTS SENT ({len(candidates)} candidates reviewed) ===================", flush=True)

    if total == 0:
        send_whatsapp("🔍 *Tender Monitor Scan Completed.*\nNo new unique medical equipment or maintenance matches found on 2merkato or eGP.")


def monitoring_loop():
    # Configurable via Render env var so this doesn't need a code change next time —
    # defaults to 6 hours if SCAN_INTERVAL_HOURS isn't set
    try:
        interval_hours = float(os.environ.get("SCAN_INTERVAL_HOURS", "6"))
    except ValueError:
        interval_hours = 6
    while True:
        try:
            check_for_tenders()
        except Exception:
            print("❌ check_for_tenders crashed at the top level:", flush=True)
            print(traceback.format_exc(), flush=True)
        time.sleep(interval_hours * 3600)


# ==================== FLASK ROUTES ====================
@app.route('/')
def home():
    return "Medical Tender Tracking Service Is Online!", 200


@app.route('/test-check')
def manual_test():
    # SECURITY: without this check, anyone who finds the public Render URL
    # could trigger a scan on demand — burning Gemini API quota and sending
    # WhatsApp messages at will. Require a shared secret token to proceed.
    if not TEST_CHECK_TOKEN:
        return "Manual trigger is disabled: TEST_CHECK_TOKEN is not configured.", 503

    provided_token = request.args.get("token", "")
    if not hmac.compare_digest(provided_token, TEST_CHECK_TOKEN):
        return "Unauthorized", 401

    threading.Thread(target=check_for_tenders).start()
    return "Scraper sync cycle triggered!", 200


# Ensure the dedup table exists as soon as the module loads (covers both
# `python app.py` local runs and gunicorn/production imports)
init_db()

if __name__ == "__main__":
    threading.Thread(target=monitoring_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
