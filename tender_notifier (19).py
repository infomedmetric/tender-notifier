"""
Medmetric Tender Notifier
==========================
Scans Ethiopian procurement portals (eGP and 2merkato) for tenders relevant
to medMETRIC Healthcare Service PLC, filters them with an AI relevance check
(Groq), deduplicates against a Postgres (Neon) table, and sends WhatsApp
alerts via an Evolution API instance for each newly matched tender.

Architecture notes (see project memory for full history):
- eGP: no browser automation. Hits the Angular app's public JSON API
  directly via `requests` (no login required for listing), runs in-process
  (no multiprocessing/subprocess timeout wrapper -- that approach caused a
  bug where collected candidates were lost when the subprocess was killed).
- 2merkato: still uses Playwright (JS-rendered portal), keyword-driven
  search (not blind pagination), with a closed-tender status badge check.
- AI filtering: Groq API via the `openai` package pointed at Groq's
  OpenAI-compatible endpoint. Model is configurable via GROQ_MODEL env var.
- Dedup: PostgreSQL on Neon.tech, `notified_tenders` table. Tenders are
  marked as seen immediately regardless of AI verdict, to avoid re-spending
  API quota re-evaluating the same tender on a later scan.
- WhatsApp: Evolution API, one message per matched tender (not batched).
- Everything sensitive/environment-specific (URLs, keys, phone numbers,
  model name, scan interval) is env-var driven -- no secrets in source.
"""

import os
import re
import time
import json
import hmac
import logging
import threading
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, request, jsonify
from openai import OpenAI
import psycopg2
from psycopg2 import pool

# Playwright is only needed for 2merkato
from playwright.sync_api import sync_playwright

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("tender_notifier")


def mask_phone(number: str) -> str:
    """Mask a phone number for safe logging, e.g. +2519******74."""
    if not number or len(number) < 6:
        return "***"
    return number[:5] + "*" * (len(number) - 7) + number[-2:]


# --------------------------------------------------------------------------
# Configuration (all env-var driven)
# --------------------------------------------------------------------------
class Config:
    # --- eGP ---
    EGP_BASE = os.environ.get("EGP_BASE", "https://production.egp.gov.et")
    EGP_SEARCH_ENDPOINT = f"{EGP_BASE}/po-gw/cms-v2/api/sourcing/get-sourcing"
    EGP_SEARCH_TERMS = [
        t.strip()
        for t in os.environ.get(
            "EGP_SEARCH_TERMS",
            "hemodialysis,dialysis,B Braun,Dialog+,biomedical,"
            "medical equipment,sterilization,autoclave,RO water treatment,"
            "ዲያሊሲስ,የህክምና መሣሪያዎች ጥገና",
        ).split(",")
        if t.strip()
    ]

    # --- 2merkato ---
    MERKATO_BASE = os.environ.get("MERKATO_BASE", "https://tender.2merkato.com")
    MERKATO_USERNAME = os.environ.get("MERKATO_USERNAME", "")
    MERKATO_PASSWORD = os.environ.get("MERKATO_PASSWORD", "")
    MERKATO_SEARCH_TERMS = [
        t.strip()
        for t in os.environ.get(
            "MERKATO_SEARCH_TERMS",
            "hemodialysis,dialysis,biomedical,medical equipment,"
            "hospital equipment,sterilization,CSSD,calibration,"
            "medical imaging,spare parts medical",
        ).split(",")
        if t.strip()
    ]

    # --- Hard exclusions (never touch without explicit instruction) ---
    HARD_EXCLUDE_TERMS = [
        "laboratory",
        "reagent",
        "medicine",
        "pharmaceutical",
        "vaccine",
        "drug",
    ]

    # --- Groq / AI ---
    GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
    GROQ_BASE_URL = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
    GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
    AI_BATCH_SIZE = int(os.environ.get("AI_BATCH_SIZE", "7"))
    AI_DETAIL_CHARS = int(os.environ.get("AI_DETAIL_CHARS", "900"))

    # --- WhatsApp / Evolution API ---
    EVOLUTION_API_URL = os.environ.get(
        "EVOLUTION_API_URL", "https://medmetric-evolution.onrender.com"
    )
    EVOLUTION_INSTANCE = os.environ.get("EVOLUTION_INSTANCE", "Tender-Notifier.")
    EVOLUTION_API_KEY = os.environ.get("EVOLUTION_API_KEY", "")
    NOTIFY_PHONE_NUMBERS = [
        n.strip()
        for n in os.environ.get("NOTIFY_PHONE_NUMBERS", "").split(",")
        if n.strip()
    ]

    # --- Database (Neon Postgres) ---
    DATABASE_URL = os.environ.get("DATABASE_URL", "")

    # --- Scan interval & scheduling ---
    SCAN_INTERVAL_HOURS = float(os.environ.get("SCAN_INTERVAL_HOURS", "6"))

    # --- /test-check route protection ---
    TEST_CHECK_TOKEN = os.environ.get("TEST_CHECK_TOKEN", "")

    @classmethod
    def validate(cls):
        warnings = []
        if not cls.GROQ_API_KEY:
            warnings.append("GROQ_API_KEY is not set -- AI filtering will fail.")
        if not cls.EVOLUTION_API_KEY:
            warnings.append("EVOLUTION_API_KEY is not set -- WhatsApp sending will fail.")
        if not cls.NOTIFY_PHONE_NUMBERS:
            warnings.append("NOTIFY_PHONE_NUMBERS is not set -- no recipients configured.")
        if not cls.DATABASE_URL:
            warnings.append("DATABASE_URL is not set -- dedup will fail.")
        if not cls.TEST_CHECK_TOKEN:
            warnings.append("TEST_CHECK_TOKEN is not set -- /test-check route is unprotected.")
        for w in warnings:
            log.warning("STARTUP WARNING: %s", w)
        if cls.NOTIFY_PHONE_NUMBERS:
            log.info(
                "Notify recipients: %s",
                ", ".join(mask_phone(n) for n in cls.NOTIFY_PHONE_NUMBERS),
            )


