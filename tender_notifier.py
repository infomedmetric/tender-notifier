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
from groq import Groq

def _call_groq_with_retry(client, system_instruction, prompt, temperature=0.2, max_retries=3, base_delay=2):
    """
    Wraps a Groq chat.completions.create call with retries + exponential
    backoff, mirroring the same reliability pattern used before with
    Gemini — a single transient network blip shouldn't immediately produce
    a fallback/degraded result.

    Model is configurable via GROQ_MODEL (defaults to llama-3.3-70b-versatile,
    a solid, widely-available free-tier Groq model). Groq's free tier also
    has its own rate limits, so batching everything into one call per scan
    cycle (see batch_analyze_tenders below) still matters just as much here
    as it did with Gemini.
    """
    model_name = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            return client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": prompt},
                ],
                temperature=temperature,
                response_format={"type": "json_object"},
            )
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                delay = base_delay * attempt  # 2s, 4s, 6s...
                print(f"⚠️ Groq call failed (attempt {attempt}/{max_retries}): {e} — retrying in {delay}s", flush=True)
                time.sleep(delay)
    raise last_error


def _build_batch_prompt(chunk_with_indices):
    """Builds the system + user prompt for one sub-batch of (index, candidate) pairs."""
    entries = []
    for i, c in chunk_with_indices:
        entries.append(
            f"[{i}] Source: {c['source']} | Title: {c['title']} | Ref No: {c.get('ref_no', '')}\n"
            f"Detail: {c.get('detail_text', '')[:900]}"
        )
    joined_entries = "\n\n".join(entries)

    system_instruction = (
        "You are an expert procurement analyst for medMETRIC Healthcare Service PLC, "
        "an Ethiopian biomedical engineering and medical equipment technical-services provider. Their scope "
        "covers: medical equipment Corrective and preventive maintenance & service contracts (including hemodialysis "
        "systems like B.Braun Dialog+ and SWS-4000A), RO/water treatment systems, CSSD & "
        "sterilization equipment (e.g. Rivamed or Aquaboss), corrective and preventive maintenance of "
        " medical imaging equipment, also medmetric participates on International Competitive Bidding (ICB) which is related to medical equipment "
        "and general medical equipment supply/consultancy. They do NOT supply medicines, must check since they don't work on "
        "pharmaceuticals, vaccines, or laboratory-only equipment/reagents/consumables — mark "
        "those NOT relevant even if they mention \"medical\" in passing. "
        "You will be given a numbered list of tenders. Analyze EACH ONE independently as per the profiles and "
        "return a JSON object with a single key \"tenders\" whose value is an array — one "
        "object per tender, covering every index given, with EXACTLY this shape:\n"
        '{"tenders": [{"index": 0, "relevant": true, "match_score": 85, '
        '"reason": "short sentence reason", "Object": "Object of Procurement types listed", '
        '"closing_date": "show Bid Submission Deadline"}, ...]}\n'
        "Respond with ONLY that JSON object — no markdown fences, no commentary."
    )
    prompt = f"Tenders to analyze:\n\n{joined_entries}"
    return system_instruction, prompt


def batch_analyze_tenders(candidates: list):
    """
    Analyzes ALL candidate tenders from a scan cycle in as FEW Groq calls as
    possible — instead of one call per tender. Even free-tier LLM APIs cap
    daily/per-minute requests (both a request count AND a tokens-per-minute
    budget), so a per-tender approach can burn through a day's quota in one
    scan.

    A single-mega-request approach hit Groq's TPM (tokens per minute) limit
    once there were 15-20+ candidates with detail text attached (413 "Request
    too large"). So candidates are split into smaller sub-batches sized to
    stay comfortably under the TPM limit, each sent as its own Groq call —
    still far fewer calls than one per tender (e.g. 23 candidates becomes
    ~3 calls instead of 23 or 46).

    Returns a dict {index: {relevant, match_score, reason, object,
    closing_date}} on success (possibly missing some indices if only some
    sub-batches failed — the caller already treats a missing index as
    "unverified, fall back to keyword match"), or None if EVERY sub-batch
    failed (e.g. no API key, or Groq unreachable entirely).
    """
    if not candidates:
        return {}

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None

    # Sized conservatively: with detail_text capped at 900 chars/candidate,
    # ~7 candidates per chunk keeps prompt+completion tokens well under
    # Groq's 12,000 TPM limit for llama-3.3-70b-versatile, even accounting
    # for the system instruction and JSON completion overhead.
    CHUNK_SIZE = 7

    client = Groq(api_key=api_key)
    indexed_candidates = list(enumerate(candidates))
    chunks = [indexed_candidates[i:i + CHUNK_SIZE] for i in range(0, len(indexed_candidates), CHUNK_SIZE)]

    all_results = {}
    any_chunk_succeeded = False

    for chunk_num, chunk in enumerate(chunks, start=1):
        system_instruction, prompt = _build_batch_prompt(chunk)
        try:
            response = _call_groq_with_retry(client, system_instruction, prompt, temperature=0.2)
            raw = (response.choices[0].message.content or "").strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            parsed = json.loads(raw)
            items = parsed.get("tenders", []) if isinstance(parsed, dict) else parsed
            for item in items:
                idx = item.get("index")
                if idx is not None:
                    all_results[idx] = item
            any_chunk_succeeded = True
        except Exception as e:
            print(f"⚠️ Batch AI analysis sub-batch {chunk_num}/{len(chunks)} failed after retries: {e}", flush=True)
            # Leave this sub-batch's indices out of all_results — the caller
            # already falls back to "unverified, keyword match only" for any
            # index missing from the results dict

        # Small pause between sub-batches so back-to-back chunks don't
        # themselves stack up against the TPM window
        if chunk_num < len(chunks):
            time.sleep(3)

    return all_results if any_chunk_succeeded else None


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
# demand, burning your Groq quota and spamming your WhatsApp numbers.
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
    "medical equipment maintenance", "Corrective maintenance", "hemodialysis", "dialysis",
    "medical equipment", "sterilization", "water treatment", "Reverse osmosis", "RO",
    "x-ray", "ultrasound", "medical imaging", "spare parts", "ICB", "International Competitive Bid",
]

