# medMETRIC Tender Agent

AI-assisted tender monitor for **medMETRIC Healthcare Service PLC**.

- Scrapes **2merkato** (Playwright) and **eGP** (`production.egp.gov.et` API)
- AI relevance scoring (Groq)
- Human-in-the-loop: tenders saved as `PENDING_APPROVAL`
- Mobile app lists tenders → you **Approve** / **Reject**
- On Approve → document preparation starts (Technical + Financial drafts)
- Short WhatsApp notification only (no auto full alerts)

---

## Deploy on Render (new service + database)

### 1. Push this folder to GitHub

```bash
cd tender-notifier
git init
git add .
git commit -m "medMETRIC tender agent – approval workflow"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/medmetric-tender-agent.git
git push -u origin main
```

### 2. Create Postgres on Render

1. Render Dashboard → **New** → **PostgreSQL**
2. Name: `medmetric-tender-db`
3. Plan: Free
4. Create → copy the **Internal Database URL** (or External if needed)

### 3. Create Web Service

1. **New** → **Web Service** → connect the GitHub repo
2. Settings:
   - **Name:** `medmetric-tender-agent`
   - **Runtime:** Docker, using the repository's `Dockerfile`.
   - Do not override Docker build/start commands. Dockerfile and requirements both pin Playwright `1.64.1`; keep these versions identical. The official Playwright container supplies its matching Chromium build.
3. Add environment variables (see table below)
4. Link the Postgres database (Render auto-sets `DATABASE_URL` if you connect it)

### 4. Required environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | Postgres connection string (auto if linked) |
| `GROQ_API_KEY` | Yes | Groq API key for AI analysis |
| `GROQ_API_KEY_EGP` | No | Separate Groq key for eGP quota (falls back to main) |
| `MOBILE_API_KEY` | Yes | API key for mobile app (`X-API-KEY` header) |
| `GLOBAL_API_KEY` | For WhatsApp | Evolution API key |
| `WHATSAPP_NUMBERS` | For WhatsApp | Comma-separated numbers, e.g. `2519xxxxxxx` |
| `EVOLUTION_BASE` | For WhatsApp | e.g. `https://medmetric-evolution.onrender.com` |
| `INSTANCE_NAME` | For WhatsApp | e.g. `Tender-Notifier.` |
| `TEST_CHECK_TOKEN` | Recommended | Secret for `/test-check?token=...` manual scan |
| `SCAN_INTERVAL_HOURS` | No | Default `6` |
| `MERKATO_USER` / `MERKATO_PASS` | Optional | 2merkato login |
| `EGP_USER` / `EGP_PASS` | Optional | eGP credentials (API path does not always need them) |
| `NOTIFY_EMPTY_SCANS` | No | `1` = WhatsApp even when no matches |

### 5. After deploy

- Health: `https://YOUR-SERVICE.onrender.com/api/health`
- After changing the Dockerfile, requirements, or Render runtime, commit and push the changes, then choose **Render → Manual Deploy → Deploy latest commit**. Wait for deployment to finish before triggering a scan. Restarting the old deployment does not replace its container image.
- Manual scan: `https://YOUR-SERVICE.onrender.com/test-check?token=YOUR_TEST_CHECK_TOKEN`
- List pending:  
  `GET /api/tenders?status=PENDING_APPROVAL`  
  Header: `X-API-KEY: your_mobile_api_key`

---

## API (mobile / agent)

All mobile routes require:

```
X-API-KEY: <MOBILE_API_KEY>
```

or

```
Authorization: Bearer <MOBILE_API_KEY>
```

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/health` | Health + allowed statuses |
| GET | `/api/tenders?status=PENDING_APPROVAL` | List tenders |
| GET | `/api/tenders/<id>` | One tender |
| POST | `/api/tenders/<id>/approve` | Approve → starts doc prep |
| POST | `/api/tenders/<id>/reject` | Reject |
| PATCH | `/api/tenders/<id>/status` | Body `{"status":"APPROVED"}` |

### Statuses

`PENDING_APPROVAL` → `APPROVED` / `REJECTED` → `DOCS_READY` / `DOCS_FAILED`  
Also: `REVIEWING`, `WON`, `LOST`, `IGNORED`, `NEW`

---

## Local run (optional)

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
export DATABASE_URL=...
export GROQ_API_KEY=...
export MOBILE_API_KEY=dev-key
python tender_notifier.py
```

---

## Project files

```
tender-notifier/
├── tender_notifier.py   # Main Flask app + scrapers + AI + approval API
├── requirements.txt
├── Procfile
├── runtime.txt
├── build.sh
├── render.yaml          # Optional Blueprint
├── .gitignore
├── APPROVAL_WORKFLOW.md
└── README.md
```

---

## Next phases (after this is live)

1. Mobile UI: Approve / Reject buttons  
2. Real Technical + Financial document generation (Word + PDF)  
3. Email monitoring (cPanel / Gmail IMAP)  
4. Official letter writing on behalf of the company  