# --------------------------------------------------------------------------
# Database (Neon Postgres) -- persistent connection pool w/ liveness checks
# --------------------------------------------------------------------------
class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self._pool = None
        self._lock = threading.Lock()
        if dsn:
            self._connect()

    def _connect(self):
        self._pool = psycopg2.pool.SimpleConnectionPool(1, 3, self.dsn)
        self._ensure_schema()

    def _ensure_schema(self):
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS notified_tenders (
                        id TEXT PRIMARY KEY,
                        source TEXT NOT NULL,
                        title TEXT,
                        matched BOOLEAN,
                        first_seen TIMESTAMPTZ DEFAULT NOW()
                    );
                    """
                )
            conn.commit()

    def get_conn(self):
        # Liveness check: pull a connection, verify it's alive, else reconnect.
        with self._lock:
            conn = self._pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1;")
            except Exception:
                log.warning("DB connection stale, reconnecting pool.")
                self._pool.putconn(conn, close=True)
                self._connect()
                conn = self._pool.getconn()
            return conn

    def release(self, conn):
        with self._lock:
            self._pool.putconn(conn)

    def is_seen(self, tender_id: str) -> bool:
        conn = self.get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM notified_tenders WHERE id = %s;", (tender_id,))
                return cur.fetchone() is not None
        finally:
            self.release(conn)

    def mark_seen(self, tender_id: str, source: str, title: str, matched: bool):
        conn = self.get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO notified_tenders (id, source, title, matched)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING;
                    """,
                    (tender_id, source, title, matched),
                )
            conn.commit()
        finally:
            self.release(conn)


db = Database(Config.DATABASE_URL)

# --------------------------------------------------------------------------
# Groq AI client (via openai package)
# --------------------------------------------------------------------------
ai_client = OpenAI(api_key=Config.GROQ_API_KEY, base_url=Config.GROQ_BASE_URL)

