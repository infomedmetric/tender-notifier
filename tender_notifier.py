import os
import time
import json
import requests
import threading
import multiprocessing
import traceback
import hmac
import psycopg2
from datetime import datetime
from flask import Flask, request
from playwright.sync_api import sync_playwright
import urllib3
from openai import OpenAI

def _call_groq_with_retry(client, system_instruction, prompt, temperature=0.2, max_retries=3, base_delay=2):
    """
    Wraps a Groq chat.completions.create call with retries + exponential
    backoff, mirroring the same reliability pattern used before with
    Gemini — a single transient network blip shouldn't immediately produce
    a fallback/degraded result.

    Model is configurable via GROQ_MODEL (defaults to openai/gpt-oss-120b).
    Groq deprecated and removed llama-3.3-70b-versatile — every call to it
    now fails with a 404 model_not_found error, confirmed across multiple
    scans even with a valid API key, so this isn't a transient issue.
    openai/gpt-oss-120b is Groq's own current recommended replacement.
    Groq's free tier also has its own rate limits, so batching everything
    into one call per scan cycle (see batch_analyze_tenders below) still
    matters just as much here as it did with Gemini.
    """
    model_name = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
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



# Keywords used to locate the parts of a detail page worth keeping when the
# full captured text (up to 6000 chars) is too long to send to the AI in
# full. A blind [:N] truncation was cutting off the closing date and
# eligibility/constraints info entirely whenever a page's nav/breadcrumb/menu
# text ran long before the real content — which is exactly why those fields
# kept coming back empty even though the scraper WAS capturing the text.
_CLOSING_DATE_HINTS = [
    "closing date", "submission deadline", "bid submission", "deadline",
    "closing time", "bid closing", "last date", "due date", "valid until",
    "validity", "opening date",
]
_CONSTRAINT_HINTS = [
    "eligib", "bid bond", "bid security", "minimum requirement", "experience",
    "similar contract", "qualification", "capital", "registration",
    "local agent", "representative", "certificate", "license", "requirement",
]


def _build_detail_snippet(detail_text: str, max_chars: int = 2400) -> str:
    """
    Instead of blindly slicing the first N characters (which was silently
    dropping the closing date and constraints whenever they appeared later
    in the page than the cutoff), keep the front of the page for
    title/scope context PLUS a window of text around any closing-date or
    constraint-related keyword found anywhere in the full captured text.
    This makes it far more likely the AI actually sees the relevant
    sentence instead of nav/menu boilerplate.
    """
    if not detail_text:
        return ""
    if len(detail_text) <= max_chars:
        return detail_text

    lower = detail_text.lower()
    front_budget = max_chars // 2
    front = detail_text[:front_budget]

    windows = []
    remaining_budget = max_chars - front_budget
    for hint in _CLOSING_DATE_HINTS + _CONSTRAINT_HINTS:
        if remaining_budget <= 0:
            break
        idx = lower.find(hint)
        if idx == -1 or idx < front_budget:
            continue  # already covered by the front slice, or not present
        start = max(0, idx - 60)
        end = min(len(detail_text), idx + 240)
        window = detail_text[start:end]
        if window not in windows:
            windows.append(window)
            remaining_budget -= len(window)

    return front + "\n[...]\n" + "\n[...]\n".join(windows) if windows else front


