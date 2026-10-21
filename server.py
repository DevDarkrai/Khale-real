#!/usr/bin/env python3
"""
KAHLE Psych Kompass · Backend Server v3.0
Dr. KAD Morgan & Associates · Jamaica · MOH-JA-2025-0047

Features:
- SQLite database optimised for 5,000+ records
- Claude AI integration that reads REAL database records to answer questions
- Full REST API (24 endpoints)
- PBKDF2 auth, HMAC sessions, rate limiting, immutable audit log

Usage:
    python app/server.py
    JWT_SECRET=mysecret ANTHROPIC_API_KEY=sk-ant-... python app/server.py
"""

import http.server
import json
import sqlite3
import os
import sys
import hashlib
import hmac
import base64
import uuid
import time
import datetime
import re
import csv
import io
import logging
import threading
import urllib.request
import urllib.error
from urllib.parse import urlparse, parse_qs
from pathlib import Path

# ── CONFIG ────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent.parent
_db_path_env = os.environ.get("DB_PATH", "").strip()
DB_PATH     = Path(_db_path_env) if _db_path_env else (BASE_DIR / "db" / "ded_platform.db")
SCHEMA_PATH = BASE_DIR / "db" / "schema.sql"
LOG_DIR     = BASE_DIR / "logs"
EXPORT_DIR  = BASE_DIR / "exports"
PORT        = int(os.environ.get("PORT", 8000))
HOST        = os.environ.get("HOST", "0.0.0.0")

JWT_SECRET      = os.environ.get("JWT_SECRET", "kadm-ded-change-this-in-prod")
SESSION_TTL     = 60 * 60 * 8      # 8 hours
RATE_LIMITS     = {}
RATE_WINDOW     = 60
RATE_MAX        = 200               # higher limit for larger dataset

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL      = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
CLAUDE_MAX_TOKENS = int(os.environ.get("CLAUDE_MAX_TOKENS", 2048))

LOG_DIR.mkdir(parents=True, exist_ok=True)
EXPORT_DIR.mkdir(parents=True, exist_ok=True)

# ── LOAD .env if present (checks project root AND app/ subfolder) ──
_env_candidates = [
    BASE_DIR / ".env",
    Path(__file__).parent / ".env",
    Path.cwd() / ".env",
]
env_file = None
for _c in _env_candidates:
    if _c.exists():
        env_file = _c
        break

print(f"[config] BASE_DIR={BASE_DIR}")
print(f"[config] .env search: {[str(c) for c in _env_candidates]}")
print(f"[config] .env found: {env_file}")

if env_file:
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key == "ANTHROPIC_API_KEY":
                ANTHROPIC_API_KEY = val  # always override from .env
            elif key == "JWT_SECRET":
                JWT_SECRET = val
            elif key == "CLAUDE_MODEL":
                CLAUDE_MODEL = val
            elif key == "PORT":
                PORT = int(val)

print(f"[config] ANTHROPIC_API_KEY={'SET ('+ANTHROPIC_API_KEY[:8]+'...)' if ANTHROPIC_API_KEY else 'NOT SET'}")

# ── LOGGING ───────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO")),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "server.log", encoding="utf-8"),
    ]
)
log = logging.getLogger("ded")

# ── DATABASE ──────────────────────────────────────────────────────
_local = threading.local()

def get_db():
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
        _local.conn.execute("PRAGMA cache_size=-64000")
        _local.conn.execute("PRAGMA temp_store=MEMORY")
    return _local.conn

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # ── Schema migration: add missing columns BEFORE running schema script ──
    # This ensures indexes on new columns don't fail on existing databases.
    migrations = [
        ("records",    "site_code",           "TEXT"),
        ("records",    "primary_domain",      "TEXT"),
        ("records",    "secondary_domain",    "TEXT"),
        ("records",    "domain_tags",         "TEXT DEFAULT '[]'"),
        ("records",    "gender_code",         "TEXT"),
        ("records",    "referral_reason",     "TEXT"),
        ("records",    "school_type",         "TEXT"),
        ("records",    "year_of_assessment",  "INTEGER"),
        ("records",    "notes",               "TEXT"),
        ("records",    "updated_at",          "DATETIME"),
        ("records",    "created_by",          "TEXT"),
        ("records",    "consent_obtained",    "INTEGER DEFAULT 0"),
        ("users",      "site_code",           "TEXT"),
        ("users",      "site_name",           "TEXT"),
        ("users",      "preferences",         "TEXT DEFAULT '{}'"),
        ("screenings", "referral_text",       "TEXT"),
        ("screenings", "ai_flag",              "INTEGER DEFAULT 0"),
        ("screenings", "duration_seconds",     "INTEGER"),
        ("screenings", "is_complete",          "INTEGER DEFAULT 1"),
        ("screenings", "core_score",           "REAL"),
        ("screenings", "modules_triggered",    "TEXT DEFAULT '[]'"),
        ("screenings", "modules_completed",    "TEXT DEFAULT '[]'"),
        ("screenings", "per_module_scores",    "TEXT DEFAULT '{}'"),
        ("screenings", "not_observed_count",   "INTEGER DEFAULT 0"),
        ("screenings", "safety_flags",         "TEXT DEFAULT '[]'"),
        ("screenings", "safety_alert",         "INTEGER DEFAULT 0"),
        ("screenings", "age_band_at_screening","TEXT"),
    ]
    for table, col, col_type in migrations:
        try:
            existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if existing and col not in existing:   # only if table exists
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
                log.info("Migration: added %s.%s", table, col)
        except Exception as e:
            log.warning("Migration skipped %s.%s: %s", table, col, e)
    conn.commit()

    # Now safe to run full schema (CREATE TABLE IF NOT EXISTS + indexes)
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        conn.executescript(f.read())

    row = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()
    if row["c"] == 0:
        _seed_users(conn)
    conn.commit()
    conn.close()
    log.info("Database ready at %s", DB_PATH)

def _seed_users(conn):
    # Admin / PI accounts — site_code=NULL means they see ALL records
    admins = [
        (str(uuid.uuid4()), "dr.morgan", "pi",         _hash_pw("KADMorgan2025!"), "Dr. Kai A.D. Morgan-Campbell", None, None),
        (str(uuid.uuid4()), "v.billett", "researcher", _hash_pw("Verol2025!"),     "Mr. Verol Billett",            None, None),
        (str(uuid.uuid4()), "dr.ho",     "researcher", _hash_pw("Djavila2025!"),   "Dr. Djavila Ho",               None, None),
    ]
    # Site accounts — each sees ONLY their own site_code records
    sites = [
        (str(uuid.uuid4()), "clinic1",   "clinician",  _hash_pw("Clinic1_2025!"),  "Dr. Sean Thorpe",              "SITE-01", "Kingston Clinic"),
        (str(uuid.uuid4()), "clinic2",   "clinician",  _hash_pw("Clinic2_2025!"),  "Dr. Yolanda Brown",            "SITE-02", "Portmore Clinic"),
        (str(uuid.uuid4()), "clinic3",   "clinician",  _hash_pw("Clinic3_2025!"),  "Dr. Marcus Reid",              "SITE-03", "Spanish Town Clinic"),
        (str(uuid.uuid4()), "school1",   "teacher",    _hash_pw("School1_2025!"),  "Ms. Andrea Campbell",          "SITE-04", "Mona Primary School"),
        (str(uuid.uuid4()), "school2",   "teacher",    _hash_pw("School2_2025!"),  "Mr. Devon Clarke",             "SITE-05", "Portmore High School"),
        (str(uuid.uuid4()), "school3",   "teacher",    _hash_pw("School3_2025!"),  "Ms. Tamika Thompson",          "SITE-06", "Portland Deaf School"),
    ]
    conn.executemany(
        "INSERT INTO users(id,username,role,password_hash,full_name,site_code,site_name) VALUES(?,?,?,?,?,?,?)",
        admins + sites
    )
    tasks = [
        ("pt-1",  1, "Ethics approval received (MOH-JA-2025-0047)",        "complete"),
        ("pt-2",  1, "De-identification pipeline established",              "complete"),
        ("pt-3",  1, "Archival dataset coded (n=10,000+ records)",          "complete"),
        ("pt-4",  1, "Multivariate logistic regression analysis",           "in_progress"),
        ("pt-5",  1, "Item extraction and Q&D Screener revision",           "pending"),
        ("pt-6",  1, "Salvation Army School for the Blind comparison",      "pending"),
        ("pt-7",  2, "Prototype digital screener deployed",                 "complete"),
        ("pt-8",  2, "Cloud deployment — no PII stored",                   "complete"),
        ("pt-9",  2, "Retrospective validation (kappa ≥0.60)",              "in_progress"),
        ("pt-10", 2, "Independent clinician discrepancy review",            "pending"),
        ("pt-11", 2, "Bias audit: rural/sensory subgroups",                 "pending"),
        ("pt-12", 3, "Separate ethics application to MOH",                  "pending"),
        ("pt-13", 3, "MOESYI institutional approval (7 schools)",           "pending"),
        ("pt-14", 3, "Teacher and parental consent procedures",             "pending"),
        ("pt-15", 3, "JSL-adapted screener for deaf-inclusive sites",       "pending"),
        ("pt-16", 3, "6-week teacher pilot + usability surveys",            "pending"),
        ("pt-17", 4, "Broader dissemination and publication",               "pending"),
    ]
    conn.executemany(
        "INSERT INTO phase_tasks(id,phase_number,task_name,status) VALUES(?,?,?,?)",
        tasks
    )
    log.info("Seeded users (%d admin, %d site) and phase tasks",
             len(admins), len(sites))

# ── AUTH ──────────────────────────────────────────────────────────
def _hash_pw(pw: str) -> str:
    salt = os.urandom(16)
    dk   = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
    return base64.b64encode(salt + dk).decode()

def _verify_pw(pw: str, stored: str) -> bool:
    try:
        raw  = base64.b64decode(stored.encode())
        salt, dk = raw[:16], raw[16:]
        return hmac.compare_digest(dk, hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000))
    except Exception:
        return False

def _make_token(user_id: str) -> str:
    payload = f"{user_id}:{int(time.time())}:{uuid.uuid4()}"
    sig     = hmac.new(JWT_SECRET.encode(), payload.encode(), "sha256").hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode()

def _verify_token(token: str):
    try:
        raw     = base64.urlsafe_b64decode(token.encode()).decode()
        *parts, sig = raw.rsplit(":", 1)
        payload = ":".join(parts)
        expected = hmac.new(JWT_SECRET.encode(), payload.encode(), "sha256").hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        db  = get_db()
        row = db.execute(
            "SELECT user_id FROM sessions WHERE token=? AND expires_at > datetime('now')",
            (token,)
        ).fetchone()
        return row["user_id"] if row else None
    except Exception:
        return None

# ── RATE LIMITER ──────────────────────────────────────────────────
_rate_lock = threading.Lock()

def _check_rate(ip: str) -> bool:
    now = time.time()
    with _rate_lock:
        ts = [t for t in RATE_LIMITS.get(ip, []) if now - t < RATE_WINDOW]
        if len(ts) >= RATE_MAX:
            return False
        ts.append(now)
        RATE_LIMITS[ip] = ts
    return True

# ── HELPERS ───────────────────────────────────────────────────────
def ok(data=None, status=200, message="ok"):
    return status, {"status": "success", "message": message,
                    "data": data, "timestamp": datetime.datetime.utcnow().isoformat() + "Z"}

def err(message, status=400, details=None):
    return status, {"status": "error", "message": message,
                    "details": details, "timestamp": datetime.datetime.utcnow().isoformat() + "Z"}

def rows_to_list(rows):
    return [dict(r) for r in rows]

def audit(user_id, action, resource_type=None, resource_id=None, ip=None, details=None):
    try:
        db = get_db()
        db.execute(
            "INSERT INTO audit_log(user_id,action,resource_type,resource_id,ip_address,details) VALUES(?,?,?,?,?,?)",
            (user_id, action, resource_type, resource_id, ip, json.dumps(details) if details else None)
        )
        db.commit()
    except Exception as e:
        log.warning("Audit write failed: %s", e)