# Any ONE of these alone is specific enough to trigger a match.
# Includes medMETRIC's actual named service lines (CSSD, RO/water treatment,
# calibration, spare parts, biomedical engineering) so tenders aren't
# under-scored just because they're not dialysis-specific.
STRONG_KEYWORDS = [
    "ICB", "hemodialysis", "dialysis", "b.braun", "dialog", "SWS",
    "x-ray", "xray", "ultrasound", "CT", "autoclave", "Water treatment",
    "RO", "sterile processing", "cssd", "Corrective maintenance",
    "reverse osmosis", "ro system", "SPHMMC", "technical service",
    "biomedical engineering", "medical imaging", "calibration",
    "EPSA", "medical equipment", "hospital equipment",
    "medical device", "የህክምና ጥገና",
]

# Generic medical-adjacent words — only count if paired with an equipment/
# procurement-type word in the same title (avoids matching HR/insurance/
# consulting tenders that merely mention "health"). "laboratory" removed —
# lab-only tenders are handled by HARD_EXCLUDE_TERMS below instead.
MEDICAL_CONTEXT = ["medical", "health", "hospital", "biomedical", "clinical"]
EQUIPMENT_CONTEXT = ["equipment", "supplies", "supply", "device", "machine",
                     "ICB", "corrective", "maintenance", "repair", "procurement of medical equipment",
                     "calibration", "installation", "servicing",
                     "consulting", "medical consultancy", "icb"]

# Always excluded regardless of context — these categories are never
# relevant to Medmetric no matter what else appears in the title.
# Laboratory-only tenders and pure medicine/pharmaceutical supply tenders
# (e.g. "RDF Medicines...") are explicitly out of scope — medMETRIC is a
# biomedical engineering/technical-service company, not a drug supplier.
HARD_EXCLUDE_TERMS = [
    "vehicle", "toyota", "car ", "motorbike", "insurance", "life insurance",
    "term life", "gpa", "spare part"
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
                                # AI quota on) the same tender on every scan cycle
                                mark_as_notified(tender_id)

                                found += 1

                                # Fetch the actual detail page — the listing title alone
                                # never contains the submission deadline, which is why
                                # closing date kept coming back "not specified." Open it
                                # in a SEPARATE tab so we don't disturb pagination state
                                # on the main listing page. This is a Playwright call, not
                                # an AI call, so it doesn't touch the AI quota.
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
                                # than calling the AI right here per-tender
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
                                    # re-spending AI quota on) the same tender
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
                                    # This is a Playwright call, not an AI call, so it
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
                                            # to the AI, and it wastes tokens/quota
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
                                    # rather than calling the AI right here per-tender
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
        # ONE (or a handful of) Groq call(s) for the entire scan cycle, covering
        # every candidate from both engines — instead of the old per-tender
        # approach that could burn a day's free-tier quota in a single scan.
        # NOTE: this only batches the AI ANALYSIS step, not the WhatsApp
        # notifications — each tender still gets sent as its own message
        # below, since that's easier to read/forward than one long combined
        # message.
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
                # Log the AI's actual reasoning, not just the title — makes
                # it possible to sanity-check a borderline rejection (e.g. a
                # title that LOOKED relevant) without digging through the
                # WhatsApp thread for context that was never sent there
                print(f"🤖 AI rejected as not relevant, skipping alert: {c['title']} — reason: {reason or '(no reason returned)'}", flush=True)
                continue

            verification_note = "⚠️ *Unverified* (AI review failed — keyword match only)\n\n" if not ai_verified else ""
            ref_line = f"📄 *Ref No:* {c['ref_no']}\n\n" if c.get("ref_no") else ""
            source_label = "eGP" if c["source"] == "egp" else "2merkato"
            label = f"New Medical Tender Found ({source_label})!"

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
    # could trigger a scan on demand — burning Groq API quota and sending
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