def _build_batch_prompt(chunk_with_indices):
    """Builds the system + user prompt for one sub-batch of (index, candidate) pairs."""
    entries = []
    for i, c in chunk_with_indices:
        snippet = _build_detail_snippet(c.get('detail_text', ''))
        entries.append(
            f"[{i}] Source: {c['source']} | Title: {c['title']} | Ref No: {c.get('ref_no', '')}\n"
            f"Detail: {snippet}"
        )
    joined_entries = "\n\n".join(entries)

    system_instruction = (
        "You are an expert procurement analyst for medMETRIC Healthcare Service PLC, "
        "an Ethiopian biomedical engineering and medical equipment technical-services provider. Their scope "
        "covers: medical equipment Corrective and preventive maintenance & service contracts (including hemodialysis "
        "systems like B.Braun Dialog+ and SWS-4000A), RO/water treatment systems, CSSD & "
        "sterilization equipment (e.g. Rivamed or Aquaboss), corrective and preventive maintenance of "
        " medical imaging equipment, Procurement of Maintenance and Repair Service, also medmetric participates on International Competitive Bidding (ICB) which is related to medical equipment "
        "and general medical equipment supply/consultancy. They do NOT supply medicines, must check since they don't work on "
        "pharmaceuticals, vaccines, or laboratory-only equipment/reagents/consumables — mark "
        "those NOT relevant even if they mention \"medical\" in passing.\n\n"
        "You will be given a numbered list of tenders, each with a Detail excerpt taken from the tender's "
        "own detail page (it may include a front section plus separate windows of text pulled from around "
        "date/eligibility keywords elsewhere on the page, joined by [...] markers — treat each window as "
        "real page text, not as connected prose).\n\n"
        "Analyze EACH tender independently and return a JSON object with a single key \"tenders\" whose "
        "value is an array — one object per tender, covering every index given, with EXACTLY this shape:\n"
        '{"tenders": [{'
        '"index": 0, '
        '"relevant": true, '
        '"match_score": 85, '
        '"reason": "short sentence on why it matches or not", '
        '"procurement_object": "the object/type of procurement as stated on the page, e.g. \'Supply and installation of hemodialysis machines\'", '
        '"constraints": "key eligibility/bid requirements actually stated in the text — e.g. bid bond amount, minimum years of similar experience, required certifications, local representation requirement. '
        'If the provided excerpt does not contain this info, respond exactly with \'Not stated in provided text\' — do NOT say \'None\' or \'None identified\', since that implies you confirmed there are none.", '
        '"closing_date": "the bid submission deadline / closing date (and time, if given) EXACTLY as it appears in the text, including the date format used. '
        'If no such date appears anywhere in the excerpt, respond exactly with \'Not stated in provided text\'. Do not confuse this with a publish date, site-visit date, or clarification-request date."'
        "}, ...]}\n"
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

    # Sized conservatively: detail snippets now run up to ~2,400 chars/
    # candidate (up from 900) so the AI can actually see closing dates and
    # constraints that appear later in the page rather than always seeing
    # blank space. ~6 candidates per chunk keeps prompt+completion tokens
    # comfortably under Groq's 12,000 TPM limit for openai/gpt-oss-120b,
    # even accounting for the system instruction and JSON completion overhead.
    CHUNK_SIZE = 6

    # Uses the standard OpenAI client pointed at Groq's OpenAI-compatible
    # endpoint (per Groq's own current setup instructions), rather than the
    # dedicated groq package. Deliberately still calling
    # chat.completions.create() with response_format={"type": "json_object"}
    # below (not the newer client.responses.create()) — Groq's Responses API
    # is explicitly marked "currently in beta" in their docs, and this
    # pipeline runs unattended and depends on getting valid, parseable JSON
    # back every single time. Chat Completions + JSON mode is the
    # well-established path for that; nothing here needed the beta endpoint.
    client = OpenAI(api_key=api_key, base_url="https://api.groq.com/openai/v1")
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

# The eGP domain has moved/varied before (production.egp.gov.et vs plain
# egp.gov.et, and there's a real chance it could live at something like
# egp.ppa.gov.et in the future). Making this a single env-var-driven
# constant means switching domains is a Render dashboard change, not a
# code change — every URL below (login, bids listing, home-navigation
# recovery, detail-page link resolution) derives from this one value.
# .rstrip("/") guards against a trailing slash in the env var producing
# doubled slashes in every derived URL below (e.g. "https://egp.gov.et/"
# + "/egp/login" would otherwise become ".../egp.gov.et//egp/login").
EGP_BASE = os.environ.get("EGP_BASE", "https://production.egp.gov.et").rstrip("/")
EGP_LOGIN_URL = f"{EGP_BASE}/egp/login"
EGP_BIDS_URL = f"{EGP_BASE}/egp/bids/all"
EGP_USER = os.environ.get("EGP_USER")
EGP_PASS = os.environ.get("EGP_PASS")
EGP_ORG_NAME = os.environ.get("EGP_ORG_NAME", "medMETRIC Healthcare Service PLC")

# OpenID Connect / IdentityServer settings discovered from the frontend bundle
# and /.well-known/openid-configuration. The SPA itself uses the password grant
# with these exact client credentials, so we can do the same and completely
# bypass the browser-based login (and the inspect-not-allowed protection).
EGP_TOKEN_URL = f"{EGP_BASE}/auth/connect/token"
EGP_CLIENT_ID = os.environ.get("EGP_CLIENT_ID", "skoruba_identity_admin")
EGP_CLIENT_SECRET = os.environ.get("EGP_CLIENT_SECRET", "skoruba_admin_client_secret")
EGP_SCOPES = "openid profile email roles erp_api offline_access"

# Search terms fed one at a time into eGP's own built-in table search box —
# far more reliable than scraping every row across 13+ pages.
# Rebuilt around medMETRIC's actual service lines (medmetrichealthcare.com):
# maintenance/service contracts, RO water treatment, CSSD/sterilization,
# and calibration — not just dialysis. "laboratory" intentionally dropped —
# lab-only tenders are explicitly out of scope.
EGP_SEARCH_TERMS = [
    "Procurement of Maintenance and Repair Service", "Procurement of Medical Equipment and Supplies", "hemodialysis", "dialysis",
    "medical equipment", "sterilization", "water treatment", "Reverse osmosis", "filters",
    "x-ray", "ultrasound", "medical imaging", "membrane", "ICB", "International Competitive Bid",
    "B Braun", "Dialog+", "ዲያሊሲስ",
]

# Search terms fed one at a time into 2merkato's own "Search by keyword" filter
# panel — mirrors the EGP_SEARCH_TERMS approach above. 2merkato's general
# listing has 25,000+ pages, so paging through the first few of those (the
# old approach) only ever saw whatever happened to be posted most recently
# across ALL categories, not specifically medical/biomedical tenders. Reusing
# the site's own keyword search narrows results down to a handful of pages
# per term, the same way it does on eGP.
MERKATO_SEARCH_TERMS = [
    "hemodialysis", "dialysis", "medical equipment", "sterilization",
    "water treatment", "reverse osmosis", "x-ray", "ultrasound",
    "medical imaging", "biomedical", "CSSD", "autoclave", "ICB",
    "medical equipment maintenance", "hospital equipment",
    "B Braun", "Dialog+", "ዲያሊሲስ",
]

# How many result pages to walk PER search term (not per whole catalog) —
# a filtered keyword search returns far fewer pages than the full listing,
# so a small number here is plenty and keeps the scan fast.
MERKATO_MAX_PAGES_PER_TERM = int(os.environ.get("MERKATO_MAX_PAGES_PER_TERM", "3"))

# Any ONE of these alone is specific enough to trigger a match.
# Includes medMETRIC's actual named service lines (CSSD, RO/water treatment,
# calibration, spare parts, biomedical engineering) so tenders aren't
# under-scored just because they're not dialysis-specific.
STRONG_KEYWORDS = [
    "ICB", "hemodialysis", "dialysis", "bbraun", "philips", "SWS",
    "x-ray", "xray", "ultrasound", "CT Scan", "autoclave", "Water treatment",
    "Procurement of Maintenance and Repair Service", "cssd", "Procurement of Medical Equipment and Supplies",
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
                     "ICB", "corrective maintenance", "maintenance", "repair", "procurement of medical equipment",
                     "calibration", "medical equipment installation", "servicing",
                     "Maintenance and Repair Service", "medical consultancy" ]

# Always excluded regardless of context — these categories are never
# relevant to Medmetric no matter what else appears in the title.
# Laboratory-only tenders and pure medicine/pharmaceutical supply tenders
# (e.g. "RDF Medicines...") are explicitly out of scope — medMETRIC is a
# biomedical engineering/technical-service company, not a drug supplier.
HARD_EXCLUDE_TERMS = [
    "vehicle", "toyota", "car ", "motorbike", "insurance", "life insurance",
    "term life", "gpa", "spare part", "construction","road"
    "laboratory", "lab reagent", "reagent", "lab equipment",
    "medicine", "medicines", "pharmaceutical", "pharmaceuticals",
    "drug", "drugs", "vaccine", "vaccines", "rdf medicine", "rdf medicines"
]

# Excluded UNLESS the title also shows clear medical + equipment context —
# generic "consultancy services" for HR/finance/etc. should be skipped, but
# "medical equipment consultancy services" should NOT be, since Medmetric
# offers exactly that
CONTEXTUAL_EXCLUDE_TERMS = ["car", "consulting firm"]


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
                    page.goto(MERKATO_LOGIN_URL, timeout=30000, wait_until="domcontentloaded")
                    page.wait_for_selector("input[type='password']", timeout=20000)

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
                        page.wait_for_timeout(4000)
                        print(f"✅ Submitted login, current URL: {page.url}", flush=True)
                    else:
                        print(f"⚠️ Could not confidently identify email/username field among: {field_info}", flush=True)
                except Exception as e:
                    print(f"⚠️ Login attempt failed, continuing without auth: {e}", flush=True)

            page.goto(MERKATO_TENDERS_URL, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

            search_input = page.locator(
                "input[placeholder*='Search by keyword' i], input[placeholder*='keyword' i]"
            ).first

            if search_input.count() == 0:
                header_clickables = page.eval_on_selector_all(
                    "header *, nav *",
                    "els => els.slice(0, 40).map(e => ({tag: e.tagName, "
                    "aria: e.getAttribute('aria-label'), cls: e.className, "
                    "text: (e.innerText || '').trim().slice(0, 30)}))"
                )
                print(f"🔎 2merkato search input not immediately visible. Header elements: {header_clickables}", flush=True)

                toggle = page.locator(
                    "[aria-label*='search' i], button:has-text('Search'), "
                    "header svg, nav svg, [class*='search-icon' i], [class*='search-toggle' i]"
                ).first
                if toggle.count() > 0:
                    try:
                        toggle.click(timeout=5000)
                        page.wait_for_timeout(1000)
                        print("✅ Clicked a probable search-toggle icon in the header", flush=True)
                    except Exception as e:
                        print(f"⚠️ Could not click search-toggle candidate: {e}", flush=True)

                search_input = page.locator(
                    "input[placeholder*='Search by keyword' i], input[placeholder*='keyword' i]"
                ).first

            if search_input.count() == 0:
                print("❌ 2merkato search-by-keyword input never appeared — falling back to the old first-page listing so this scan isn't a total loss.", flush=True)
                links = page.locator("a[href*='/tenders/']").all()
                print(f"Fallback listing page: found {len(links)} raw tender links", flush=True)
                found += _process_merkato_links(links, context, candidates, seen_this_scan=set())
                browser.close()
                print(f"2merkato engine complete. Collected {found} candidate matches for AI review.", flush=True)
                return found

            print("✅ Found 2merkato 'Search by keyword' input — proceeding with keyword-driven search", flush=True)

            seen_this_scan = set()
            for term in MERKATO_SEARCH_TERMS:
                try:
                    if search_input.count() == 0 or not search_input.is_visible():
                        toggle = page.locator(
                            "[aria-label*='search' i], button:has-text('Search'), "
                            "header svg, nav svg, [class*='search-icon' i], [class*='search-toggle' i]"
                        ).first
                        if toggle.count() > 0:
                            try:
                                toggle.click(timeout=5000)
                                page.wait_for_timeout(1000)
                            except Exception:
                                pass
                        search_input = page.locator(
                            "input[placeholder*='Search by keyword' i], input[placeholder*='keyword' i]"
                        ).first

                    search_input.click()
                    search_input.fill("")
                    page.wait_for_timeout(200)
                    search_input.fill(term)

                    url_before = page.url
                    search_input.press("Enter")
                    page.wait_for_timeout(2000)

                    if page.url == url_before:
                        filter_btn = page.locator("button:has-text('Filter')").first
                        if filter_btn.count() > 0:
                            try:
                                filter_btn.click(timeout=5000, force=True)
                            except Exception as e:
                                print(f"⚠️ Forced Filter click also failed for '{term}': {e}", flush=True)

                    page.wait_for_timeout(2500)
                    print(f"➡️ 2merkato search '{term}' submitted, current URL: {page.url}", flush=True)

                    for page_num in range(1, MERKATO_MAX_PAGES_PER_TERM + 1):
                        if page_num > 1:
                            next_btn = page.locator(
                                "button:has-text('Next'), a:has-text('Next'), [aria-label*='next' i]"
                            ).first
                            if next_btn.count() == 0:
                                break
                            try:
                                next_btn.click(timeout=5000)
                                page.wait_for_timeout(2000)
                            except Exception:
                                break

                        links = page.locator("a[href*='/tenders/']").all()
                        print(f"2merkato search '{term}' page {page_num}: found {len(links)} raw tender links", flush=True)
                        if not links:
                            break

                        found += _process_merkato_links(links, context, candidates, seen_this_scan)

                except Exception as e:
                    print(f"⚠️ 2merkato search for '{term}' failed: {e}", flush=True)
                    continue

            browser.close()

        print(f"2merkato engine complete. Collected {found} candidate matches for AI review.", flush=True)
        return found

    except Exception as e:
        print(f"❌ 2merkato extraction engine down: {e}", flush=True)
        return 0


def _get_merkato_card_status(link) -> str:
    try:
        container = link.locator("xpath=ancestor::*[contains(., 'Bidding')][1]").first
        if container.count() == 0:
            return "unknown"
        card_text = container.inner_text(timeout=3000)
    except Exception:
        return "unknown"

    lower = card_text.lower()
    idx = lower.find("bidding")
    if idx == -1:
        return "unknown"
    window = lower[idx: idx + 40]
    if "closed" in window:
        return "closed"
    if "open" in window:
        return "open"
    return "unknown"


def _process_merkato_links(links, context, candidates: list, seen_this_scan: set) -> int:
    found = 0
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
                    status = _get_merkato_card_status(link)
                    if status == "closed":
                        print(f"⏭️ Skipping closed 2merkato tender (Bidding: Closed): {title_text}", flush=True)
                        mark_as_notified(tender_id)
                        continue

                    mark_as_notified(tender_id)
                    found += 1

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
    return found


def _egp_get_tokens():
    """
    Obtain access_token + refresh_token via the Resource Owner Password
    Credentials grant. This is exactly what the official Angular SPA does
    on login, so it is a supported (and currently working) path.
    """
    if not EGP_USER or not EGP_PASS:
        print("⚠️ EGP_USER / EGP_PASS not set — cannot obtain tokens", flush=True)
        return None

    data = {
        "grant_type": "password",
        "username": EGP_USER,
        "password": EGP_PASS,
        "client_id": EGP_CLIENT_ID,
        "client_secret": EGP_CLIENT_SECRET,
        "scope": EGP_SCOPES,
    }
    try:
        r = requests.post(
            EGP_TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
            verify=False,
        )
        if r.status_code != 200:
            print(f"❌ eGP token request failed ({r.status_code}): {r.text[:300]}", flush=True)
            return None
        tokens = r.json()
        if "access_token" not in tokens:
            print(f"❌ eGP token response missing access_token: {tokens}", flush=True)
            return None
        print(f"✅ eGP tokens obtained (expires_in={tokens.get('expires_in')}s, scope={tokens.get('scope')})", flush=True)
        return tokens
    except Exception as e:
        print(f"❌ eGP token request exception: {e}", flush=True)
        return None


def _egp_inject_tokens(context, page, tokens):
    """Inject OIDC tokens into browser localStorage so the SPA is authenticated."""
    import time as _time
    now_ms = int(_time.time() * 1000)
    expires_in = int(tokens.get("expires_in", 3600))
    expires_at = now_ms + (expires_in * 1000)

    page.goto(f"{EGP_BASE}/egp/", timeout=30000, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)

    access_token = tokens["access_token"]
    refresh_token = tokens.get("refresh_token", "")
    id_token = tokens.get("id_token", "")
    scope = tokens.get("scope", EGP_SCOPES)

    script = f"""
    () => {{
        localStorage.setItem('access_token', {json.dumps(access_token)});
        localStorage.setItem('refresh_token', {json.dumps(refresh_token)});
        localStorage.setItem('id_token', {json.dumps(id_token)});
        localStorage.setItem('expires_at', '{expires_at}');
        localStorage.setItem('access_token_stored_at', '{now_ms}');
        localStorage.setItem('granted_scopes', {json.dumps(scope.split())});
        localStorage.setItem('ps/store/access_token', {json.dumps(access_token)});
        try {{ sessionStorage.setItem('access_token', {json.dumps(access_token)}); }} catch (e) {{}}
        return true;
    }}
    """
    page.evaluate(script)
    print("✅ Injected eGP tokens into browser localStorage", flush=True)
    context.set_extra_http_headers({"Authorization": f"Bearer {access_token}"})


def _egp_worker(result_queue):
    local_candidates = []
    try:
        found = scrape_egp(local_candidates)
        result_queue.put({"error": None, "candidates": local_candidates, "found": found})
    except Exception as e:
        result_queue.put({"error": str(e), "candidates": local_candidates, "found": 0})


def run_egp_with_timeout(candidates: list, timeout_seconds: int = 300) -> int:
    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue()
    proc = ctx.Process(target=_egp_worker, args=(result_queue,))
    proc.start()
    proc.join(timeout=timeout_seconds)

    if proc.is_alive():
        print(f"⏱️ eGP engine exceeded {timeout_seconds}s and appears hung — terminating it so the rest of the scan cycle isn't blocked. Will retry next cycle.", flush=True)
        proc.terminate()
        proc.join(timeout=10)
        if proc.is_alive():
            proc.kill()
            proc.join()
        return 0

    try:
        result = result_queue.get_nowait()
    except Exception:
        print("⚠️ eGP worker process exited without returning a result (likely crashed before reporting back).", flush=True)
        return 0

    if result["error"]:
        print(f"❌ eGP engine failed in subprocess: {result['error']}", flush=True)

    candidates.extend(result["candidates"])
    return result["found"]


# ==================== ENGINE: eGP (egp.gov.et) ====================
def scrape_egp(candidates: list):
    print(f"[{datetime.now()}] 🔍 Running eGP Engine (Playwright)...", flush=True)
    found = 0

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ]
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1366, "height": 768},
                locale="en-US",
                timezone_id="Africa/Addis_Ababa",
            )
            context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                window.chrome = window.chrome || { runtime: {} };
            """)
            page = context.new_page()

            # --- Token-based login (bypasses inspect-not-allowed) ---
            if EGP_USER and EGP_PASS:
                tokens = _egp_get_tokens()
                if not tokens:
                    print("❌ Could not obtain eGP tokens — aborting eGP engine for this cycle", flush=True)
                    browser.close()
                    return 0

                try:
                    _egp_inject_tokens(context, page, tokens)

                    page.goto(f"{EGP_BASE}/egp/home", timeout=30000, wait_until="domcontentloaded")
                    page.wait_for_timeout(3000)
                    print(f"✅ Post-token navigation URL: {page.url}", flush=True)

                    # Organization selection
                    try:
                        print("🏢 Checking for organization selection prompt...", flush=True)
                        try:
                            page.wait_for_selector(
                                f"text=/{EGP_ORG_NAME}/i, text=MEDMETRIC HEALTHCARE SERVICE PLC, "
                                ".card, [class*='org' i], [role='button']",
                                timeout=12000
                            )
                        except Exception:
                            pass

                        org_card = page.locator(f"text=/{EGP_ORG_NAME}/i").first
                        if org_card.count() == 0:
                            org_card = page.locator("text=MEDMETRIC HEALTHCARE SERVICE PLC").first

                        if org_card.count() > 0:
                            org_card.click()
                            print("✅ Organization selected successfully.", flush=True)
                            page.wait_for_timeout(4000)
                        else:
                            fallback_org = page.locator(
                                ".card, div[role='button'], .list-group-item, "
                                "[class*='organization' i], [class*='org-card' i], "
                                "li, button:has-text('Continue'), button:has-text('Select')"
                            ).first
                            if fallback_org.count() > 0:
                                fallback_org.click()
                                print("✅ Fallback organization selected.", flush=True)
                                page.wait_for_timeout(4000)
                            else:
                                print("ℹ️ No organization card found — assuming already selected or not required.", flush=True)

                        if "/login" in page.url or "organization-selector" in page.url or "inspect-not-allowed" in page.url:
                            print(f"⚠️ Still on {page.url} after org step — trying /egp/home again", flush=True)
                            page.goto(f"{EGP_BASE}/egp/home", timeout=20000, wait_until="domcontentloaded")
                            page.wait_for_timeout(2000)
                            print(f"🔎 URL after recovery: {page.url}", flush=True)
                    except Exception as org_err:
                        print(f"ℹ️ Organization selection skipped or auto-resolved: {org_err}", flush=True)

                except Exception as e:
                    print(f"⚠️ Token injection / post-login navigation failed: {e}", flush=True)
                    try:
                        print(f"🔎 Page URL: {page.url}", flush=True)
                        print(f"🔎 Body snippet: {page.locator('body').inner_text(timeout=3000)[:400]}", flush=True)
                    except Exception:
                        pass
                    browser.close()
                    return 0
            else:
                print("ℹ️ EGP_USER/EGP_PASS not set — continuing without authentication", flush=True)

            # Dismiss blocking modals
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
                try:
                    overlay = page.locator(".cdk-overlay-backdrop, nz-modal-container").first
                    if overlay.count() == 0:
                        break
                    backdrop = page.locator(".cdk-overlay-backdrop").first
                    if backdrop.count() > 0:
                        backdrop.click(timeout=2000, force=True)
                        page.wait_for_timeout(500)
                except Exception:
                    pass

            # Navigate to Bidding List
            try:
                tenders_link = page.locator("nav a:has-text('Tenders'), header a:has-text('Tenders')").first
                if tenders_link.count() == 0:
                    tenders_link = page.get_by_role("link", name="Tenders", exact=True)
                if tenders_link.count() == 0:
                    tenders_link = page.locator("text=Tenders").first

                try:
                    tenders_link.click(timeout=15000)
                except Exception as click_err:
                    print(f"⚠️ Normal click on Tenders failed ({click_err}), forcing click", flush=True)
                    tenders_link.click(timeout=10000, force=True)
                page.wait_for_selector("text=/Bidding List/i", timeout=15000)
                print("✅ Reached Bidding List view", flush=True)
            except Exception as e:
                print(f"❌ Could not reach Bidding List view: {e}", flush=True)
                try:
                    print(f"🔎 Current URL: {page.url}", flush=True)
                    print(f"🔎 Body snippet: {page.locator('body').inner_text(timeout=3000)[:500]}", flush=True)
                except Exception:
                    pass
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

                    actual_value = search_box.input_value()
                    rows = page.locator("table tbody tr").all()
                    first_row_preview = ""
                    if rows:
                        try:
                            first_row_preview = rows[0].inner_text().replace("\\n", " | ")[:100]
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
                                    mark_as_notified(tender_id)
                                    found += 1

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

                                    detail_text = ""
                                    if detail_link and detail_link != EGP_BIDS_URL:
                                        detail_page = None
                                        try:
                                            detail_page = context.new_page()
                                            detail_page.goto(detail_link, timeout=20000, wait_until="domcontentloaded")
                                            try:
                                                detail_page.wait_for_load_state("networkidle", timeout=10000)
                                            except Exception:
                                                pass
                                            for poll_attempt in range(5):
                                                detail_page.wait_for_timeout(1500)
                                                try:
                                                    candidate_text = detail_page.locator("body").inner_text(timeout=5000)
                                                except Exception:
                                                    candidate_text = ""
                                                if len(candidate_text) > 300:
                                                    detail_text = candidate_text
                                                    break
                                                detail_text = candidate_text
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
        egp_found = run_egp_with_timeout(candidates)
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
                procurement_object = ""
                constraints = "Unknown — AI unavailable"
                closing_date = "Unknown — AI unavailable"
                ai_verified = False
            elif result is None:
                # Batch call succeeded but this particular index was missing
                # from the response — same fallback, just for one item
                is_relevant = True
                match_score = "?"
                reason = "Missing from AI batch response — keyword match only"
                procurement_object = ""
                constraints = "Unknown — AI unavailable"
                closing_date = "Unknown — AI unavailable"
                ai_verified = False
            else:
                is_relevant = bool(result.get("relevant", True))
                match_score = result.get("match_score", "?")
                reason = result.get("reason", "")
                procurement_object = result.get("procurement_object", "")
                # "Not stated in provided text" (not "None identified") is the
                # correct fallback here — the AI was only shown an excerpt of
                # the page, so an empty result means "not found in what we
                # sent it", not "confirmed there are none".
                constraints = result.get("constraints") or "Not stated in provided text"
                closing_date = result.get("closing_date") or "Not stated in provided text"
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
            object_line = f"🏷️ *Procurement Object:* {procurement_object}\n" if procurement_object else ""
            source_label = "eGP" if c["source"] == "egp" else "2merkato"
            label = f"New Medical Tender Found ({source_label})!"

            alert = (
                f"🔔 *{label}*\n\n"
                f"{verification_note}"
                f"📋 *Title:* {c['title']}\n"
                f"{ref_line}"
                f"{object_line}"
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