RELEVANCE_SCHEMA_PROMPT = """You are screening Ethiopian government/private procurement
tenders for medMETRIC Healthcare Service PLC, a biomedical engineering and medical
equipment services company (hemodialysis systems incl. B.Braun Dialog+ and SWS-4000A,
RO/water treatment, CSSD & sterilization equipment (Rivamed), calibration & compliance
testing, medical imaging, spare parts sourcing, medical equipment supply & consultancy).

For each tender below, decide if it is genuinely relevant to medMETRIC's business.
Exclude tenders that are purely about: laboratory reagents, pharmaceuticals, vaccines,
medicines/drugs, or generic non-medical consulting/services with no equipment component.
Medical-equipment-focused consultancy IS in scope.

Respond ONLY with a JSON array (no markdown, no preamble), one object per tender, in the
same order given, with this exact shape:
[
  {
    "id": "<tender id as given>",
    "relevant": true/false,
    "reason": "<short reason>",
    "constraints": "<eligibility/bid constraints if mentioned, else empty string>",
    "closing_date": "<closing/submission date if mentioned, else empty string>"
  }
]

Tenders:
"""


def ai_filter_batch(tenders: list) -> dict:
    """
    Given a list of candidate tender dicts (each with id/title/detail_text),
    return a dict of id -> verdict dict from the AI.
    """
    results = {}
    for i in range(0, len(tenders), Config.AI_BATCH_SIZE):
        chunk = tenders[i : i + Config.AI_BATCH_SIZE]
        listing = "\n\n".join(
            f"ID: {t['id']}\nTitle: {t['title']}\n"
            f"Detail: {t['detail_text'][:Config.AI_DETAIL_CHARS]}"
            for t in chunk
        )
        prompt = RELEVANCE_SCHEMA_PROMPT + listing
        try:
            resp = ai_client.chat.completions.create(
                model=Config.GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
            )
            raw = resp.choices[0].message.content.strip()
            raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
            parsed = json.loads(raw)
            for verdict in parsed:
                results[str(verdict.get("id"))] = verdict
        except Exception as e:
            log.error("AI batch failed for chunk starting at %d: %s", i, e)
            # Fail closed for this chunk -- treat as not relevant rather than
            # risk spamming WhatsApp with unverified matches.
            for t in chunk:
                results[str(t["id"])] = {
                    "id": t["id"],
                    "relevant": False,
                    "reason": "AI evaluation failed",
                    "constraints": "",
                    "closing_date": "",
                }
    return results


def is_relevant_tender(title: str, detail_text: str) -> bool:
    """Pre-filter keyword check. DO NOT modify keyword lists without
    explicit instruction -- eGP and 2merkato engines are treated
    independently but share this helper."""
    text = f"{title} {detail_text}".lower()
    if any(term.lower() in text for term in Config.HARD_EXCLUDE_TERMS):
        return False
    return True


