
## WhatsApp / Evolution API (this build)

- Robust `send_whatsapp()` for Evolution v1 + v2 payloads
- Phone numbers normalized (e.g. `09…` → `2519…`)
- `EVOLUTION_API_KEY` preferred over `GLOBAL_API_KEY`
- `GET/POST /api/whatsapp/test` to verify Evolution wiring
- Health reports `whatsapp_configured`, recipient count, instance name

# Tender notifier v2

## Playwright browser/runtime alignment (2026-10-26)
- Fixed the 2merkato startup error where Python Playwright 1.64.1 expected a Chromium revision absent from the old `v1.48.0-jammy` container.
- Docker base image and exact `playwright==1.64.1` package pin now match.
- Render Blueprint now uses the repository Dockerfile so it cannot silently follow a different native-Python browser installation path.
- After pushing this change, trigger a fresh Render deploy; restarting the existing old image is not sufficient.

Based on your uploaded notifier + the earlier fixes (404 on unknown ids, no phantom rows, no fake DOCS_READY stub).

## 2merkato
- Logs whether MERKATO_USER / MERKATO_PASS are set, and verifies the login (login_ok).
- Hard time budget (MERKATO_TIME_BUDGET_SEC, default 420) so it can't hang the scan.
- Fallback crawls `/tenders?page=N`, not just page 1.
- Counters per stage (raw links, title-relevant, closed, already notified, candidates) -> `GET /api/scan-report`.
- I could not reach tender.2merkato.com from my sandbox: selectors are UNVERIFIED. Run a scan, read `/api/scan-report`
  and the Render logs; they show which stage returns zero.

## Approved tenders
- A re-scan can no longer revert APPROVED/REJECTED/DOCS_* back to PENDING_APPROVAL.
- Drafts are backed up in Postgres (`POST /api/tenders/<id>/drafts`) and returned by `GET /api/tenders`.
- DB helpers now commit/rollback, so one failed query no longer poisons the shared connection.
- Render free Postgres is deleted after 30 days — the most likely way "everything disappears".

## Other
- `POST /api/scan` + `GET /api/scan-report` (scan lock, no overlapping scans).
- HARD_EXCLUDE_TERMS had a missing comma ("road" "laboratory" were fused). Now "road construction"; "laboratory" is still NOT excluded (same effective behaviour as before).