def _get_user_site(user_id: str, db) -> str | None:
    """
    Returns the site_code for a given user, or None if they are
    an admin/PI who can see all sites.
    """
    row = db.execute(
        "SELECT role, site_code FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if not row:
        return None
    # Admins and PIs see everything — no site filter
    if row["role"] in ("pi", "admin", "researcher"):
        return "ALL"
    return row["site_code"]  # e.g. "SITE-01" or None


def _site_filter(user_id: str, db, table_alias: str = "") -> tuple:
    """
    Returns (extra_where_clause, extra_args) to append to any query
    so site-scoped users only see their own records.

    table_alias: prefix for the site_code column, e.g. "r" → "r.site_code"
                 Leave empty ("") for simple single-table queries without an alias.

    Usage (with alias):
        sf, sf_args = _site_filter(uid, db, "r")
        db.execute(f"SELECT ... FROM records r WHERE r.is_deleted=0{sf}", sf_args)

    Usage (no alias):
        sf, sf_args = _site_filter(uid, db)
        db.execute(f"SELECT ... FROM records WHERE is_deleted=0{sf}", sf_args)
    """
    site = _get_user_site(user_id, db)
    if site == "ALL" or site is None:
        return "", []
    col = f"{table_alias}.site_code" if table_alias else "site_code"
    return f" AND {col}=?", [site]


def _compute_kappa(pairs):
    levels = ["low", "moderate", "high"]
    n = len(pairs)
    if n == 0:
        return 0.0
    matrix = {a: {b: 0 for b in levels} for a in levels}
    for row in pairs:
        a, b = row["ai_risk"], row["cli_risk"]
        if a in levels and b in levels:
            matrix[a][b] += 1
    po = sum(matrix[l][l] for l in levels) / n
    pe = sum(
        (sum(matrix[l][x] for x in levels) / n) * (sum(matrix[x][l] for x in levels) / n)
        for l in levels
    )
    return round((po - pe) / (1 - pe), 4) if pe < 1 else 1.0

# ══════════════════════════════════════════════════════════════════
# CLAUDE AI INTEGRATION
# The core feature: Claude reads real database records to answer questions
# ══════════════════════════════════════════════════════════════════

CLAUDE_SYSTEM_PROMPT = """You are KAHLE Psych Kompass AI — the research intelligence assistant for the KAHLE Psych Kompass at Dr. KAD Morgan & Associates, Jamaica. Ethics Ref: MOH-JA-2025-0047.

You have access to a LIVE DATABASE containing real de-identified research records from the Jamaican youth mental health screening study. When answering questions, you receive actual data pulled from the database in each message — use it to give specific, data-driven answers.

YOUR CAPABILITIES:
- Analyse patterns across thousands of de-identified screening records
- Compute and explain statistics (kappa, risk distributions, domain scores)
- Identify trends by parish, age band, setting, and time period  
- Compare subgroups (rural vs urban, deaf-inclusive vs mainstream)
- Answer questions about specific records using their Study IDs
- Provide culturally contextualised interpretation of Jamaican youth data
- Flag data quality issues, bias risks, and representation gaps

CRITICAL RULES:
1. You NEVER make diagnoses. All outputs are "risk indicators" only.
2. You ALWAYS contextualise within Jamaican cultural norms and the WEIRD bias literature.
3. You ALWAYS acknowledge the non-diagnostic framing of the screener.
4. You flag when patterns may reflect sampling bias (referral-based, not population-based).
5. Child safety: any disclosed abuse risk → direct to CPFSA: 1-888-PROTECT.
6. You are warm, precise, and speak to both clinicians and researchers.

CULTURAL CONTEXT (always apply):
- PEP (Primary Exit Profile) and CSEC/CAPE exam pressure drives many 9-14 referrals
- Jamaican Patois influences how symptoms present and are reported by teachers/parents
- Church and family community structures affect disclosure and help-seeking
- Rural parishes (Portland, St. Elizabeth, St. Ann, Westmoreland) are underrepresented
- WEIRD (Western, Educated, Industrialized, Rich, Democratic) bias is documented in all instruments used

INSTRUMENT: KAHLE PSYCH KOMPASS v1.0 (Parent/Caregiver form)
Two-tier architecture: Kompass Core (50 universal items) → conditional follow-up modules.
Age bands: 4-7 (early childhood) · 8-12 (middle childhood) · 13-17 (adolescence)
Scale: 0=Never · 1=A little · 2=Sometimes · 3=Often · ?=Not observed (not scored)

FOLLOW-UP MODULES (17 total, opened by Core endorsements ≥2):
DEV  — Adaptive, Language & Motor Development (Core 1–3)
AI   — ADHD Inattentive (Core 4–5)
AH   — ADHD Hyperactive-Impulsive (Core 6–7)
AS   — Autism Social Communication (Core 2, 8–9)
AR   — Autism Repetitive & Sensory (Core 10–11)
EF   — Executive Functioning & Processing (Core 4, 15)
RD   — Reading / Dyslexia (Core 12)
WR   — Written Expression / Dysgraphia (Core 13)
MA   — Mathematics / Dyscalculia (Core 14)
PA   — Visual, Auditory & Fluency (Core 15–17)
AN   — Anxiety (Core 18–22, 35, 38)
MD   — Depression, Irritability & Mood (Core 23–26, 36–38)
TR   — Trauma, Adversity & Stress (Core 27–30)
DB   — Oppositional, Aggressive & Conduct (Core 26, 31–34, 44)
SP   — School Avoidance, Peers & Belonging (Core 35, 43–44)
HS   — Eating, Sleep & Somatic (Core 36–38)
RX   — Tics, Psychosis, Substance & Digital (Core 39–42)

SAFETY: Items S1–S8 and Core C45–C47 bypass scoring entirely. Any endorsement requires same-day clinician review.
SCORING NOTE: Per Section 7 of the instrument, do not publish diagnostic cut-offs. Use item-level responses, domain sums, impairment indicators and safety flags. Missing/? responses are stored separately and not scored as zero.

CLINICAL NOTES ON KEY DOMAINS:
- Self-Harm and Suicidal Ideation: ALWAYS flag urgency; recommend immediate CPFSA referral if disclosed
- Psychotic Disorders: rare in this dataset (<2%); high screener scores across social, emotional, and trauma items
- Substance Use Disorders: often secondary to trauma or mood disorder; common co-occurrence with Depressive and Anxiety domains
- Somatic Symptom Disorders: often misclassified as physical health; headaches and stomach pain without medical cause are key indicators
- Epilepsy and Neurological Conditions: screener items q9 (academic decline) and q10 (trauma exposure) may be elevated due to school absence
- Headache Disorders (Migraine/Tension-Type): significant cause of school absence; elevated q9 in dataset
- Neurocognitive Disorders: rare in paediatric population; typically linked to acquired brain injury, HIE at birth, or post-infectious encephalitis
- Depressive Disorders: in Jamaican context, depression may present as irritability, somatic complaints, or withdrawal rather than overt sadness
- Anxiety Disorders: school refusal and PEP/CSEC performance anxiety are common presentations in ages 9-14

When you receive database context, analyse it carefully and give specific numbers. If asked about a record that isn't in your context, say so — do not fabricate data.

DATA SHAPE NOTE: The context may include separate single-dimension breakdowns (e.g. "parish_breakdown" lists totals per parish; "age_band_breakdown" lists totals per age band) AND a "cross_tabulation" field. When a question combines two or more dimensions — e.g. "12-14 year-olds in Kingston", "ADHD cases in rural settings", "deaf-inclusive sites in St. Catherine" — the single-dimension tables CANNOT answer it on their own, since they are independent totals, not an intersection. Always check for a "cross_tabulation" field first for combined-dimension questions; it contains the actual intersected counts you need. If "cross_tabulation" is present, use it directly and confidently — do not say the data is unavailable. Only state that data is missing if no relevant field was provided at all.

CHARTS & GRAPHS: When your response includes numerical comparisons, distributions, or trends that would be clearer as a visual, embed a chart using a fenced code block with language "chart". The JSON spec supports:
- type: "bar" | "line" | "pie" | "doughnut"
- title: chart title string
- labels: array of category labels
- datasets: array of {label, data: [numbers]} objects
- unit: optional suffix for tooltip values (e.g. "%" or " records")
- beginAtZero: true (default) | false

Example:
```chart
{"type":"bar","title":"Risk Level by Parish","labels":["Kingston","St. Andrew","St. Catherine"],"datasets":[{"label":"High Risk","data":[42,31,28]},{"label":"Moderate Risk","data":[89,74,65]}],"unit":" records"}
```

Use charts whenever you have 3+ data points being compared, showing a distribution, or showing change over categories. Always pair a chart with a brief written interpretation — charts alone do not tell the story."""


def _get_db_context_for_query(user_message: str, db, limit: int = 50,
                               site_filter: str = "", site_args: list = None) -> dict:
    """
    Kompass-aware context builder for the AI analysis tool.
    Routes queries to the right SQL based on keywords, returns structured
    JSON for Claude to reason over. All queries are site-scoped.
    """
    if site_args is None:
        site_args = []
    msg  = user_message.lower()
    ctx  = {}

    # ── MODULE LOOKUP TABLE ──────────────────────────────────────
    KOMPASS_MODULES = {
        "DEV": "Adaptive, Language & Motor Development",
        "AI":  "ADHD – Inattentive",
        "AH":  "ADHD – Hyperactive-Impulsive",
        "AS":  "Autism – Social Communication",
        "AR":  "Autism – Repetitive & Sensory",
        "EF":  "Executive Functioning & Processing",
        "RD":  "Reading / Dyslexia",
        "WR":  "Written Expression / Dysgraphia",
        "MA":  "Mathematics / Dyscalculia",
        "PA":  "Visual, Auditory & Fluency",
        "AN":  "Anxiety",
        "MD":  "Depression, Irritability & Mood",
        "TR":  "Trauma, Adversity & Stress",
        "DB":  "Oppositional, Aggressive & Conduct",
        "SP":  "School Avoidance, Peers & Belonging",
        "HS":  "Eating, Sleep & Somatic",
        "RX":  "Tics, Psychosis, Substance & Digital",
    }

    # ── MODULE KEYWORD MAP ───────────────────────────────────────
    # Maps query keywords → module codes so we can pull targeted data
    KEYWORD_TO_MODULES = {
        "adhd":           ["AI","AH"],
        "inattentive":    ["AI"],
        "hyperactive":    ["AH"],
        "impulsiv":       ["AH"],
        "autism":         ["AS","AR"],
        "asd":            ["AS","AR"],
        "social commun":  ["AS"],
        "repetitive":     ["AR"],
        "sensory":        ["AR"],
        "executive":      ["EF"],
        "processing":     ["EF","PA"],
        "dyslexia":       ["RD"],
        "reading":        ["RD"],
        "dysgraphia":     ["WR"],
        "writing":        ["WR"],
        "written":        ["WR"],
        "dyscalculia":    ["MA"],
        "math":           ["MA"],
        "maths":          ["MA"],
        "numeracy":       ["MA"],
        "auditory":       ["PA"],
        "visual":         ["PA"],
        "fluency":        ["PA"],
        "anxiety":        ["AN"],
        "anxious":        ["AN"],
        "worry":          ["AN"],
        "depress":        ["MD"],
        "mood":           ["MD"],
        "irritab":        ["MD"],
        "trauma":         ["TR"],
        "adversity":      ["TR"],
        "stress":         ["TR"],
        "oppositional":   ["DB"],
        "conduct":        ["DB"],
        "aggress":        ["DB"],
        "defian":         ["DB"],
        "school avoid":   ["SP"],
        "avoidance":      ["SP"],
        "peer":           ["SP"],
        "belong":         ["SP"],
        "eating":         ["HS"],
        "sleep":          ["HS"],
        "somatic":        ["HS"],
        "tic":            ["RX"],
        "psychosis":      ["RX"],
        "psychotic":      ["RX"],
        "substance":      ["RX"],
        "alcohol":        ["RX"],
        "digital":        ["RX"],
        "gaming":         ["RX"],
        "adaptive":       ["DEV"],
        "language":       ["DEV"],
        "motor":          ["DEV"],
        "develop":        ["DEV"],
    }

    # Detect which modules (if any) are relevant to this query
    mentioned_modules = set()
    for kw, codes in KEYWORD_TO_MODULES.items():
        if kw in msg:
            mentioned_modules.update(codes)

    # ── ALWAYS INCLUDE: KOMPASS SUMMARY STATS ───────────────────
    stats = db.execute(f"""
        SELECT
            (SELECT COUNT(*) FROM records r
             WHERE r.is_deleted=0{site_filter})                                          AS total_records,
            (SELECT COUNT(*) FROM records r
             WHERE r.is_deleted=0 AND r.phase=2{site_filter})                           AS pilot_records,
            (SELECT COUNT(*) FROM screenings s JOIN records r ON r.id=s.record_id
             WHERE r.is_deleted=0{site_filter})                                          AS total_screenings,
            (SELECT COUNT(*) FROM screenings s JOIN records r ON r.id=s.record_id
             WHERE s.risk_level='high' AND r.is_deleted=0{site_filter})                AS high_risk,
            (SELECT COUNT(*) FROM screenings s JOIN records r ON r.id=s.record_id
             WHERE s.risk_level='moderate' AND r.is_deleted=0{site_filter})            AS moderate_risk,
            (SELECT COUNT(*) FROM screenings s JOIN records r ON r.id=s.record_id
             WHERE s.risk_level='low' AND r.is_deleted=0{site_filter})                 AS low_risk,
            (SELECT COUNT(*) FROM screenings s JOIN records r ON r.id=s.record_id
             WHERE s.safety_alert=1 AND r.is_deleted=0{site_filter})                    AS safety_alerts,
            (SELECT ROUND(AVG(s.core_score),1) FROM screenings s
             JOIN records r ON r.id=s.record_id
             WHERE s.core_score IS NOT NULL AND r.is_deleted=0{site_filter})            AS avg_core_score,
            (SELECT ROUND(AVG(s.score_overall),1) FROM screenings s
             JOIN records r ON r.id=s.record_id
             WHERE s.score_overall IS NOT NULL AND r.is_deleted=0{site_filter})         AS avg_overall_score,
            (SELECT ROUND(AVG(s.not_observed_count),1) FROM screenings s
             JOIN records r ON r.id=s.record_id
             WHERE r.is_deleted=0{site_filter})                                          AS avg_not_observed
    """, site_args * 10).fetchone()
    ctx["database_summary"] = dict(stats)

    # ── RISK DISTRIBUTION ────────────────────────────────────────
    # Always include — Claude needs this for almost every question
    risk_rows = db.execute(f"""
        SELECT s.risk_level,
               COUNT(*) as count,
               ROUND(COUNT(*)*100.0/NULLIF((
                   SELECT COUNT(*) FROM screenings s2
                   JOIN records r2 ON r2.id=s2.record_id
                   WHERE r2.is_deleted=0{site_filter}),0),1) as pct,
               ROUND(AVG(s.core_score),1) as avg_core,
               ROUND(AVG(s.score_overall),1) as avg_overall
        FROM screenings s JOIN records r ON r.id=s.record_id
        WHERE s.score_overall IS NOT NULL AND r.is_deleted=0{site_filter}
        GROUP BY s.risk_level ORDER BY s.risk_level
    """, site_args * 2).fetchall()
    ctx["risk_distribution"] = rows_to_list(risk_rows)

    # ── MODULE TRIGGER RATES ─────────────────────────────────────
    # Always include — this is the core Kompass analytics view.
    # Parse JSON modules_triggered column to count per-module rates.
    # SQLite JSON support: json_each() extracts array elements.
    try:
        mod_rows = db.execute(f"""
            SELECT
                je.value                              AS module_code,
                COUNT(*)                              AS triggered_count,
                ROUND(COUNT(*)*100.0/NULLIF((
                    SELECT COUNT(*) FROM screenings s2
                    JOIN records r2 ON r2.id=s2.record_id
                    WHERE r2.is_deleted=0{site_filter} AND s2.modules_triggered IS NOT NULL
                    AND s2.modules_triggered != '[]'),0),1) AS trigger_pct,
                ROUND(AVG(s.core_score),1)            AS avg_core_when_triggered,
                ROUND(AVG(s.score_overall),1)         AS avg_overall_when_triggered
            FROM screenings s
            JOIN records r ON r.id=s.record_id
            JOIN json_each(s.modules_triggered) je
            WHERE r.is_deleted=0{site_filter}
              AND s.modules_triggered IS NOT NULL
              AND s.modules_triggered != '[]'
            GROUP BY je.value
            ORDER BY triggered_count DESC
        """, site_args * 2).fetchall()
        ctx["module_trigger_rates"] = rows_to_list(mod_rows)
    except Exception as e:
        log.warning("Module trigger rates query failed: %s", e)

    # ── SAFETY FLAGS ─────────────────────────────────────────────
    safety_kw = ["safety", "self-harm", "suicid", "crisis", "harm", "danger",
                 "protect", "urgent", "immediate", "alert", "s1", "s2", "s3",
                 "c45", "c46", "c47"]
    if any(w in msg for w in safety_kw) or ctx["database_summary"].get("safety_alerts", 0) > 0:
        try:
            safe_rows = db.execute(f"""
                SELECT r.id, r.age_band, r.parish, r.setting,
                       s.safety_flags, s.risk_level, s.score_overall,
                       s.completed_at
                FROM screenings s JOIN records r ON r.id=s.record_id
                WHERE s.safety_alert=1 AND r.is_deleted=0{site_filter}
                ORDER BY s.completed_at DESC LIMIT 20
            """, site_args).fetchall()
            ctx["safety_alerts"] = rows_to_list(safe_rows)

            safe_count = db.execute(f"""
                SELECT r.parish, COUNT(*) as alerts
                FROM screenings s JOIN records r ON r.id=s.record_id
                WHERE s.safety_alert=1 AND r.is_deleted=0{site_filter}
                GROUP BY r.parish ORDER BY alerts DESC
            """, site_args).fetchall()
            ctx["safety_by_parish"] = rows_to_list(safe_count)
        except Exception as e:
            log.warning("Safety query failed: %s", e)

    # ── MODULE-SPECIFIC DEEP DIVE ────────────────────────────────
    if mentioned_modules:
        # Pull per-module score distributions for mentioned modules
        for module_code in list(mentioned_modules)[:4]:  # cap at 4 to stay in context window
            try:
                col_name = f"mod_{module_code.lower()}_score"
                rows = db.execute(f"""
                    SELECT
                        r.age_band,
                        r.parish,
                        r.setting,
                        CAST(
                            json_extract(s.per_module_scores, '$.{module_code}')
                        AS REAL) AS module_score,
                        s.risk_level,
                        s.core_score
                    FROM screenings s JOIN records r ON r.id=s.record_id
                    WHERE r.is_deleted=0{site_filter}
                      AND json_extract(s.per_module_scores, '$.{module_code}') IS NOT NULL
                    ORDER BY module_score DESC
                    LIMIT 100
                """, site_args).fetchall()

                if rows:
                    scores = [r["module_score"] for r in rows if r["module_score"] is not None]
                    agg = db.execute(f"""
                        SELECT
                            COUNT(*) as n,
                            ROUND(AVG(CAST(json_extract(s.per_module_scores, '$.{module_code}') AS REAL)),1) as mean,
                            ROUND(MIN(CAST(json_extract(s.per_module_scores, '$.{module_code}') AS REAL)),1) as min,
                            ROUND(MAX(CAST(json_extract(s.per_module_scores, '$.{module_code}') AS REAL)),1) as max
                        FROM screenings s JOIN records r ON r.id=s.record_id
                        WHERE r.is_deleted=0{site_filter}
                          AND json_extract(s.per_module_scores, '$.{module_code}') IS NOT NULL
                    """, site_args).fetchone()

                    by_age = db.execute(f"""
                        SELECT r.age_band,
                               COUNT(*) as n,
                               ROUND(AVG(CAST(json_extract(s.per_module_scores, '$.{module_code}') AS REAL)),1) as mean_score
                        FROM screenings s JOIN records r ON r.id=s.record_id
                        WHERE r.is_deleted=0{site_filter}
                          AND json_extract(s.per_module_scores, '$.{module_code}') IS NOT NULL
                        GROUP BY r.age_band ORDER BY r.age_band
                    """, site_args).fetchall()

                    ctx[f"module_{module_code}"] = {
                        "module_code": module_code,
                        "module_label": KOMPASS_MODULES.get(module_code, module_code),
                        "stats": dict(agg) if agg else {},
                        "by_age_band": rows_to_list(by_age),
                    }
            except Exception as e:
                log.warning("Module deep dive failed for %s: %s", module_code, e)

    # ── MODULE CO-OCCURRENCE ──────────────────────────────────────
    cooc_kw = ["co-occur", "co occur", "together", "comorbid", "combination",
               "both", "and", "alongside", "common pair", "overlap"]
    if any(w in msg for w in cooc_kw) or len(mentioned_modules) >= 2:
        try:
            # Pull all trigger lists and compute pairs in Python
            # (SQLite doesn't have efficient array pair expansion)
            all_triggers = db.execute(f"""
                SELECT s.modules_triggered
                FROM screenings s JOIN records r ON r.id=s.record_id
                WHERE r.is_deleted=0{site_filter}
                  AND s.modules_triggered IS NOT NULL
                  AND s.modules_triggered != '[]'
                LIMIT 5000
            """, site_args).fetchall()

            pair_counts: dict = {}
            for row in all_triggers:
                try:
                    mods = json.loads(row["modules_triggered"])
                    mods = sorted(set(mods))
                    for i in range(len(mods)):
                        for j in range(i+1, len(mods)):
                            pair = f"{mods[i]}+{mods[j]}"
                            pair_counts[pair] = pair_counts.get(pair, 0) + 1
                except Exception:
                    pass

            top_pairs = sorted(pair_counts.items(), key=lambda x: -x[1])[:15]
            ctx["module_co_occurrence"] = [
                {"pair": p, "count": c,
                 "modules": [KOMPASS_MODULES.get(m, m) for m in p.split("+")]}
                for p, c in top_pairs
            ]
        except Exception as e:
            log.warning("Co-occurrence query failed: %s", e)

    # ── PARISH BREAKDOWN ─────────────────────────────────────────
    parish_kw = ["parish", "kingston", "st. catherine", "portland", "westmoreland",
                 "rural", "urban", "region", "location", "st. elizabeth",
                 "st. ann", "st. james", "manchester", "trelawny", "hanover",
                 "st. mary", "st. thomas"]
    if any(w in msg for w in parish_kw):
        rows = db.execute(f"""
            SELECT r.parish,
                   COUNT(DISTINCT r.id) as records,
                   ROUND(AVG(s.core_score),1) as avg_core_score,
                   ROUND(AVG(s.score_overall),1) as avg_overall,
                   SUM(CASE WHEN s.risk_level='high' THEN 1 ELSE 0 END) as high_risk,
                   SUM(CASE WHEN s.risk_level='moderate' THEN 1 ELSE 0 END) as moderate_risk,
                   SUM(CASE WHEN s.risk_level='low' THEN 1 ELSE 0 END) as low_risk,
                   SUM(CASE WHEN s.safety_alert=1 THEN 1 ELSE 0 END) as safety_alerts
            FROM records r LEFT JOIN screenings s ON s.record_id=r.id
            WHERE r.is_deleted=0{site_filter}
            GROUP BY r.parish ORDER BY records DESC
        """, site_args).fetchall()
        ctx["parish_breakdown"] = rows_to_list(rows)

        # Parish-level module trigger rates
        try:
            mod_parish = db.execute(f"""
                SELECT r.parish, je.value AS module_code, COUNT(*) as count
                FROM screenings s
                JOIN records r ON r.id=s.record_id
                JOIN json_each(s.modules_triggered) je
                WHERE r.is_deleted=0{site_filter}
                  AND s.modules_triggered IS NOT NULL
                GROUP BY r.parish, je.value
                ORDER BY r.parish, count DESC
            """, site_args).fetchall()
            ctx["module_rates_by_parish"] = rows_to_list(mod_parish)
        except Exception as e:
            log.warning("Parish module rates failed: %s", e)

    # ── AGE BAND BREAKDOWN ───────────────────────────────────────
    age_kw = ["age", "year", "4-7", "8-12", "13-17", "early childhood",
              "middle childhood", "adolescen", "teen", "child", "csec", "pep"]
    if any(w in msg for w in age_kw):
        rows = db.execute(f"""
            SELECT r.age_band,
                   COUNT(DISTINCT r.id) as records,
                   ROUND(AVG(s.core_score),1) as avg_core_score,
                   ROUND(AVG(s.score_overall),1) as avg_overall,
                   SUM(CASE WHEN s.risk_level='high' THEN 1 ELSE 0 END) as high_risk,
                   SUM(CASE WHEN s.safety_alert=1 THEN 1 ELSE 0 END) as safety_alerts,
                   SUM(CASE WHEN s.not_observed_count > 5 THEN 1 ELSE 0 END) as high_not_observed
            FROM records r LEFT JOIN screenings s ON s.record_id=r.id
            WHERE r.is_deleted=0{site_filter}
            GROUP BY r.age_band ORDER BY r.age_band
        """, site_args).fetchall()
        ctx["age_band_breakdown"] = rows_to_list(rows)

        # Age-level module trigger rates
        try:
            mod_age = db.execute(f"""
                SELECT r.age_band, je.value AS module_code, COUNT(*) as count
                FROM screenings s
                JOIN records r ON r.id=s.record_id
                JOIN json_each(s.modules_triggered) je
                WHERE r.is_deleted=0{site_filter}
                  AND s.modules_triggered IS NOT NULL
                GROUP BY r.age_band, je.value
                ORDER BY r.age_band, count DESC
            """, site_args).fetchall()
            ctx["module_rates_by_age"] = rows_to_list(mod_age)
        except Exception as e:
            log.warning("Age module rates failed: %s", e)

    # ── CORE SCORE DISTRIBUTION ──────────────────────────────────
    core_kw = ["core score", "core", "overall score", "average score", "score dist",
               "score breakdown", "percentile", "mean score"]
    if any(w in msg for w in core_kw) or any(w in msg for w in
           ["score", "distribution", "average", "mean", "spread"]):
        rows = db.execute(f"""
            SELECT
                CASE
                    WHEN s.core_score < 20  THEN '0–19%'
                    WHEN s.core_score < 40  THEN '20–39%'
                    WHEN s.core_score < 55  THEN '40–54%'
                    WHEN s.core_score < 70  THEN '55–69%'
                    ELSE '70–100%'
                END AS score_band,
                COUNT(*) as count,
                ROUND(AVG(s.core_score),1) as avg_core
            FROM screenings s JOIN records r ON r.id=s.record_id
            WHERE s.core_score IS NOT NULL AND r.is_deleted=0{site_filter}
            GROUP BY score_band ORDER BY score_band
        """, site_args).fetchall()
        ctx["core_score_distribution"] = rows_to_list(rows)

        ctx["core_score_stats"] = dict(db.execute(f"""
            SELECT
                ROUND(AVG(s.core_score),1) as mean,
                ROUND(MIN(s.core_score),1) as min,
                ROUND(MAX(s.core_score),1) as max,
                COUNT(*) as n
            FROM screenings s JOIN records r ON r.id=s.record_id
            WHERE s.core_score IS NOT NULL AND r.is_deleted=0{site_filter}
        """, site_args).fetchone() or {})

    # ── NOT OBSERVED / ? RESPONSES ───────────────────────────────
    if any(w in msg for w in ["not observed", "not sure", "missing", "incomplete",
                               "question mark", "skipped", "unknown"]):
        rows = db.execute(f"""
            SELECT
                r.age_band,
                ROUND(AVG(s.not_observed_count),1) as avg_not_observed,
                SUM(CASE WHEN s.not_observed_count > 10 THEN 1 ELSE 0 END) as high_not_observed_count
            FROM screenings s JOIN records r ON r.id=s.record_id
            WHERE r.is_deleted=0{site_filter}
            GROUP BY r.age_band ORDER BY r.age_band
        """, site_args).fetchall()
        ctx["not_observed_by_age"] = rows_to_list(rows)

    # ── CROSS-TABULATIONS ────────────────────────────────────────
    PARISH_NAMES = {
        "kingston":     "Kingston & St. Andrew",
        "st. andrew":   "Kingston & St. Andrew",
        "ksa":          "Kingston & St. Andrew",
        "st. catherine":"St. Catherine",
        "catherine":    "St. Catherine",
        "st. elizabeth":"St. Elizabeth",
        "portland":     "Portland",
        "st. ann":      "St. Ann",
        "westmoreland": "Westmoreland",
        "manchester":   "Manchester",
        "st. james":    "St. James",
    }
    KOMPASS_AGE_KNOWN = ["4-7", "8-12", "13-17"]
    SETTINGS_KNOWN    = ["school", "clinic", "deaf-inclusive"]

    mentioned_parishes = sorted({v for k,v in PARISH_NAMES.items() if k in msg})
    mentioned_ages     = [a for a in KOMPASS_AGE_KNOWN if a in msg or a.replace("-","–") in msg]
    mentioned_settings = [s for s in SETTINGS_KNOWN if s in msg]

    dims_present = sum([bool(mentioned_parishes), bool(mentioned_ages),
                        bool(mentioned_settings), bool(mentioned_modules)])

    if dims_present >= 2:
        group_cols  = []
        where_extra, where_args = [], list(site_args)
        if mentioned_parishes:
            group_cols.append("r.parish")
            where_extra.append(f"r.parish IN ({','.join('?' for _ in mentioned_parishes)})")
            where_args.extend(mentioned_parishes)
        if mentioned_ages:
            group_cols.append("r.age_band")
            where_extra.append(f"r.age_band IN ({','.join('?' for _ in mentioned_ages)})")
            where_args.extend(mentioned_ages)
        if mentioned_settings:
            group_cols.append("r.setting")
            where_extra.append(f"r.setting IN ({','.join('?' for _ in mentioned_settings)})")
            where_args.extend(mentioned_settings)
        if mentioned_modules and not mentioned_parishes and not mentioned_ages:
            group_cols.append("r.primary_domain")

        if len(group_cols) >= 1:
            extra_sql = (" AND " + " AND ".join(where_extra)) if where_extra else ""
            try:
                rows = db.execute(f"""
                    SELECT {', '.join(group_cols)},
                           COUNT(DISTINCT r.id) as records,
                           ROUND(AVG(s.core_score),1) as avg_core_score,
                           ROUND(AVG(s.score_overall),1) as avg_overall,
                           SUM(CASE WHEN s.risk_level='high' THEN 1 ELSE 0 END) as high_risk,
                           SUM(CASE WHEN s.safety_alert=1 THEN 1 ELSE 0 END) as safety_alerts
                    FROM records r LEFT JOIN screenings s ON s.record_id=r.id
                    WHERE r.is_deleted=0{site_filter}{extra_sql}
                    GROUP BY {', '.join(group_cols)} ORDER BY records DESC LIMIT 100
                """, where_args).fetchall()
                ctx["cross_tabulation"] = {
                    "dimensions": [c.replace("r.", "") for c in group_cols],
                    "filters_applied": {
                        "parishes":  mentioned_parishes or None,
                        "age_bands": mentioned_ages     or None,
                        "settings":  mentioned_settings or None,
                        "modules":   list(mentioned_modules) or None,
                    },
                    "results": rows_to_list(rows),
                }
            except Exception as e:
                log.warning("Cross-tab query failed: %s", e)

    # ── SETTING BREAKDOWN ────────────────────────────────────────
    setting_kw = ["setting", "school", "clinic", "deaf", "inclusive", "site"]
    if any(w in msg for w in setting_kw):
        rows = db.execute(f"""
            SELECT r.setting,
                   COUNT(DISTINCT r.id) as records,
                   ROUND(AVG(s.core_score),1) as avg_core_score,
                   ROUND(AVG(s.score_overall),1) as avg_overall,
                   SUM(CASE WHEN s.risk_level='high' THEN 1 ELSE 0 END) as high_risk,
                   SUM(CASE WHEN s.safety_alert=1 THEN 1 ELSE 0 END) as safety_alerts
            FROM records r LEFT JOIN screenings s ON s.record_id=r.id
            WHERE r.is_deleted=0{site_filter} GROUP BY r.setting ORDER BY records DESC
        """, site_args).fetchall()
        ctx["setting_breakdown"] = rows_to_list(rows)

    # ── KAPPA / CLINICIAN AGREEMENT ──────────────────────────────
    kappa_kw = ["kappa", "agree", "clinician", "valid", "accuracy",
                "sensitivity", "specificity", "concordance"]
    if any(w in msg for w in kappa_kw):
        pairs = db.execute(f"""
            SELECT s.risk_level as ai_risk, cv.validated_risk as cli_risk,
                   r.parish, r.setting, r.age_band
            FROM screenings s
            JOIN clinician_validations cv ON cv.screening_id=s.id
            JOIN records r ON r.id=s.record_id
            WHERE cv.kappa_eligible=1 AND r.is_deleted=0{site_filter}
        """, site_args).fetchall()
        kappa  = _compute_kappa(pairs)
        levels = ["low", "moderate", "high"]
        matrix = {a: {b: 0 for b in levels} for a in levels}
        for p in pairs:
            a, b = p["ai_risk"], p["cli_risk"]
            if a in levels and b in levels:
                matrix[a][b] += 1
        ctx["kappa_analysis"] = {
            "kappa_overall":   kappa,
            "n_pairs":         len(pairs),
            "interpretation":  "Substantial" if kappa>=0.61 else "Moderate" if kappa>=0.41 else "Fair",
            "confusion_matrix": matrix,
        }

    # ── BIAS & REPRESENTATION ────────────────────────────────────
    bias_kw = ["bias", "rural", "representation", "underrepresent",
               "sensory", "deaf", "equity", "access"]
    if any(w in msg for w in bias_kw):
        rural = ["Portland", "St. Elizabeth", "St. Ann", "Westmoreland"]
        rq = ",".join(["?"] * len(rural))
        row = db.execute(f"""
            SELECT
                COUNT(CASE WHEN r.parish IN ({rq}) THEN 1 END) as rural_count,
                COUNT(CASE WHEN r.parish NOT IN ({rq}) THEN 1 END) as urban_count,
                COUNT(*) as total,
                COUNT(CASE WHEN r.setting='deaf-inclusive' THEN 1 END) as deaf_inclusive
            FROM records r WHERE r.is_deleted=0{site_filter}
        """, rural + rural + site_args).fetchone()
        if row:
            ctx["bias_representation"] = {
                "rural_count":        row["rural_count"],
                "urban_count":        row["urban_count"],
                "total":              row["total"],
                "rural_pct":          round(row["rural_count"]/row["total"]*100,1) if row["total"] else 0,
                "deaf_inclusive_count": row["deaf_inclusive"],
            }

    # ── TREND OVER TIME ──────────────────────────────────────────
    trend_kw = ["trend", "time", "month", "year", "over time", "increase",
                "decrease", "change", "2024", "2025", "2026"]
    if any(w in msg for w in trend_kw):
        rows = db.execute(f"""
            SELECT CAST(strftime('%Y', s.completed_at) AS INTEGER) as year,
                   CAST(strftime('%m', s.completed_at) AS INTEGER) as month,
                   COUNT(*) as screenings,
                   SUM(CASE WHEN s.risk_level='high' THEN 1 ELSE 0 END) as high_risk,
                   SUM(CASE WHEN s.safety_alert=1 THEN 1 ELSE 0 END) as safety_alerts,
                   ROUND(AVG(s.core_score),1) as avg_core_score
            FROM screenings s JOIN records r ON r.id=s.record_id
            WHERE s.completed_at IS NOT NULL AND r.is_deleted=0{site_filter}
            GROUP BY year, month ORDER BY year, month
        """, site_args).fetchall()
        ctx["trend_over_time"] = rows_to_list(rows)

    # ── REFERRALS ────────────────────────────────────────────────
    ref_kw = ["referral", "pending", "follow up", "referred", "urgent",
              "immediate", "outstanding", "action"]
    if any(w in msg for w in ref_kw):
        rows = db.execute(f"""
            SELECT rf.status, rf.urgency, rf.referral_level, COUNT(*) as count
            FROM referrals rf JOIN screenings s ON s.id=rf.screening_id
            JOIN records r ON r.id=s.record_id
            WHERE r.is_deleted=0{site_filter}
            GROUP BY rf.status, rf.urgency, rf.referral_level ORDER BY count DESC
        """, site_args).fetchall()
        ctx["referral_status"] = rows_to_list(rows)

        pending = db.execute(f"""
            SELECT r.id, r.parish, r.age_band, s.score_overall, s.core_score,
                   s.risk_level, s.safety_alert, s.modules_triggered
            FROM records r JOIN screenings s ON s.record_id=r.id
            JOIN referrals rf ON rf.screening_id=s.id
            WHERE rf.status='pending' AND r.is_deleted=0{site_filter}
            ORDER BY s.score_overall DESC LIMIT 25
        """, site_args).fetchall()
        ctx["pending_referrals"] = rows_to_list(pending)

    # ── SPECIFIC RECORD LOOKUP ───────────────────────────────────
    record_ids = re.findall(r"KADM-\d{4,6}", user_message, re.IGNORECASE)
    if record_ids:
        for rid in record_ids[:3]:
            row = db.execute(f"""
                SELECT r.id, r.age_band, r.parish, r.setting, r.rated_by,
                       r.instruments, r.primary_domain, r.phase, r.created_at,
                       s.screener_version, s.core_score, s.score_overall,
                       s.risk_level, s.modules_triggered, s.modules_completed,
                       s.per_module_scores, s.not_observed_count,
                       s.safety_alert, s.safety_flags, s.completed_at,
                       cv.validated_risk, cv.primary_concern, cv.confidence_level
                FROM records r
                LEFT JOIN screenings s ON s.record_id=r.id
                LEFT JOIN clinician_validations cv ON cv.screening_id=s.id
                WHERE r.id=? AND r.is_deleted=0{site_filter}
                ORDER BY s.completed_at DESC LIMIT 1
            """, [rid.upper()] + site_args).fetchone()
            ctx[f"record_{rid.upper()}"] = dict(row) if row else "Not found in database (may be outside your site access)"

    # ── PHASE / RESEARCH PROGRESS ────────────────────────────────
    phase_kw = ["phase", "archival", "pilot", "stage", "progress", "task"]
    if any(w in msg for w in phase_kw):
        rows = db.execute("""
            SELECT phase_number, task_name, status
            FROM phase_tasks ORDER BY phase_number, id
        """).fetchall()
        ctx["phase_tasks"] = rows_to_list(rows)

    return ctx



def call_claude(messages: list, db, user_message: str, api_key: str,
                site_filter: str = "", site_args: list = None) -> tuple:
    """
    Call the Anthropic API with real database context embedded in the message.
    site_filter/site_args scope the DB queries to the calling user's site.
    Returns (reply_text, tokens_used, data_context_json)
    """
    if site_args is None:
        site_args = []
    if not api_key:
        return (
            "No API key configured. Please set ANTHROPIC_API_KEY in your .env file "
            "or enter your key in the AI console.", 0, "{}"
        )

    # Pull relevant data from the database — scoped to the user's site
    try:
        data_ctx = _get_db_context_for_query(user_message, db,
                                              site_filter=site_filter,
                                              site_args=site_args)
    except Exception as e:
        log.warning("DB context fetch error: %s", e)
        data_ctx = {}

    # Build the message with database context embedded
    ctx_json   = json.dumps(data_ctx, indent=2, default=str)
    augmented_msg = (
        f"{user_message}\n\n"
        f"---\n"
        f"DATABASE CONTEXT (live data as of {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}):\n"
        f"```json\n{ctx_json}\n```\n"
        f"Use this data to answer the question with specific numbers."
    )

    # Build API messages list — replace last user message with augmented version
    api_messages = []
    for m in messages[:-1]:
        api_messages.append({"role": m["role"], "content": m["content"]})
    api_messages.append({"role": "user", "content": augmented_msg})

    payload = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": CLAUDE_MAX_TOKENS,
        "system": CLAUDE_SYSTEM_PROMPT,
        "messages": api_messages,
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type":       "application/json",
            "x-api-key":          api_key,
            "anthropic-version":  "2023-06-01",
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data    = json.loads(resp.read())
            text    = data.get("content", [{}])[0].get("text", "No response generated.")
            tokens  = data.get("usage", {}).get("output_tokens", 0)
            return text, tokens, ctx_json
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        log.error("Claude API error %d: %s", e.code, body)
        if e.code == 401:
            return "Invalid API key. Please check your ANTHROPIC_API_KEY setting.", 0, "{}"
        if e.code == 429:
            return "Rate limit reached. Please wait a moment and try again.", 0, "{}"
        return f"API error ({e.code}): {body[:200]}", 0, "{}"
    except Exception as e:
        log.error("Claude call failed: %s", e)
        return f"Connection error: {str(e)}", 0, "{}"


# ══════════════════════════════════════════════════════════════════
# ROUTE HANDLERS
# ══════════════════════════════════════════════════════════════════

def handle_health(req):
    db = get_db()
    rc = db.execute("SELECT COUNT(*) as c FROM records WHERE is_deleted=0").fetchone()["c"]
    sc = db.execute("SELECT COUNT(*) as c FROM screenings").fetchone()["c"]
    ai = "configured" if ANTHROPIC_API_KEY else "not_configured"
    return ok({
        "service":    "KAHLE Psych Kompass API",
        "version":    "3.0.0",
        "ethics":     "MOH-JA-2025-0047",
        "db_ok":      True,
        "records":    rc,
        "screenings": sc,
        "claude_ai":  ai,
        "model":      CLAUDE_MODEL,
        "uptime_s":   int(time.time() - START_TIME),
    })

def handle_login(req):
    body = req.get("body", {})
    username = body.get("username", "").strip()
    password = body.get("password", "")
    if not username or not password:
        return err("Username and password required", 400)
    db  = get_db()
    row = db.execute(
        "SELECT * FROM users WHERE username=? AND is_active=1", (username,)
    ).fetchone()
    if not row or not _verify_pw(password, row["password_hash"]):
        audit(None, "LOGIN_FAILED", "auth", username, req.get("ip"))
        return err("Invalid credentials", 401)
    token = _make_token(row["id"])
    exp   = datetime.datetime.utcnow() + datetime.timedelta(seconds=SESSION_TTL)
    db.execute(
        "INSERT INTO sessions(token,user_id,expires_at,ip_address) VALUES(?,?,?,?)",
        (token, row["id"], exp.isoformat(), req.get("ip"))
    )
    db.execute("UPDATE users SET last_login=datetime('now') WHERE id=?", (row["id"],))
    db.commit()
    audit(row["id"], "LOGIN_SUCCESS", "auth", row["id"], req.get("ip"))
    return ok({
        "token":      token,
        "expires_at": exp.isoformat() + "Z",
        "user": {
            "id":        row["id"],
            "username":  row["username"],
            "role":      row["role"],
            "full_name": row["full_name"],
            "site_code": row["site_code"],
            "site_name": row["site_name"],
            "is_admin":  row["role"] in ("pi", "admin", "researcher"),
        },
        "claude_ai_available": bool(ANTHROPIC_API_KEY),
    }, message="Logged in successfully")

def handle_logout(req):
    token = req.get("token")
    if token:
        db = get_db()
        db.execute("DELETE FROM sessions WHERE token=?", (token,))
        db.commit()
    return ok(message="Logged out")

def handle_me(req):
    uid = req.get("user_id")
    if not uid:
        return err("Not authenticated", 401)
    db  = get_db()
    row = db.execute(
        "SELECT id,username,role,full_name,last_login FROM users WHERE id=?", (uid,)
    ).fetchone()
    return ok(dict(row)) if row else err("User not found", 404)

def handle_dashboard_stats(req):
    uid = req.get("user_id")
    db  = get_db()
    sf, sf_args = _site_filter(uid, db, "r") if uid else ("", [])

    total   = db.execute(f"SELECT COUNT(*) as c FROM records r WHERE r.is_deleted=0{sf}", sf_args).fetchone()["c"]
    piloted = db.execute(f"SELECT COUNT(*) as c FROM records r WHERE r.phase=2 AND r.is_deleted=0{sf}", sf_args).fetchone()["c"]
    high    = db.execute(
        f"SELECT COUNT(*) as c FROM screenings s JOIN records r ON r.id=s.record_id WHERE s.risk_level='high' AND r.is_deleted=0{sf}",
        sf_args
    ).fetchone()["c"]

    pairs = db.execute(f"""
        SELECT s.risk_level as ai_risk, cv.validated_risk as cli_risk
        FROM screenings s
        JOIN clinician_validations cv ON cv.screening_id=s.id
        JOIN records r ON r.id=s.record_id
        WHERE cv.kappa_eligible=1 AND r.is_deleted=0{sf}
    """, sf_args).fetchall()
    kappa = _compute_kappa(pairs) if pairs else 0.0

    domain_rows = db.execute(f"""
        SELECT r.primary_domain, s.risk_level, COUNT(*) as cnt
        FROM records r JOIN screenings s ON s.record_id=r.id
        WHERE r.primary_domain IS NOT NULL AND r.is_deleted=0{sf}
        GROUP BY r.primary_domain, s.risk_level
    """, sf_args).fetchall()
    domains = {}
    for row in domain_rows:
        d = row["primary_domain"]
        if d not in domains:
            domains[d] = {"low": 0, "moderate": 0, "high": 0}
        domains[d][row["risk_level"]] = domains[d].get(row["risk_level"], 0) + row["cnt"]

    recent = db.execute(f"""
        SELECT r.id, r.age_band, r.parish, r.setting,
               s.risk_level, s.score_overall, s.completed_at
        FROM screenings s JOIN records r ON r.id=s.record_id
        WHERE s.risk_level IN ('high','moderate') AND r.is_deleted=0{sf}
        ORDER BY s.completed_at DESC LIMIT 8
    """, sf_args).fetchall()

    parish_dist = db.execute(f"""
        SELECT r.parish, COUNT(*) as cnt FROM records r
        WHERE r.is_deleted=0{sf} GROUP BY r.parish ORDER BY cnt DESC
    """, sf_args).fetchall()

    bias = db.execute("SELECT * FROM bias_audits ORDER BY audit_date DESC LIMIT 1").fetchone()

    # Include site info for the frontend to display
    site = _get_user_site(uid, db) if uid else "ALL"
    site_name = None
    if uid and site not in ("ALL", None):
        row = db.execute("SELECT site_name FROM users WHERE id=?", (uid,)).fetchone()
        site_name = row["site_name"] if row else None

    return ok({
        "totals": {"records": total, "piloted": piloted,
                   "high_risk": high, "kappa": round(kappa, 3)},
        "domain_distribution":  domains,
        "recent_referrals":     rows_to_list(recent),
        "parish_distribution":  rows_to_list(parish_dist),
        "latest_bias_audit":    dict(bias) if bias else None,
        "claude_available":     bool(ANTHROPIC_API_KEY),
        "site_scoped":          site not in ("ALL", None),
        "site_code":            site if site not in ("ALL", None) else None,
        "site_name":            site_name,
    })

def handle_records_list(req):
    uid    = req.get("user_id")
    db     = get_db()
    sf, sf_args = _site_filter(uid, db, "r") if uid else ("", [])
    params = req.get("params", {})
    parish = params.get("parish",   [None])[0]
    risk   = params.get("risk",     [None])[0]
    age    = params.get("age_band", [None])[0]
    phase  = params.get("phase",    [None])[0]
    search = params.get("search",   [None])[0]
    domain = params.get("domain",   [None])[0]
    limit  = min(int(params.get("limit", ["50"])[0]), 200)
    offset = int(params.get("offset", ["0"])[0])

    where, args = ["r.is_deleted=0"], []
    if parish: where.append("r.parish=?");        args.append(parish)
    if age:    where.append("r.age_band=?");       args.append(age)
    if phase:  where.append("r.phase=?");          args.append(int(phase))
    if risk:   where.append("s.risk_level=?");     args.append(risk)
    if domain: where.append("r.primary_domain=?"); args.append(domain)
    if search: where.append("r.id LIKE ?");        args.append(f"%{search.upper()}%")
    # Site-scoping: appended to WHERE directly
    if sf:
        where.append(f"r.site_code=?")
        args.extend(sf_args)

    sql = f"""
        SELECT r.id, r.age_band, r.parish, r.setting, r.rated_by,
               r.instruments, r.primary_domain, r.secondary_domain, r.domain_tags,
               r.phase, r.data_source, r.gender_code, r.created_at, r.site_code,
               s.risk_level, s.score_overall, s.completed_at,
               s.core_score, s.per_module_scores, s.modules_triggered,
               s.safety_alert, s.not_observed_count,
               cv.validated_risk
        FROM records r
        LEFT JOIN screenings s ON s.record_id=r.id
        LEFT JOIN clinician_validations cv ON cv.screening_id=s.id
        WHERE {' AND '.join(where)}
        ORDER BY r.created_at DESC
        LIMIT ? OFFSET ?
    """
    rows  = db.execute(sql, args + [limit, offset]).fetchall()
    total = db.execute(
        f"SELECT COUNT(DISTINCT r.id) as c FROM records r "
        f"LEFT JOIN screenings s ON s.record_id=r.id "
        f"WHERE {' AND '.join(where)}", args
    ).fetchone()["c"]

    return ok({"records": rows_to_list(rows), "total": total,
               "limit": limit, "offset": offset})

def handle_record_create(req):
    uid  = req.get("user_id")
    if not uid:
        return err("Not authenticated", 401)
    body = req.get("body", {})
    required = ["age_band", "parish", "setting", "rated_by"]
    missing  = [f for f in required if not body.get(f)]
    if missing:
        return err(f"Missing required fields: {', '.join(missing)}", 400)

    db     = get_db()
    # Stamp record with the creating user's site_code
    user_row  = db.execute("SELECT site_code FROM users WHERE id=?", (uid,)).fetchone()
    site_code = user_row["site_code"] if user_row else None

    # Generate a collision-safe ID by finding the true numeric maximum,
    # then retrying with an increment if a collision still occurs (race condition safety).
    for attempt in range(10):
        row = db.execute(
            "SELECT MAX(CAST(SUBSTR(id, 6) AS INTEGER)) AS max_n FROM records WHERE id LIKE 'KADM-%'"
        ).fetchone()
        next_n = (row["max_n"] or 0) + 1 + attempt
        rec_id = f"KADM-{next_n:05d}"
        # Check it doesn't already exist before inserting
        if not db.execute("SELECT 1 FROM records WHERE id=?", (rec_id,)).fetchone():
            break
    else:
        return err("Could not generate unique record ID — please try again", 500)

    db.execute(
        """INSERT INTO records(id,age_band,parish,setting,rated_by,instruments,
           primary_domain,secondary_domain,domain_tags,phase,data_source,gender_code,
           referral_reason,site_code,created_by,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (rec_id, body["age_band"], body["parish"], body["setting"],
         body["rated_by"], json.dumps(body.get("instruments", [])),
         body.get("primary_domain"), body.get("secondary_domain"),
         json.dumps(body.get("domain_tags", [])),
         body.get("phase", 2), body.get("data_source", "pilot"),
         body.get("gender_code"), body.get("referral_reason"),
         site_code, uid, datetime.datetime.utcnow().isoformat())
    )
    db.commit()
    audit(uid, "RECORD_CREATE", "record", rec_id, req.get("ip"))
    return ok({"id": rec_id}, status=201, message="Record created")

def handle_record_get(req, record_id):
    db  = get_db()
    row = db.execute(
        "SELECT * FROM records WHERE id=? AND is_deleted=0", (record_id,)
    ).fetchone()
    if not row:
        return err("Record not found", 404)
    rec = dict(row)
    rec["screenings"]        = rows_to_list(db.execute("SELECT * FROM screenings WHERE record_id=? ORDER BY completed_at DESC", (record_id,)).fetchall())
    rec["instrument_scores"] = rows_to_list(db.execute("SELECT * FROM instrument_scores WHERE record_id=?", (record_id,)).fetchall())
    rec["consents"]          = rows_to_list(db.execute("SELECT id,consent_type,status,signed_at,form_version FROM consents WHERE record_id=?", (record_id,)).fetchall())
    return ok(rec)

# ══════════════════════════════════════════════════════════════════════
# KAHLE PSYCH KOMPASS ENGINE  v1.0
# Implements the two-tier architecture from the KAHLE Psych Kompass
# Parent/Caregiver Item Bank v0.1 (25 August 2026).
# ══════════════════════════════════════════════════════════════════════

# Age band constants matching the document
KOMPASS_AGE_BANDS = ["4-7", "8-12", "13-17"]

# Core item age routing: which items are shown for which age bands
# Keys: item code (C1–C50), Values: set of valid age bands
# "All" = all three bands; otherwise listed explicitly
CORE_AGE_ROUTING = {
    # Items available to ALL age bands
    **{f"C{n}": {"4-7","8-12","13-17"} for n in
       [1,2,3,4,5,6,7,8,9,10,11,12,13,14,16,17,18,20,22,23,24,26,27,28,
        29,30,31,32,35,36,37,38,39,42,43,44,45,46,47,48,49,50]},
    # 8–17 only
    "C15": {"8-12","13-17"},
    "C21": {"8-12","13-17"},
    "C25": {"8-12","13-17"},
    "C33": {"8-12","13-17"},
    "C34": {"8-12","13-17"},
    "C41": {"8-12","13-17"},
    # 4–12 only
    "C19": {"4-7","8-12"},
    # 13–17 only
    "C40": {"13-17"},
}

# Module trigger rules: which Core items (at score >= 2) open each module
KOMPASS_MODULE_TRIGGERS = {
    "DEV":  {"core": [1,2,3],        "label": "Adaptive, Language & Motor Development"},
    "AI":   {"core": [4,5],          "label": "ADHD – Inattentive"},
    "AH":   {"core": [6,7],          "label": "ADHD – Hyperactive-Impulsive"},
    "AS":   {"core": [2,8,9],        "label": "Autism – Social Communication"},
    "AR":   {"core": [10,11],        "label": "Autism – Repetitive & Sensory"},
    "EF":   {"core": [4,15],         "label": "Executive Functioning & Processing"},
    "RD":   {"core": [12],           "label": "Reading / Dyslexia"},
    "WR":   {"core": [13],           "label": "Written Expression / Dysgraphia"},
    "MA":   {"core": [14],           "label": "Mathematics / Dyscalculia"},
    "PA":   {"core": [15,16,17],     "label": "Visual, Auditory & Fluency"},
    "AN":   {"core": [18,19,20,21,22,35,38], "label": "Anxiety"},
    "MD":   {"core": [23,24,25,26,36,37,38], "label": "Depression, Irritability & Mood"},
    "TR":   {"core": [27,28,29,30],  "label": "Trauma, Adversity & Stress"},
    "DB":   {"core": [26,31,32,33,34,44],    "label": "Oppositional, Aggressive & Conduct"},
    "SP":   {"core": [35,43,44],     "label": "School Avoidance, Peers & Belonging"},
    "HS":   {"core": [36,37,38],     "label": "Eating, Sleep & Somatic"},
    "RX":   {"core": [39,40,41,42],  "label": "Tics, Psychosis, Substance & Digital"},
}

# Safety items that bypass scoring entirely
SAFETY_ITEM_CODES = ["S1","S2","S3","S4","S5","S6","S7","S8"]

# Core items that, if endorsed >= 1, trigger the safety review section
SAFETY_TRIGGER_CORE = [45, 46, 47]


def kompass_get_triggered_modules(core_responses: dict, age_band: str) -> list:
    """
    Given a dict of Core item responses {C1: score, C2: score, ...}
    and the child's age band, return a list of module codes to open.
    Threshold: Core item score >= 2 (Sometimes or more).
    """
    triggered = []
    for module_code, meta in KOMPASS_MODULE_TRIGGERS.items():
        for core_num in meta["core"]:
            item_key = f"C{core_num}"
            score = core_responses.get(item_key)
            if score is not None and score != "?" and int(score) >= 2:
                # Check the item is valid for this age band
                valid_bands = CORE_AGE_ROUTING.get(item_key, {"4-7","8-12","13-17"})
                if age_band in valid_bands:
                    triggered.append(module_code)
                    break  # one triggering item is enough per module
    return triggered


def kompass_check_safety(responses: dict) -> tuple:
    """
    Check Core items 45–47 and explicit safety items S1–S8.
    Returns (safety_alert: bool, safety_flags: list of item codes endorsed)
    """
    flags = []
    # Core items that indicate safety concern
    for n in SAFETY_TRIGGER_CORE:
        key = f"C{n}"
        val = responses.get(key)
        if val is not None and val != "?" and int(val) >= 1:
            flags.append(key)
    # Explicit safety items
    for code in SAFETY_ITEM_CODES:
        val = responses.get(code)
        if val is not None and val != "?" and int(val) >= 1:
            flags.append(code)
    return (len(flags) > 0, flags)


def kompass_score(responses: dict, age_band: str) -> dict:
    """
    Score a completed Kompass response dict.
    Returns a result dict with per-module scores, overall risk, etc.
    Scale: 0=Never, 1=A little, 2=Sometimes, 3=Often, ?=Not observed (excluded)
    """
    # Separate Core, module, and safety responses
    core_resp    = {k: v for k, v in responses.items() if k.startswith("C") and k[1:].isdigit()}
    module_resp  = {k: v for k, v in responses.items()
                    if not k.startswith("C") and k not in SAFETY_ITEM_CODES}
    safety_resp  = {k: v for k, v in responses.items() if k in SAFETY_ITEM_CODES}

    not_observed = sum(1 for v in responses.values() if v == "?")

    # Score Core items (exclude ?, exclude items not valid for this age band)
    core_vals = []
    for k, v in core_resp.items():
        if v == "?": continue
        valid_bands = CORE_AGE_ROUTING.get(k, {"4-7","8-12","13-17"})
        if age_band in valid_bands:
            core_vals.append(int(v))
    core_score = round(sum(core_vals) / (len(core_vals) * 3) * 100, 1) if core_vals else 0.0

    # Get triggered modules from Core responses
    triggered = kompass_get_triggered_modules(core_resp, age_band)

    # Score each module
    per_module_scores = {}
    # Group module responses by prefix
    module_groups: dict = {}
    for k, v in module_resp.items():
        if v == "?": continue
        # Extract prefix (letters before digits): AI1 → AI, RX10 → RX
        import re as _re
        m = _re.match(r'^([A-Z]+)', k)
        if m:
            prefix = m.group(1)
            module_groups.setdefault(prefix, []).append(int(v))

    for prefix, vals in module_groups.items():
        if vals:
            per_module_scores[prefix] = round(sum(vals) / (len(vals) * 3) * 100, 1)

    # Overall risk: weighted blend of Core score + any module scores
    all_scores = [core_score] + list(per_module_scores.values())
    overall = round(sum(all_scores) / len(all_scores), 1) if all_scores else 0.0

    # Risk level thresholds (provisional — no empirical cut-offs in v0.1 per Section 7)
    risk = "high" if overall >= 55 else ("moderate" if overall >= 30 else "low")

    # Safety check
    safety_alert, safety_flags = kompass_check_safety(responses)
    if safety_alert:
        risk = "high"

    referral_map = {
        "high":     "Significant indicators across multiple domains. Immediate clinician review required. Do not delay referral.",
        "moderate": "Elevated indicators in one or more domains. Clinician review recommended within 2–4 weeks.",
        "low":      "No significant indicators at this time. Continue routine monitoring and review.",
    }

    return {
        "core_score":          core_score,
        "score_overall":       overall,
        "risk_level":          risk,
        "referral_text":       referral_map[risk],
        "modules_triggered":   triggered,
        "per_module_scores":   per_module_scores,
        "not_observed_count":  not_observed,
        "safety_alert":        safety_alert,
        "safety_flags":        safety_flags,
    }


def handle_screening_submit(req):
    uid       = req.get("user_id")
    body      = req.get("body", {})
    record_id = body.get("record_id")
    responses = body.get("responses", {})
    age_band  = body.get("age_band", "8-12")
    modules_completed = body.get("modules_completed", [])

    if not record_id or not responses:
        return err("record_id and responses required", 400)
    if age_band not in KOMPASS_AGE_BANDS:
        age_band = "8-12"  # safe default

    db = get_db()
    rec = db.execute(
        "SELECT id, age_band FROM records WHERE id=? AND is_deleted=0", (record_id,)
    ).fetchone()
    if not rec:
        return err("Record not found", 404)

    # Use the record's stored age_band if not provided in body
    if not body.get("age_band") and rec["age_band"]:
        age_band = rec["age_band"]

    # Run Kompass engine
    result = kompass_score(responses, age_band)

    sid = str(uuid.uuid4())
    db.execute(
        """INSERT INTO screenings(
               id, record_id, screener_version, responses,
               score_overall, core_score, risk_level, referral_text,
               modules_triggered, modules_completed, per_module_scores,
               not_observed_count, safety_flags, safety_alert,
               age_band_at_screening, ai_flag, administered_by)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            sid, record_id, "kompass-1.0", json.dumps(responses),
            result["score_overall"], result["core_score"],
            result["risk_level"], result["referral_text"],
            json.dumps(result["modules_triggered"]),
            json.dumps(modules_completed),
            json.dumps(result["per_module_scores"]),
            result["not_observed_count"],
            json.dumps(result["safety_flags"]),
            1 if result["safety_alert"] else 0,
            age_band, 0, uid,
        )
    )

    # Update the record with domain information from triggered modules
    if result["per_module_scores"] or result["modules_triggered"]:
        top_module = (max(result["per_module_scores"], key=result["per_module_scores"].get)
                      if result["per_module_scores"] else
                      result["modules_triggered"][0] if result["modules_triggered"] else None)
        top_label  = KOMPASS_MODULE_TRIGGERS.get(top_module, {}).get("label", top_module) if top_module else None
        all_labels = [KOMPASS_MODULE_TRIGGERS.get(m, {}).get("label", m)
                      for m in result["modules_triggered"]]
        db.execute(
            "UPDATE records SET primary_domain=?, domain_tags=?, updated_at=? WHERE id=?",
            (top_label, json.dumps(all_labels),
             datetime.datetime.utcnow().isoformat(), record_id)
        )

    if result["risk_level"] == "high" or result["safety_alert"]:
        urgency = "immediate" if result["safety_alert"] else "urgent"
        db.execute(
            "INSERT INTO referrals(id,screening_id,referral_level,urgency,referred_by) VALUES(?,?,?,?,?)",
            (str(uuid.uuid4()), sid, "clinical_psychologist", urgency, uid)
        )

    db.commit()
    audit(uid, "SCREENING_SUBMIT", "screening", sid, req.get("ip"), {
        "record_id":     record_id,
        "risk":          result["risk_level"],
        "safety_alert":  result["safety_alert"],
        "modules":       result["modules_triggered"],
    })

    return ok({
        "screening_id":       sid,
        "risk_level":         result["risk_level"],
        "score_overall":      result["score_overall"],
        "core_score":         result["core_score"],
        "referral_text":      result["referral_text"],
        "modules_triggered":  result["modules_triggered"],
        "per_module_scores":  result["per_module_scores"],
        "not_observed_count": result["not_observed_count"],
        "safety_alert":       result["safety_alert"],
        "safety_flags":       result["safety_flags"],
        "non_diagnostic_notice": (
            "⚠ IMMEDIATE CLINICIAN REVIEW REQUIRED. Safety items endorsed."
            if result["safety_alert"] else
            "AI-assisted risk indicator only. Requires clinician interpretation within cultural context."
        ),
    }, status=201)