# --------------------------------------------------------------------------
# eGP engine -- public JSON API, no browser automation, runs in-process
# --------------------------------------------------------------------------
def scrape_egp() -> list:
    """
    Calls the Angular app's public sourcing API directly. No login needed.
    Runs fully in-process (no multiprocessing/subprocess timeout wrapper --
    that approach previously caused collected candidates to be discarded
    when the subprocess hit its timeout and was killed).
    """
    candidates = []
    now = datetime.now(timezone.utc)
    seen_ids_this_run = set()

    for term in Config.EGP_SEARCH_TERMS:
        try:
            resp = requests.get(
                Config.EGP_SEARCH_ENDPOINT,
                params={"search": term, "top": 25},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning("eGP API call failed for term '%s': %s", term, e)
            continue

        items = data.get("data") or data.get("items") or data if isinstance(data, list) else data.get("data", [])
        if not isinstance(items, list):
            items = []

        for item in items:
            tender_id = str(item.get("id") or item.get("sourcingId") or item.get("referenceNumber") or "")
            if not tender_id or tender_id in seen_ids_this_run:
                continue

            deadline_raw = item.get("submissionDeadline")
            deadline_dt = None
            if deadline_raw:
                try:
                    deadline_dt = datetime.fromisoformat(deadline_raw.replace("Z", "+00:00"))
                except Exception:
                    deadline_dt = None

            if deadline_dt and deadline_dt < now:
                # Skip tenders whose submission deadline has already passed.
                continue

            title = item.get("lotName", "") or item.get("title", "")
            description = item.get("description", "")
            bid_security = item.get("bidSecurity", "")
            procuring_entity = item.get("procuringEntity", "")

            detail_text = (
                f"Title: {title}\n"
                f"Reference: {item.get('referenceNumber', '')}\n"
                f"Procuring Entity: {procuring_entity}\n"
                f"Closing Date: {deadline_raw or 'N/A'}\n"
                f"Bid Security: {bid_security}\n"
                f"Description: {description}"
            )

            if not is_relevant_tender(title, detail_text):
                continue

            seen_ids_this_run.add(tender_id)
            candidates.append(
                {
                    "id": f"egp-{tender_id}",
                    "source": "eGP",
                    "title": title,
                    "detail_text": detail_text,
                    "closing_date": deadline_raw or "",
                    "url": f"{Config.EGP_BASE}/sourcing/{tender_id}",
                }
            )

    log.info("eGP: collected %d candidates", len(candidates))
    return candidates


# --------------------------------------------------------------------------
# 2merkato engine -- Playwright, keyword-driven search, closed-status check
# --------------------------------------------------------------------------
def scrape_2merkato() -> list:
    candidates = []
    seen_ids_this_run = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        try:
            if Config.MERKATO_USERNAME and Config.MERKATO_PASSWORD:
                page.goto(f"{Config.MERKATO_BASE}/login", timeout=30000)
                page.fill("input[name='username']", Config.MERKATO_USERNAME)
                page.fill("input[name='password']", Config.MERKATO_PASSWORD)
                page.click("button[type='submit']")
                page.wait_for_load_state("networkidle")

            for term in Config.MERKATO_SEARCH_TERMS:
                try:
                    search_url = f"{Config.MERKATO_BASE}/search?q={requests.utils.quote(term)}"
                    page.goto(search_url, timeout=30000)
                    page.wait_for_load_state("networkidle")

                    rows = page.query_selector_all("div.tender-item, tr.tender-row")
                    for row in rows:
                        status_badge = row.query_selector(".status-badge, .badge-closed")
                        if status_badge:
                            status_text = (status_badge.inner_text() or "").strip().lower()
                            if "closed" in status_text:
                                continue

                        title_el = row.query_selector("a.tender-title, a")
                        if not title_el:
                            continue
                        title = (title_el.inner_text() or "").strip()
                        href = title_el.get_attribute("href") or ""
                        tender_id = re.sub(r"\W+", "-", href.strip("/"))[:80] or title[:40]

                        if not tender_id or tender_id in seen_ids_this_run:
                            continue

                        detail_el = row.query_selector(".tender-detail, .description")
                        detail_text = (detail_el.inner_text().strip() if detail_el else title)

                        if not is_relevant_tender(title, detail_text):
                            continue

                        seen_ids_this_run.add(tender_id)
                        candidates.append(
                            {
                                "id": f"merkato-{tender_id}",
                                "source": "2merkato",
                                "title": title,
                                "detail_text": detail_text,
                                "closing_date": "",
                                "url": href if href.startswith("http") else f"{Config.MERKATO_BASE}{href}",
                            }
                        )
                except Exception as e:
                    log.warning("2merkato search failed for term '%s': %s", term, e)
                    continue
        finally:
            context.close()
            browser.close()

    log.info("2merkato: collected %d candidates", len(candidates))
    return candidates


# --------------------------------------------------------------------------
# WhatsApp via Evolution API -- one message per matched tender
# --------------------------------------------------------------------------
def send_whatsapp_message(phone_number: str, text: str) -> bool:
    url = f"{Config.EVOLUTION_API_URL}/message/sendText/{Config.EVOLUTION_INSTANCE}"
    headers = {"apikey": Config.EVOLUTION_API_KEY, "Content-Type": "application/json"}
    payload = {"number": phone_number, "text": text}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=20)
        resp.raise_for_status()
        return True
    except Exception as e:
        log.error("WhatsApp send failed for %s: %s", mask_phone(phone_number), e)
        return False


