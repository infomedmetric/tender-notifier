# Tender Approval Workflow (Phase 1)

Updated: 2026-10-16

## What changed

Human-in-the-loop approval replaces auto WhatsApp full alerts.

### Flow

1. Scrapers (2merkato + eGP) run as before
2. Keyword filter + AI relevance analysis
3. Relevant tenders saved as **`PENDING_APPROVAL`**
4. Short WhatsApp only: “N new tender(s) waiting for your approval in the app”
5. Mobile app lists tenders → user **Approve** or **Reject**
6. On **Approve** → status `APPROVED` → document preparation starts (stub for now → later real Technical + Financial .docx/.pdf)
7. Status becomes `DOCS_READY` when drafts are ready

### Statuses

| Status | Meaning |
|--------|---------|
| PENDING_APPROVAL | AI matched — waiting for you |
| APPROVED | You approved — docs being prepared |
| REJECTED | You rejected |
| DOCS_READY | Technical + Financial drafts ready |
| DOCS_FAILED | Document prep failed |
| REVIEWING / WON / LOST / IGNORED | Existing workflow |

### API (auth: `X-API-KEY` or `Authorization: Bearer …`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/tenders?status=PENDING_APPROVAL` | List pending (comma-separated statuses ok) |
| GET | `/api/tenders/<id>` | Single tender detail |
| POST | `/api/tenders/<id>/approve` | Approve → starts doc prep |
| POST | `/api/tenders/<id>/reject` | Reject |
| PATCH/POST | `/api/tenders/<id>/status` | Body `{"status":"APPROVED"}` etc. |
| GET | `/api/health` | Health + lists allowed statuses |

### Deploy

Replace the existing `tender_notifier.py` on Render (or your host) with this file.
No new required env vars. Optional:

- `NOTIFY_EMPTY_SCANS=1` — WhatsApp also when scan finds nothing

### Next (Phase 2)

- Real Technical + Financial document generation (Word + PDF) using your sample proposals
- Mobile UI: Approve / Reject buttons + open tender link
- Email monitoring (cPanel / Gmail IMAP)
- Letter writing on behalf of the company