def handle_screening_validate(req, screening_id):
    uid  = req.get("user_id")
    body = req.get("body", {})
    v_risk = body.get("validated_risk")
    if v_risk not in ["low", "moderate", "high"]:
        return err("validated_risk must be low|moderate|high", 400)
    db = get_db()
    if not db.execute("SELECT id FROM screenings WHERE id=?", (screening_id,)).fetchone():
        return err("Screening not found", 404)
    vid = str(uuid.uuid4())
    db.execute(
        """INSERT INTO clinician_validations
           (id,screening_id,clinician_id,validated_risk,primary_concern,
            secondary_concern,confidence_level,notes)
           VALUES(?,?,?,?,?,?,?,?)""",
        (vid, screening_id, uid, v_risk, body.get("primary_concern"),
         body.get("secondary_concern"), body.get("confidence_level"),
         body.get("notes"))
    )
    db.commit()
    audit(uid, "CLINICIAN_VALIDATE", "validation", vid, req.get("ip"))
    return ok({"validation_id": vid})

def handle_consents_list(req):
    db  = get_db()
    params = req.get("params", {})
    rid    = params.get("record_id", [None])[0]
    status = params.get("status",    [None])[0]
    where, args = ["1=1"], []
    if rid:    where.append("record_id=?"); args.append(rid)
    if status: where.append("status=?");   args.append(status)
    rows = db.execute(
        f"SELECT * FROM consents WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT 100",
        args
    ).fetchall()
    return ok(rows_to_list(rows))

