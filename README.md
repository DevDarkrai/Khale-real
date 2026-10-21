# KAHLE Psych Kompass
**Dr. Kai A.D. Morgan-Campbell · Kai Morgan & Associates · Jamaica**
Ethics Ref: MOH-JA-2025-0047

---

## Quick Start (Windows PowerShell)

> **Important:** `static/index.html` is a thin client — it has no data of its own
> and reads everything live from the Python server. Keep the server running the
> whole time you use the app.

### Step 1 — Set up your API key

```powershell
cd ded-project
Copy-Item .env.example .env
notepad .env
```

Set `ANTHROPIC_API_KEY=sk-ant-your-key-here`, then save and close.

### Step 2 — Generate data and start the server

```powershell
python start.py --generate 10000 --reset
```

`--reset` wipes the existing database and generates a fresh 10,000-record dataset.
Omit `--reset` to top up an existing database without losing existing records.

### Step 3 — Open the frontend

```powershell
Start-Process "static\index.html"
```

Keep the PowerShell window open — closing it stops the server.

---

## Demo Accounts

After `--reset`, the following accounts are available:

| Username | Password | Role | Access |
|---|---|---|---|
| `dr.morgan` | `KADMorgan2025!` | PI | **All sites · all 10,000 records** |
| `v.billett` | `Verol2025!` | Researcher | **All sites · all records** |
| `clinic1` | `Clinic1_2025!` | Clinician | Kingston Clinic only (SITE-01) |
| `clinic2` | `Clinic2_2025!` | Clinician | Portmore Clinic only (SITE-02) |
| `clinic3` | `Clinic3_2025!` | Clinician | Spanish Town Clinic only (SITE-03) |
| `school1` | `School1_2025!` | Teacher | Mona Primary School only (SITE-04) |
| `school2` | `School2_2025!` | Teacher | Portmore High School only (SITE-05) |
| `school3` | `School3_2025!` | Teacher | Portland Deaf School only (SITE-06) |

**Site-scoped accounts** (clinic1–3, school1–3) see only their own records.
Admins (dr.morgan, v.billett) see all records across all sites.

---

## Architecture

```
ded-project/
├── start.py                  ← Entry point
├── .env.example              → copy to .env, add API key
├── requirements.txt
├── README.md
├── app/
│   └── server.py             ← Python REST API (~1,500 lines)
├── db/
│   ├── schema.sql            ← 13 tables, WAL mode, 18 indexes
│   └── ded_platform.db       ← auto-created on first run
├── scripts/
│   └── generate_data.py      ← Synthetic data generator (default: 10,000)
├── static/
│   └── index.html            ← Server-connected frontend
├── logs/server.log
└── exports/
```

**Backend:** Pure Python stdlib · SQLite WAL mode, 64MB cache · PBKDF2-HMAC-SHA256 auth
· HMAC-signed sessions (8hr TTL) · Rate limiting (200 req/60s/IP) · Immutable audit log

**Site isolation:** Every data query (dashboard, records, analytics, AI context, CSV export)
is automatically scoped to the logged-in user's site. Admins bypass all filters.
Cross-site leakage: zero — verified by 20 automated integration tests.

**Claude AI:** Server-side via `/api/ai/chat`. Context builder detects query keywords,
runs targeted SQL (including cross-tabulations), embeds real DB data into Claude's context.
All queries are site-scoped. AI chat also explains which site it is reading from.

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| POST | /api/auth/login | Login → returns token + site info |
| POST | /api/auth/logout | Invalidate session |
| GET | /api/auth/me | Current user info |
| GET | /api/health | Server + DB health check |
| GET | /api/dashboard/stats | Dashboard counts, kappa, domain breakdown |
| GET | /api/records | Paginated records list (site-scoped) |
| POST | /api/records | Create new record |
| GET | /api/records/:id | Single record detail |
| POST | /api/screenings | Submit screener responses |
| POST | /api/screenings/:id/validate | Clinician validation |
| GET | /api/analytics/kappa | Cohen's kappa analysis |
| GET | /api/analytics/bias | Bias monitoring stats |
| GET | /api/analytics/psychometrics | Domain score statistics |
| POST | /api/ai/chat | Claude AI with live DB context |
| GET | /api/ai/conversations | Conversation history |
| GET | /api/phases | Phase task tracker |
| PATCH | /api/phases/tasks/:id | Update task status |
| GET | /api/consents | Consent records |
| POST | /api/consents | Create consent record |
| PATCH | /api/consents/:id/sign | Sign consent |
| GET | /api/referrals | Referral list |
| GET | /api/export/csv | De-identified CSV export (site-scoped) |
| GET | /api/audit-log | Immutable audit trail |

---

## .env Configuration

```env
HOST=0.0.0.0
PORT=8000
JWT_SECRET=<change-in-production>
ANTHROPIC_API_KEY=sk-ant-your-key-here
CLAUDE_MODEL=claude-sonnet-4-6
CLAUDE_MAX_TOKENS=2048
DB_PATH=          # blank = default db/ded_platform.db
LOG_LEVEL=INFO
```

---

## Research Context

4-phase 24-month study. Phase 1 (archival) and Phase 2 (AI model + retrospective
validation) are in progress. Phase 3 (prospective pilot) requires **separate ethics
approval** — NOT covered by MOH-JA-2025-0047. Instruments: BASC-3, ADHDT-2,
CPT-3/CATA, GARS-3, ADOS-2, Quick & Dirty Screener. All outputs are non-diagnostic
risk indicators (Low/Moderate/High) only.

---

## Data Generation

`python start.py --generate 10000 --reset`

- 10,000 records total
- 80% archival (Phase 1, no site_code — visible to all)
- 20% pilot (Phase 2, randomly distributed across 6 demo sites)
- Weighted parish distribution: KSA 32%, St. Catherine 27%, rural 36%
- Age peak: 9–11 (36%), 12–14 (31%)
- ADHD most common domain (31%)
- ~72% clinician validation rate (κ ≈ 0.68–0.74)
- Generation time: ~3s · DB size: ~27MB
