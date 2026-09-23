# Bidvest ESG

Data capture and reporting tool for Bidvest Catering Services' ESG checklist
(74 data points across Environmental, Social and Governance pillars, seeded
from the Group's checklist — see `checklist_items.json`).

Built on the same core principles as Waste Smart: a single always-current
capture record per data point (status / value / period / notes / owner),
a full history log of every change, evidence files uploaded straight against
the record they support, and a live summary dashboard mirroring the original
spreadsheet's collection-progress and workload-by-frequency views. Every one
of the 74 requirements goes through the identical capture-and-upload flow —
there's no special case per data point.

## Local run

```
pip install -r requirements.txt
python3 app.py            # dev server on :5000
```

or production-style:

```
gunicorn -w 2 -b 0.0.0.0:8420 --timeout 30 app:app
```

## Logins

Two shared passwords (set via env vars, defaults below for local testing):

- `APP_PASSWORD_TEAM` (default `bidvest-esg-team`) — capture/upload access.
- `APP_PASSWORD_ADMIN` (default `bidvest-esg-admin`) — capture/upload plus
  export and evidence deletion.

Each person types their own name at login so every change and upload is
attributed (`updated_by` / `uploaded_by`), without needing individual accounts.

## Data

SQLite at `data/bidvest_esg.db`, evidence files under `data/uploads/<item_id>/`.
On Render this needs the attached persistent disk mounted at `/app/data`
(already in `render.yaml`) — without it, data resets on every restart/redeploy.

## API

- `POST /api/login`, `POST /api/logout`, `GET /api/session`
- `GET /api/checklist` — all 74 items + current capture state + evidence count
- `POST /api/capture` — upsert status/value/period/notes/owner for one item
- `GET /api/history/<item_id>` — change history
- `GET /api/evidence/<item_id>`, `POST /api/upload`, `GET /api/download/<id>`,
  `DELETE /api/evidence/<id>` (admin)
- `GET /api/summary` — pillar/status and frequency rollups
- `GET /api/export/xlsx` — full checklist + summary as a downloadable workbook
- `GET /health`
