# Render environment variables — medMETRIC Tender Agent

Set these in **Render Dashboard → your Web Service → Environment**.

## Required

| Variable | Example | Purpose |
|----------|---------|---------|
| `DATABASE_URL` | *(from Render Postgres)* | Stores tenders + statuses |
| `GROQ_API_KEY` | `gsk_...` | AI relevance scoring |
| `MOBILE_API_KEY` | long random string | App uses this as `X-API-KEY` |
| `TEST_CHECK_TOKEN` | random string | Manual scan: `/test-check?token=...` |

## Recommended

| Variable | Example | Purpose |
|----------|---------|---------|
| `GROQ_MODEL` | `llama-3.1-8b-instant` | Groq model id |
| `GROQ_API_KEY_EGP` | second `gsk_...` | Extra quota for eGP batch (optional) |
| `SCAN_INTERVAL_HOURS` | `6` | Background scan interval |
| `MERKATO_USER` | your 2merkato login | Playwright login |
| `MERKATO_PASS` | password | Playwright login |
| `EGP_USER` | eGP portal user | Optional portal login |
| `EGP_PASS` | eGP password | Optional portal login |
| `EGP_BASE` | `https://production.egp.gov.et` | eGP host |

## Optional WhatsApp (short notify only)

| Variable | Purpose |
|----------|---------|
| `EVOLUTION_BASE` | Evolution API base URL |
| `INSTANCE_NAME` | Instance name |
| `GLOBAL_API_KEY` | Evolution / fallback API key |
| `WHATSAPP_NUMBERS` | Comma-separated numbers |

## Optional tuning

| Variable | Default | Purpose |
|----------|---------|---------|
| `GROQ_TEMPERATURE` | `0.2` | AI temperature |
| `GROQ_MAX_TOKENS` | `2048` | Max completion tokens |
| `AI_CHUNK_SIZE` | `3` | Batch size for AI |
| `MERKATO_MAX_PAGES` | `4` | 2merkato pages |
| `MERKATO_MAX_PAGES_PER_TERM` | `3` | Pages per search term |

## Mobile app settings (not Render)

On the phone **Settings**:

| Field | Example |
|-------|---------|
| Tender API URL | `https://YOUR-SERVICE.onrender.com` |
| Tender API key | same as `MOBILE_API_KEY` |

## API used by the app

```
GET  /api/health
POST /api/scan          ← mobile "Sync & scan open tenders"
GET  /api/tenders?status=PENDING_APPROVAL,APPROVED,...
POST /api/tenders/<id>/approve
POST /api/tenders/<id>/reject
```

Scan results are written to Postgres (`tenders` table, status `PENDING_APPROVAL`).
Scan progress is shared across workers via the `scan_status` table.


Header: `X-API-KEY: <MOBILE_API_KEY>`

## Flow

1. Render Playwright + eGP scrape → AI → save as `PENDING_APPROVAL` in Postgres  
2. App **Pull from cloud** → copies into phone SQLite → you Approve / Reject  
3. App posts Approve/Reject to Render → Postgres updated; Approve can start doc prep  

## Deploy

Deploy on Render with **Docker** using the repository `Dockerfile` (also configured in `render.yaml`). The Docker base image and Python dependency are both pinned to Playwright `1.64.1`; the browser executable must match the Python package version. Do not use an old Playwright Docker image with an unconstrained or newer pip package.

After pushing a Dockerfile or requirements change, choose **Manual Deploy → Deploy latest commit** and wait for the build to finish before running a scan. A restart of the previous deployment keeps its old image.


## Scan lock / stuck "already running"

| Variable | Default | Purpose |
|----------|---------|---------|
| `STALE_SCAN_MINUTES` | `25` | Auto-clear `running` lock older than this |
| `SCAN_RECENT_COOLDOWN_MINUTES` | `0` | Block re-scan for N minutes after success (`0` = always allow) |

Immediate unlock without redeploy:

```bash
curl -X POST "https://YOUR-SERVICE.onrender.com/api/scan/reset" \
  -H "X-API-KEY: YOUR_MOBILE_API_KEY"
```

Force start even if lock is fresh:

```bash
curl -X POST "https://YOUR-SERVICE.onrender.com/api/scan?force=1" \
  -H "X-API-KEY: YOUR_MOBILE_API_KEY"
```

Each `/api/scan` runs **both** eGP and 2merkato engines.