def handle_consent_create(req):
    uid  = req.get("user_id")
    body = req.get("body", {})
    ct   = body.get("consent_type")
    if ct not in ["child_assent", "parent_guardian", "teacher_clinician"]:
        return err("Invalid consent_type", 400)
    fp_content = json.dumps({**body, "ts": datetime.datetime.utcnow().isoformat()}, sort_keys=True)
    fp = hashlib.sha256(fp_content.encode()).hexdigest()
    cid = str(uuid.uuid4())
    db  = get_db()
    db.execute(
        """INSERT INTO consents(id,record_id,consent_type,form_version,language,
           site_code,status,hash_fingerprint,created_by)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (cid, body.get("record_id"), ct, body.get("form_version", "2.1"),
         body.get("language", "English"), body.get("site_code"), "pending", fp, uid)
    )
    db.commit()
    audit(uid, "CONSENT_CREATE", "consent", cid, req.get("ip"))
    return ok({"consent_id": cid, "hash_fingerprint": fp,
               "notice": "Stored separately from assessment data per governance protocol."}, status=201)

def handle_consent_sign(req, consent_id):
    uid = req.get("user_id")
    db  = get_db()
    if not db.execute("SELECT id FROM consents WHERE id=?", (consent_id,)).fetchone():
        return err("Consent not found", 404)
    db.execute("UPDATE consents SET status='signed', signed_at=datetime('now') WHERE id=?", (consent_id,))
    db.commit()
    audit(uid, "CONSENT_SIGN", "consent", consent_id, req.get("ip"))
    return ok({"consent_id": consent_id, "status": "signed"})

def handle_analytics_kappa(req):
    uid = req.get("user_id")
    db  = get_db()
    sf, sf_args = _site_filter(uid, db, "r") if uid else ("", [])
    pairs = db.execute(f"""
        SELECT s.risk_level as ai_risk, cv.validated_risk as cli_risk,
               r.parish, r.setting, r.age_band
        FROM screenings s
        JOIN clinician_validations cv ON cv.screening_id=s.id
        JOIN records r ON r.id=s.record_id
        WHERE cv.kappa_eligible=1 AND r.is_deleted=0{sf}
    """, sf_args).fetchall()
    kappa  = _compute_kappa(pairs)
    levels = ["low","moderate","high"]
    matrix = {a: {b: 0 for b in levels} for a in levels}
    for row in pairs:
        a, b = row["ai_risk"], row["cli_risk"]
        if a in levels and b in levels:
            matrix[a][b] += 1
    by_parish  = {}
    by_setting = {}
    for row in pairs:
        p, s = row["parish"], row["setting"]
        if p not in by_parish:  by_parish[p]  = []
        if s not in by_setting: by_setting[s] = []
        by_parish[p].append(row)
        by_setting[s].append(row)
    return ok({
        "kappa_overall":    kappa,
        "n_pairs":          len(pairs),
        "interpretation":   "Substantial" if kappa>=0.61 else "Moderate" if kappa>=0.41 else "Fair",
        "confusion_matrix": matrix,
        "kappa_by_parish":  {p: _compute_kappa(v) for p, v in by_parish.items()},
        "kappa_by_setting": {s: _compute_kappa(v) for s, v in by_setting.items()},
    })

def handle_analytics_bias(req):
    uid  = req.get("user_id")
    db   = get_db()
    sf, sf_args = _site_filter(uid, db) if uid else ("", [])  # no alias — bare table queries
    rural = ["Portland","St. Elizabeth","St. Ann","Westmoreland"]
    total = db.execute(f"SELECT COUNT(*) as c FROM records WHERE is_deleted=0{sf}", sf_args).fetchone()["c"]
    rc    = db.execute(
        f"SELECT COUNT(*) as c FROM records WHERE is_deleted=0 AND parish IN ({','.join(['?']*len(rural))}){sf}",
        rural + sf_args
    ).fetchone()["c"]
    deaf  = db.execute(
        f"SELECT COUNT(*) as c FROM records WHERE setting='deaf-inclusive' AND is_deleted=0{sf}",
        sf_args
    ).fetchone()["c"]
    latest = db.execute("SELECT * FROM bias_audits ORDER BY audit_date DESC LIMIT 1").fetchone()
    return ok({
        "live_stats":   {"rural_count": rc, "total_records": total,
                         "rural_pct": round(rc/total*100,1) if total else 0,
                         "deaf_inclusive_count": deaf},
        "latest_audit": dict(latest) if latest else None,
        "recommendations": [
            "Expand data collection from Portland, St. Elizabeth, and Westmoreland",
            "Increase deaf-inclusive site participation before Phase 3",
            "Review ADHD indicators for gender bias (currently 58% male-coded referrals)",
            "Validate model separately for 15–18 age band (lower n)",
        ],
    })

def handle_analytics_psychometrics(req):
    uid = req.get("user_id")
    db  = get_db()
    sf, sf_args = _site_filter(uid, db, "r") if uid else ("", [])
    rows = db.execute(f"""
        SELECT s.score_attention, s.score_asd, s.score_emotional, s.score_trauma,
               s.score_overall, s.risk_level
        FROM screenings s
        JOIN records r ON r.id=s.record_id
        WHERE s.score_overall IS NOT NULL AND r.is_deleted=0{sf}
    """, sf_args).fetchall()
    if not rows:
        return ok({"message": "No screening data yet"})
    def stats(col):
        vals = sorted([r[col] for r in rows if r[col] is not None])
        if not vals: return {}
        n  = len(vals); mu = sum(vals)/n
        sd = (sum((x-mu)**2 for x in vals)/n)**0.5
        return {"n": n, "mean": round(mu,2), "sd": round(sd,2),
                "min": round(vals[0],2), "max": round(vals[-1],2),
                "p25": round(vals[n//4],2), "median": round(vals[n//2],2),
                "p75": round(vals[3*n//4],2)}
    rc = {"low": 0, "moderate": 0, "high": 0}
    for r in rows:
        if r["risk_level"]: rc[r["risk_level"]] = rc.get(r["risk_level"],0)+1
    return ok({
        "domain_stats": {
            "attention": stats("score_attention"), "asd": stats("score_asd"),
            "emotional": stats("score_emotional"), "trauma": stats("score_trauma"),
            "overall":   stats("score_overall"),
        },
        "risk_distribution": rc,
        "n_screenings": len(rows),
    })

def handle_phases_list(req):
    db   = get_db()
    rows = db.execute("SELECT * FROM phase_tasks ORDER BY phase_number, id").fetchall()
    by_phase = {}
    for row in rows:
        p = row["phase_number"]
        if p not in by_phase: by_phase[p] = []
        by_phase[p].append(dict(row))
    return ok(by_phase)

def handle_phase_task_update(req, task_id):
    uid  = req.get("user_id")
    body = req.get("body", {})
    status = body.get("status")
    if status not in ["pending","in_progress","complete","blocked"]:
        return err("Invalid status", 400)
    db = get_db()
    db.execute(
        "UPDATE phase_tasks SET status=?, updated_at=datetime('now'), notes=? WHERE id=?",
        (status, body.get("notes"), task_id)
    )
    db.commit()
    audit(uid, "PHASE_TASK_UPDATE", "phase_task", task_id, req.get("ip"))
    return ok({"task_id": task_id, "status": status})

def handle_referrals_list(req):
    db = get_db()
    rows = db.execute("""
        SELECT rf.*, r.id as record_id, r.parish, r.age_band, r.setting,
               s.risk_level, s.score_overall
        FROM referrals rf
        JOIN screenings s ON s.id=rf.screening_id
        JOIN records r ON r.id=s.record_id
        ORDER BY rf.referred_at DESC LIMIT 100
    """).fetchall()
    return ok(rows_to_list(rows))

def handle_export_csv(req):
    uid  = req.get("user_id")
    db   = get_db()
    sf, sf_args = _site_filter(uid, db, "r") if uid else ("", [])
    rows = db.execute(f"""
        SELECT r.id, r.age_band, r.parish, r.setting, r.rated_by,
               r.instruments, r.primary_domain, r.secondary_domain,
               r.phase, r.data_source, r.gender_code, r.year_of_assessment,
               r.school_type, r.site_code, r.created_at,
               s.risk_level, s.score_attention, s.score_asd,
               s.score_emotional, s.score_trauma, s.score_overall,
               cv.validated_risk, cv.confidence_level
        FROM records r
        LEFT JOIN screenings s ON s.record_id=r.id
        LEFT JOIN clinician_validations cv ON cv.screening_id=s.id
        WHERE r.is_deleted=0{sf}
        ORDER BY r.created_at DESC
    """, sf_args).fetchall()
    buf = io.StringIO()
    w   = csv.writer(buf)
    w.writerow(["study_id","age_band","parish","setting","rated_by","instruments",
                "primary_domain","secondary_domain","phase","data_source",
                "gender_code","year_of_assessment","school_type","created_at",
                "ai_risk_level","score_attention","score_asd","score_emotional",
                "score_trauma","score_overall","clinician_validated_risk",
                "clinician_confidence"])
    for r in rows:
        w.writerow([r["id"],r["age_band"],r["parish"],r["setting"],r["rated_by"],
                    r["instruments"],r["primary_domain"],r["secondary_domain"],
                    r["phase"],r["data_source"],r["gender_code"],
                    r["year_of_assessment"],r["school_type"],r["created_at"],
                    r["risk_level"],r["score_attention"],r["score_asd"],
                    r["score_emotional"],r["score_trauma"],r["score_overall"],
                    r["validated_risk"],r["confidence_level"]])
    csv_data = buf.getvalue()
    fn = f"ded_export_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    (EXPORT_DIR / fn).write_text(csv_data, encoding="utf-8")
    audit(uid, "EXPORT_CSV", "export", fn, req.get("ip"), {"rows": len(rows)})
    return ok({"filename": fn, "records": len(rows), "csv_data": csv_data,
               "notice": "De-identified data only. For authorised research use."})

# ══ AI CONVERSATION ENDPOINTS ═════════════════════════════════════

def handle_ai_chat(req):
    """
    Main Claude AI endpoint.
    Receives a message, pulls relevant DB data, sends to Claude, stores conversation.
    """
    uid  = req.get("user_id")
    if not uid:
        return err("Not authenticated", 401)

    body    = req.get("body", {})
    message = body.get("message", "").strip()
    conv_id = body.get("conversation_id")
    api_key = body.get("api_key") or ANTHROPIC_API_KEY

    if not message:
        return err("message is required", 400)

    db = get_db()
    sf, sf_args = _site_filter(uid, db)

    # Get or create conversation
    if conv_id:
        conv = db.execute(
            "SELECT * FROM ai_conversations WHERE id=? AND user_id=?",
            (conv_id, uid)
        ).fetchone()
        if not conv:
            return err("Conversation not found", 404)
    else:
        conv_id   = str(uuid.uuid4())
        title     = message[:60] + ("…" if len(message) > 60 else "")
        # Record count scoped to the user's site
        rec_count = db.execute(
            f"SELECT COUNT(*) as c FROM records WHERE is_deleted=0{sf}", sf_args
        ).fetchone()["c"]
        db.execute(
            """INSERT INTO ai_conversations
               (id,user_id,title,context_type,record_count_at_time,model_used)
               VALUES(?,?,?,?,?,?)""",
            (conv_id, uid, title, body.get("context_type","general"),
             rec_count, CLAUDE_MODEL)
        )

    # Load message history for this conversation (last 10 exchanges)
    history = db.execute(
        "SELECT role, content FROM ai_messages WHERE conversation_id=? ORDER BY id DESC LIMIT 20",
        (conv_id,)
    ).fetchall()
    messages = list(reversed([{"role": r["role"], "content": r["content"]} for r in history]))
    messages.append({"role": "user", "content": message})

    # Call Claude with DB context — scoped to the user's site
    reply, tokens, data_ctx = call_claude(messages, db, message, api_key,
                                           site_filter=sf, site_args=sf_args)

    # Store both messages
    db.execute(
        "INSERT INTO ai_messages(conversation_id,role,content,data_context) VALUES(?,?,?,?)",
        (conv_id, "user", message, None)
    )
    db.execute(
        "INSERT INTO ai_messages(conversation_id,role,content,tokens_used,data_context) VALUES(?,?,?,?,?)",
        (conv_id, "assistant", reply, tokens, data_ctx)
    )
    db.execute(
        """UPDATE ai_conversations
           SET last_message_at=datetime('now'),
               message_count=message_count+1
           WHERE id=?""",
        (conv_id,)
    )
    db.commit()
    audit(uid, "AI_CHAT", "ai_conversation", conv_id, req.get("ip"),
          {"tokens": tokens, "has_api_key": bool(api_key)})

    return ok({
        "conversation_id": conv_id,
        "reply":           reply,
        "tokens_used":     tokens,
        "model":           CLAUDE_MODEL,
    }, status=200)

def handle_ai_conversations_list(req):
    uid = req.get("user_id")
    if not uid:
        return err("Not authenticated", 401)
    db   = get_db()
    rows = db.execute(
        """SELECT id, title, context_type, started_at, last_message_at,
                  message_count, model_used
           FROM ai_conversations
           WHERE user_id=? AND is_archived=0
           ORDER BY last_message_at DESC LIMIT 50""",
        (uid,)
    ).fetchall()
    return ok(rows_to_list(rows))

def handle_ai_conversation_history(req, conv_id):
    uid = req.get("user_id")
    if not uid:
        return err("Not authenticated", 401)
    db = get_db()
    conv = db.execute(
        "SELECT * FROM ai_conversations WHERE id=? AND user_id=?", (conv_id, uid)
    ).fetchone()
    if not conv:
        return err("Conversation not found", 404)
    msgs = db.execute(
        "SELECT role, content, tokens_used, created_at FROM ai_messages WHERE conversation_id=? ORDER BY id",
        (conv_id,)
    ).fetchall()
    return ok({"conversation": dict(conv), "messages": rows_to_list(msgs)})

def handle_ai_query_templates(req):
    db   = get_db()
    rows = db.execute(
        "SELECT id,name,description,category,natural_language FROM ai_query_templates WHERE is_active=1 ORDER BY category,name"
    ).fetchall()
    return ok(rows_to_list(rows))

def handle_audit_log(req):
    db   = get_db()
    rows = db.execute("""
        SELECT al.*, u.username FROM audit_log al
        LEFT JOIN users u ON u.id=al.user_id
        ORDER BY al.timestamp DESC LIMIT 200
    """).fetchall()
    return ok(rows_to_list(rows))

# ══════════════════════════════════════════════════════════════════
# HTTP HANDLER
# ══════════════════════════════════════════════════════════════════
CORS_HEADERS = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-API-Version",
    "Access-Control-Max-Age":       "86400",
}

ROUTES = []

def route(method, pattern):
    def dec(fn):
        regex = re.compile("^" + re.sub(r":(\w+)", r"(?P<\1>[^/]+)", pattern) + "$")
        ROUTES.append((method, regex, fn))
        return fn
    return dec

@route("GET",   "/api/health")
def r_health(req, **k):              return handle_health(req)
@route("POST",  "/api/auth/login")
def r_login(req, **k):               return handle_login(req)
@route("POST",  "/api/auth/logout")
def r_logout(req, **k):              return handle_logout(req)
@route("GET",   "/api/auth/me")
def r_me(req, **k):                  return handle_me(req)
@route("GET",   "/api/dashboard/stats")
def r_stats(req, **k):               return handle_dashboard_stats(req)
@route("GET",   "/api/records")
def r_recs(req, **k):                return handle_records_list(req)
@route("POST",  "/api/records")
def r_rec_c(req, **k):               return handle_record_create(req)
@route("GET",   "/api/records/:id")
def r_rec_g(req, id, **k):           return handle_record_get(req, id)

@route("POST",  "/api/records/:id/domain")
def r_rec_domain(req, id, **k):
    uid  = req.get("user_id")
    body = req.get("body", {})
    dom  = body.get("primary_domain")
    if not dom:
        return err("primary_domain required", 400)
    db = get_db()
    db.execute("UPDATE records SET primary_domain=?, updated_at=? WHERE id=? AND is_deleted=0",
               (dom, datetime.datetime.utcnow().isoformat(), id))
    db.commit()
    return ok({"id": id, "primary_domain": dom})

@route("POST",  "/api/screenings")
def r_scr(req, **k):                 return handle_screening_submit(req)
@route("POST",  "/api/screenings/:id/validate")
def r_val(req, id, **k):             return handle_screening_validate(req, id)
@route("GET",   "/api/consents")
def r_cons_l(req, **k):              return handle_consents_list(req)
@route("POST",  "/api/consents")
def r_cons_c(req, **k):              return handle_consent_create(req)
@route("PATCH", "/api/consents/:id/sign")
def r_cons_s(req, id, **k):          return handle_consent_sign(req, id)
@route("GET",   "/api/analytics/kappa")
def r_kappa(req, **k):               return handle_analytics_kappa(req)
@route("GET",   "/api/analytics/bias")
def r_bias(req, **k):                return handle_analytics_bias(req)
@route("GET",   "/api/analytics/psychometrics")
def r_psych(req, **k):               return handle_analytics_psychometrics(req)
@route("GET",   "/api/phases")
def r_phases(req, **k):              return handle_phases_list(req)
@route("PATCH", "/api/phases/tasks/:id")
def r_ptask(req, id, **k):           return handle_phase_task_update(req, id)
@route("GET",   "/api/referrals")
def r_refs(req, **k):                return handle_referrals_list(req)
@route("GET",   "/api/export/csv")
def r_export(req, **k):              return handle_export_csv(req)
@route("GET",   "/api/audit-log")
def r_audit(req, **k):               return handle_audit_log(req)
# AI endpoints
@route("POST",  "/api/ai/chat")
def r_ai_chat(req, **k):             return handle_ai_chat(req)

@route("POST",  "/api/ai/chat/stream")
def r_ai_chat_stream(req, **k):
    h = req.get("_handler")  # DEDHandler instance
    uid = req.get("user_id")
    if not uid:
        return err("Not authenticated", 401)

    body    = req.get("body", {})
    message = body.get("message", "").strip()
    conv_id = body.get("conversation_id")
    api_key = body.get("api_key") or ANTHROPIC_API_KEY
    log.info("[stream] api_key=%s", api_key[:12] + "..." if api_key else "EMPTY")

    if not message:
        return err("message is required", 400)

    db = get_db()
    sf, sf_args = _site_filter(uid, db)

    # Get or create conversation
    if conv_id:
        conv = db.execute(
            "SELECT * FROM ai_conversations WHERE id=? AND user_id=?",
            (conv_id, uid)
        ).fetchone()
        if not conv:
            return err("Conversation not found", 404)
    else:
        conv_id = str(uuid.uuid4())
        title   = message[:60] + ("…" if len(message) > 60 else "")
        rec_count = db.execute(
            f"SELECT COUNT(*) as c FROM records WHERE is_deleted=0{sf}", sf_args
        ).fetchone()["c"]
        db.execute(
            """INSERT INTO ai_conversations
               (id,user_id,title,context_type,record_count_at_time,model_used)
               VALUES(?,?,?,?,?,?)""",
            (conv_id, uid, title, body.get("context_type","general"),
             rec_count, CLAUDE_MODEL)
        )

    # Load message history
    history = db.execute(
        "SELECT role, content FROM ai_messages WHERE conversation_id=? ORDER BY id DESC LIMIT 20",
        (conv_id,)
    ).fetchall()
    messages = list(reversed([{"role": r["role"], "content": r["content"]} for r in history]))
    messages.append({"role": "user", "content": message})

    # Build augmented message with DB context
    try:
        data_ctx = _get_db_context_for_query(message, db,
                                              site_filter=sf, site_args=sf_args)
    except Exception as e:
        log.warning("DB context fetch error: %s", e)
        data_ctx = {}
    ctx_json = json.dumps(data_ctx, indent=2, default=str)
    augmented_msg = (
        f"{message}\n\n"
        f"---\n"
        f"DATABASE CONTEXT (live data as of {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}):\n"
        f"```json\n{ctx_json}\n```\n"
        f"Use this data to answer the question with specific numbers."
    )
    api_messages = []
    for m2 in messages[:-1]:
        api_messages.append({"role": m2["role"], "content": m2["content"]})
    api_messages.append({"role": "user", "content": augmented_msg})

    payload = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": CLAUDE_MAX_TOKENS,
        "system": CLAUDE_SYSTEM_PROMPT,
        "messages": api_messages,
        "stream": True,
    }).encode()

    api_req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type":       "application/json",
            "x-api-key":          api_key,
            "anthropic-version":  "2023-06-01",
        },
        method="POST"
    )

    # --- Send SSE headers ---
    h.send_response(200)
    for k2, v2 in CORS_HEADERS.items():
        h.send_header(k2, v2)
    h.send_header("Content-Type",  "text/event-stream; charset=utf-8")
    h.send_header("Cache-Control", "no-cache")
    h.send_header("X-Accel-Buffering", "no")
    h.end_headers()

    # Disable Nagle's algorithm so every flush goes out immediately
    import socket as _socket
    try:
        h.connection.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
    except Exception:
        pass

    def sse(event, data):
        line = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        try:
            h.wfile.write(line.encode())
            h.wfile.flush()
        except Exception:
            pass  # client disconnected

    # Send conversation id first so the frontend can store it
    sse("conv", {"conversation_id": conv_id})

    full_reply = []
    tokens_used = 0

    try:
        with urllib.request.urlopen(api_req, timeout=120) as resp:
            for raw_line in resp:
                line = raw_line.decode().rstrip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    ev = json.loads(chunk)
                except Exception:
                    continue
                ev_type = ev.get("type", "")
                if ev_type == "content_block_delta":
                    delta = ev.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        full_reply.append(text)
                        sse("token", {"text": text})
                elif ev_type == "message_delta":
                    tokens_used = ev.get("usage", {}).get("output_tokens", 0)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        log.error("Claude stream API error %d: %s", e.code, err_body)
        if e.code == 401:
            sse("error", {"message": "Invalid API key."})
        elif e.code == 429:
            sse("error", {"message": "Rate limit reached. Please wait a moment."})
        else:
            sse("error", {"message": f"API error ({e.code})"})
        sse("done", {"ok": False})
        return None
    except Exception as e:
        log.error("Claude stream failed: %s", e)
        sse("error", {"message": f"Connection error: {str(e)}"})
        sse("done", {"ok": False})
        return None

    reply = "".join(full_reply) or "No response generated."

    # Save to DB
    try:
        db.execute(
            "INSERT INTO ai_messages(conversation_id,role,content,data_context) VALUES(?,?,?,?)",
            (conv_id, "user", message, None)
        )
        db.execute(
            "INSERT INTO ai_messages(conversation_id,role,content,tokens_used,data_context) VALUES(?,?,?,?,?)",
            (conv_id, "assistant", reply, tokens_used, ctx_json)
        )
        db.execute(
            """UPDATE ai_conversations
               SET last_message_at=datetime('now'), message_count=message_count+1
               WHERE id=?""",
            (conv_id,)
        )
        db.commit()
    except Exception as db_err:
        log.error("Stream handler DB save error: %s", db_err)

    sse("done", {"ok": True, "tokens": tokens_used})
    return None  # raw response already written
@route("GET",   "/api/ai/conversations")
def r_ai_convs(req, **k):            return handle_ai_conversations_list(req)
@route("GET",   "/api/ai/conversations/:id")
def r_ai_conv(req, id, **k):         return handle_ai_conversation_history(req, id)
@route("GET",   "/api/ai/query-templates")
def r_ai_qtpl(req, **k):             return handle_ai_query_templates(req)


class DEDHandler(http.server.BaseHTTPRequestHandler):
    server_version = "KOMPASS-API/3.0"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def send_json(self, status, data):
        body = json.dumps(data, default=str, indent=2).encode()
        self.send_response(status)
        for k, v in CORS_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("X-API-Version", "3.0.0")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        for k, v in CORS_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()

    def _dispatch(self, method):
        ip     = self.client_address[0]
        if not _check_rate(ip):
            self.send_json(429, {"status":"error","message":"Rate limit exceeded"})
            return
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)
        auth   = self.headers.get("Authorization", "")
        token  = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else None
        user_id = _verify_token(token) if token else None
        body   = {}
        clen   = int(self.headers.get("Content-Length", 0))
        if clen > 0:
            try:
                body = json.loads(self.rfile.read(clen))
            except Exception:
                self.send_json(*err("Invalid JSON body", 400))
                return
        req = {"method": method, "path": path, "params": params,
               "body": body, "token": token, "user_id": user_id, "ip": ip,
               "_handler": self}   # expose raw handler for SSE routes
        for r_method, regex, handler in ROUTES:
            if r_method != method:
                continue
            m = regex.match(path)
            if m:
                try:
                    result = handler(req, **m.groupdict())
                    if result is None:
                        return  # SSE / raw-write handler already responded
                    self.send_json(*result)
                except Exception as e:
                    import traceback as _tb
                    tb = _tb.format_exc()
                    log.exception("Handler error: %s", e)
                    self.send_json(500, {
                        "status":  "error",
                        "message": "Internal server error",
                        "detail":  str(e),
                        "trace":   tb.splitlines()[-5:],  # last 5 lines of traceback
                    })
                return
        self.send_json(404, {"status":"error","message":f"No route: {method} {path}"})

    def _serve_static(self, path):
        """Serve static files from the static/ directory."""
        if path in ("/", "/index.html"):
            file_path = BASE_DIR / "static" / "index.html"
        else:
            rel = path.lstrip("/")
            file_path = BASE_DIR / "static" / rel

        if not file_path.exists() or not file_path.is_file():
            return False

        # Prevent path traversal
        try:
            file_path.resolve().relative_to((BASE_DIR / "static").resolve())
        except ValueError:
            return False

        MIME = {
            ".html": "text/html; charset=utf-8",
            ".css":  "text/css",
            ".js":   "application/javascript",
            ".png":  "image/png",
            ".jpg":  "image/jpeg",
            ".svg":  "image/svg+xml",
            ".ico":  "image/x-icon",
        }
        mime = MIME.get(file_path.suffix.lower(), "application/octet-stream")
        data = file_path.read_bytes()

        self.send_response(200)
        for k, v in CORS_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)
        return True

    def do_GET(self):
        # Serve static files for any non-API path
        parsed_path = self.path.split("?")[0]
        if not parsed_path.startswith("/api/"):
            if self._serve_static(parsed_path):
                return
        self._dispatch("GET")

    def do_POST(self):   self._dispatch("POST")
    def do_PATCH(self):  self._dispatch("PATCH")
    def do_PUT(self):    self._dispatch("PUT")
    def do_DELETE(self): self._dispatch("DELETE")


START_TIME = time.time()

def main():
    log.info("=" * 60)
    log.info("  KAHLE Psych Kompass · v3.0.0")
    log.info("  Ethics Ref: MOH-JA-2025-0047")
    log.info("  Claude AI: %s", "configured ✓" if ANTHROPIC_API_KEY else "not configured (set ANTHROPIC_API_KEY)")
    log.info("  Model: %s", CLAUDE_MODEL)
    log.info("=" * 60)
    init_db()
    server = http.server.ThreadingHTTPServer((HOST, PORT), DEDHandler)
    log.info("Server running → http://localhost:%d", PORT)
    log.info("Endpoints: %d routes registered", len(ROUTES))
    log.info("Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        server.shutdown()

if __name__ == "__main__":
    main()