def notify_tender(tender: dict, verdict: dict):
    message = (
        f"\U0001F4E2 *New Tender Match*\n\n"
        f"*Title:* {tender['title']}\n"
        f"*Source:* {tender['source']}\n"
        f"*Closing Date:* {verdict.get('closing_date') or tender.get('closing_date') or 'N/A'}\n"
        f"*Why it matched:* {verdict.get('reason', '')}\n"
        f"*Constraints:* {verdict.get('constraints') or 'N/A'}\n"
        f"*Link:* {tender.get('url', 'N/A')}"
    )
    for phone in Config.NOTIFY_PHONE_NUMBERS:
        send_whatsapp_message(phone, message)


def send_status_report(summary: str):
    for phone in Config.NOTIFY_PHONE_NUMBERS:
        send_whatsapp_message(phone, summary)


# --------------------------------------------------------------------------
# Core scan loop
# --------------------------------------------------------------------------
def run_scan() -> dict:
    log.info("=== Starting scan cycle ===")
    all_candidates = []

    for engine_name, engine_fn in (
        ("eGP", scrape_egp),
        ("2merkato", scrape_2merkato),
    ):
        try:
            all_candidates.extend(engine_fn())
        except Exception as e:
            log.error("%s engine crashed: %s", engine_name, e)

    # Filter out tenders already seen (regardless of prior verdict).
    new_candidates = [t for t in all_candidates if not db.is_seen(t["id"])]
    log.info(
        "Total candidates: %d, new (unseen) candidates: %d",
        len(all_candidates),
        len(new_candidates),
    )

    matched_count = 0
    if new_candidates:
        verdicts = ai_filter_batch(new_candidates)
        for tender in new_candidates:
            verdict = verdicts.get(tender["id"].split("-", 1)[-1]) or verdicts.get(tender["id"]) or {}
            is_match = bool(verdict.get("relevant"))

            # Mark as seen immediately regardless of verdict, to avoid
            # re-spending AI quota re-evaluating the same tender later.
            db.mark_seen(tender["id"], tender["source"], tender["title"], is_match)

            if is_match:
                matched_count += 1
                notify_tender(tender, verdict)

    summary = (
        f"Tender scan complete. {len(all_candidates)} candidates checked, "
        f"{len(new_candidates)} new, {matched_count} matched."
        if matched_count
        else f"Tender scan complete. No new matching tenders "
        f"({len(new_candidates)} new candidates evaluated, none relevant)."
    )
    log.info(summary)
    return {
        "total_candidates": len(all_candidates),
        "new_candidates": len(new_candidates),
        "matched": matched_count,
        "summary": summary,
    }


def scheduler_loop():
    interval_seconds = Config.SCAN_INTERVAL_HOURS * 3600
    while True:
        try:
            run_scan()
        except Exception as e:
            log.error("Scheduled scan failed: %s", e)
        time.sleep(interval_seconds)


# --------------------------------------------------------------------------
# Flask app
# --------------------------------------------------------------------------
app = Flask(__name__)


@app.route("/")
def index():
    return jsonify(
        {
            "service": "Medmetric Tender Notifier",
            "status": "running",
            "scan_interval_hours": Config.SCAN_INTERVAL_HOURS,
        }
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/test-check")
def test_check():
    token = request.args.get("token", "")
    if not Config.TEST_CHECK_TOKEN or not hmac.compare_digest(token, Config.TEST_CHECK_TOKEN):
        return jsonify({"error": "unauthorized"}), 401
    result = run_scan()
    return jsonify(result)


def start_background_scheduler():
    thread = threading.Thread(target=scheduler_loop, daemon=True)
    thread.start()
    log.info("Background scheduler started (interval: %sh)", Config.SCAN_INTERVAL_HOURS)


if __name__ == "__main__":
    Config.validate()
    start_background_scheduler()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
