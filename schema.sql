-- ═══════════════════════════════════════════════════════════════
-- KAHLE Psych Kompass · Database Schema v3.0
-- Dr. KAD Morgan & Associates · Jamaica
-- Ethics Ref: MOH-JA-2025-0047
-- Optimised for 5,000+ records with Claude AI integration
-- ═══════════════════════════════════════════════════════════════

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=NORMAL;
PRAGMA cache_size=-64000;   -- 64 MB cache for large datasets
PRAGMA temp_store=MEMORY;

-- ── USERS ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    role          TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    full_name     TEXT NOT NULL,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_login    DATETIME,
    is_active     INTEGER DEFAULT 1,
    -- Site scoping: NULL = admin/PI (sees all sites)
    site_code     TEXT,   -- e.g. SITE-01, SITE-02
    site_name     TEXT,   -- e.g. "Kingston Clinic", "Portmore Primary School"
    -- Claude API key stored per user (encrypted at app level)
    claude_api_key_hash TEXT,
    preferences   TEXT DEFAULT '{}'  -- JSON: UI preferences
);

-- ── SESSIONS ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    expires_at  DATETIME NOT NULL,
    ip_address  TEXT,
    user_agent  TEXT
);

-- ── RECORDS (De-identified) ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS records (
    id              TEXT PRIMARY KEY,
    age_band        TEXT NOT NULL,
    parish          TEXT NOT NULL,
    setting         TEXT NOT NULL,
    rated_by        TEXT NOT NULL,
    instruments     TEXT NOT NULL DEFAULT '[]',  -- JSON array
    primary_domain  TEXT,
    secondary_domain TEXT,
    domain_tags     TEXT DEFAULT '[]',  -- JSON array of all Kompass module labels triggered
    phase           INTEGER NOT NULL DEFAULT 1,
    data_source     TEXT DEFAULT 'archival',
    gender_code     TEXT,      -- M/F/NB/U — coded, not identifying
    year_of_assessment INTEGER,
    school_type     TEXT,      -- primary/secondary/special/clinic
    referral_reason TEXT,
    consent_obtained INTEGER DEFAULT 0,
    site_code       TEXT,      -- which site created this record (NULL = archival/unassigned)
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    created_by      TEXT REFERENCES users(id),
    updated_at      DATETIME,
    notes           TEXT,
    is_deleted      INTEGER DEFAULT 0  -- soft delete
);

-- ── SCREENINGS ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS screenings (
    id               TEXT PRIMARY KEY,
    record_id        TEXT NOT NULL REFERENCES records(id),
    screener_version TEXT NOT NULL DEFAULT 'kompass-1.0',
    responses        TEXT NOT NULL DEFAULT '{}',  -- JSON: all item responses keyed by item code
    -- Legacy subscores (kept for archival data compatibility)
    score_attention  REAL,
    score_asd        REAL,
    score_emotional  REAL,
    score_trauma     REAL,
    score_social     REAL,
    -- Kompass scoring fields
    score_overall    REAL,
    core_score       REAL,   -- mean of endorsed Core items
    risk_level       TEXT,   -- low / moderate / high
    referral_text    TEXT,
    -- Kompass module tracking
    modules_triggered  TEXT DEFAULT '[]',  -- JSON array: which modules Core opened
    modules_completed  TEXT DEFAULT '[]',  -- JSON array: which modules caregiver finished
    per_module_scores  TEXT DEFAULT '{}',  -- JSON: {module_code: score, ...}
    not_observed_count INTEGER DEFAULT 0,  -- number of ? responses (not scored as 0)
    -- Safety
    safety_flags     TEXT DEFAULT '[]',    -- JSON array of endorsed safety item codes
    safety_alert     INTEGER DEFAULT 0,    -- 1 = immediate clinician review required
    -- Meta
    age_band_at_screening TEXT,            -- age band used for item routing
    ai_flag          INTEGER DEFAULT 0,
    completed_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    administered_by  TEXT REFERENCES users(id),
    duration_seconds INTEGER,
    is_complete      INTEGER DEFAULT 1
);

-- ── CLINICIAN VALIDATIONS ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS clinician_validations (
    id              TEXT PRIMARY KEY,
    screening_id    TEXT NOT NULL REFERENCES screenings(id),
    clinician_id    TEXT NOT NULL REFERENCES users(id),
    validated_risk  TEXT,
    primary_concern TEXT,
    secondary_concern TEXT,
    kappa_eligible  INTEGER DEFAULT 1,
    confidence_level INTEGER,  -- 1-5 clinician confidence rating
    notes           TEXT,
    validated_at    DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── INSTRUMENT SCORES ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS instrument_scores (
    id              TEXT PRIMARY KEY,
    record_id       TEXT NOT NULL REFERENCES records(id),
    instrument      TEXT NOT NULL,
    subscale        TEXT,
    raw_score       REAL,
    t_score         REAL,
    percentile      REAL,
    classification  TEXT,   -- Average/At-Risk/Clinically Significant
    administered_at DATE,
    notes           TEXT
);

-- ── CONSENTS ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS consents (
    id              TEXT PRIMARY KEY,
    record_id       TEXT REFERENCES records(id),
    consent_type    TEXT NOT NULL,
    form_version    TEXT NOT NULL DEFAULT '2.1',
    language        TEXT DEFAULT 'English',
    site_code       TEXT,
    status          TEXT DEFAULT 'pending',
    signed_at       DATETIME,
    withdrawn_at    DATETIME,
    hash_fingerprint TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    created_by      TEXT REFERENCES users(id),
    stored_separately INTEGER DEFAULT 1
);

-- ── REFERRALS ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS referrals (
    id              TEXT PRIMARY KEY,
    screening_id    TEXT NOT NULL REFERENCES screenings(id),
    referral_level  TEXT NOT NULL,
    urgency         TEXT DEFAULT 'routine',
    status          TEXT DEFAULT 'pending',
    referred_by     TEXT REFERENCES users(id),
    referred_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    completed_at    DATETIME,
    outcome_notes   TEXT
);

-- ── BIAS AUDITS ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bias_audits (
    id              TEXT PRIMARY KEY,
    audit_date      DATE NOT NULL,
    total_records   INTEGER,
    rural_count     INTEGER,
    urban_count     INTEGER,
    rural_pct       REAL,
    deaf_count      INTEGER,
    blind_count      INTEGER,
    gender_m_count  INTEGER,
    gender_f_count  INTEGER,
    kappa_overall   REAL,
    kappa_rural     REAL,
    kappa_sensory   REAL,
    kappa_by_parish TEXT,   -- JSON
    notes           TEXT,
    auditor_id      TEXT REFERENCES users(id),
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── PHASE TASKS ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS phase_tasks (
    id              TEXT PRIMARY KEY,
    phase_number    INTEGER NOT NULL,
    task_name       TEXT NOT NULL,
    status          TEXT DEFAULT 'pending',
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_by      TEXT,
    notes           TEXT
);

-- ── CLAUDE AI CONVERSATIONS ────────────────────────────────────────
-- Each conversation is a session of Q&A with Claude about the data
CREATE TABLE IF NOT EXISTS ai_conversations (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    title           TEXT,                  -- auto-generated from first message
    context_type    TEXT DEFAULT 'general',-- general/record/screening/analytics
    context_id      TEXT,                  -- optional: record_id or screening_id
    record_count_at_time INTEGER,          -- how many records Claude could see
    started_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_message_at DATETIME,
    message_count   INTEGER DEFAULT 0,
    is_archived     INTEGER DEFAULT 0,
    model_used      TEXT DEFAULT 'claude-sonnet-4-6'
);

-- ── CLAUDE AI MESSAGES ────────────────────────────────────────────
-- Full message history for each conversation
CREATE TABLE IF NOT EXISTS ai_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES ai_conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,         -- user / assistant
    content         TEXT NOT NULL,         -- full message text
    tokens_used     INTEGER,
    data_context    TEXT,                  -- JSON: what DB data was sent
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── AI QUERY TEMPLATES ────────────────────────────────────────────
-- Pre-built smart queries that Claude can run on the database
CREATE TABLE IF NOT EXISTS ai_query_templates (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT,
    category        TEXT,                  -- analytics/screening/bias/referral
    sql_template    TEXT NOT NULL,         -- parameterised SQL
    natural_language TEXT,                 -- what this query answers
    is_active       INTEGER DEFAULT 1,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── AUDIT LOG (immutable) ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       DATETIME DEFAULT CURRENT_TIMESTAMP,
    user_id         TEXT,
    action          TEXT NOT NULL,
    resource_type   TEXT,
    resource_id     TEXT,
    ip_address      TEXT,
    details         TEXT       -- JSON
);

-- ═══════════════════════════════════════════════════════════════
-- INDEXES — critical for performance at 5000+ records
-- ═══════════════════════════════════════════════════════════════

CREATE INDEX IF NOT EXISTS idx_records_parish      ON records(parish)         WHERE is_deleted=0;
CREATE INDEX IF NOT EXISTS idx_records_phase       ON records(phase)          WHERE is_deleted=0;
CREATE INDEX IF NOT EXISTS idx_records_domain      ON records(primary_domain) WHERE is_deleted=0;
CREATE INDEX IF NOT EXISTS idx_records_age         ON records(age_band)       WHERE is_deleted=0;
CREATE INDEX IF NOT EXISTS idx_records_setting     ON records(setting)        WHERE is_deleted=0;
CREATE INDEX IF NOT EXISTS idx_records_created     ON records(created_at)     WHERE is_deleted=0;
CREATE INDEX IF NOT EXISTS idx_records_source      ON records(data_source)    WHERE is_deleted=0;
CREATE INDEX IF NOT EXISTS idx_records_site        ON records(site_code)      WHERE is_deleted=0;
CREATE INDEX IF NOT EXISTS idx_screenings_record   ON screenings(record_id);
CREATE INDEX IF NOT EXISTS idx_screenings_risk     ON screenings(risk_level);
CREATE INDEX IF NOT EXISTS idx_screenings_date     ON screenings(completed_at);
CREATE INDEX IF NOT EXISTS idx_validations_screen  ON clinician_validations(screening_id);
CREATE INDEX IF NOT EXISTS idx_instrument_record   ON instrument_scores(record_id);
CREATE INDEX IF NOT EXISTS idx_consents_record     ON consents(record_id);
CREATE INDEX IF NOT EXISTS idx_referrals_screen    ON referrals(screening_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user       ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_exp        ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_audit_ts            ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_ai_messages_conv    ON ai_messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_ai_conv_user        ON ai_conversations(user_id);

-- ═══════════════════════════════════════════════════════════════
-- SEED AI QUERY TEMPLATES
-- These are the "smart queries" Claude uses to answer questions
-- ═══════════════════════════════════════════════════════════════

INSERT OR IGNORE INTO ai_query_templates VALUES
('qt-01','Risk level distribution','Count records by AI risk level across all screenings','analytics',
 'SELECT risk_level, COUNT(*) as count, ROUND(COUNT(*)*100.0/(SELECT COUNT(*) FROM screenings),1) as pct FROM screenings GROUP BY risk_level ORDER BY count DESC',
 'What is the distribution of risk levels across all screenings?',1,CURRENT_TIMESTAMP),

('qt-02','Domain prevalence','Primary domain frequency','analytics',
 'SELECT primary_domain, COUNT(*) as count FROM records WHERE is_deleted=0 AND primary_domain IS NOT NULL GROUP BY primary_domain ORDER BY count DESC',
 'What are the most common primary domains / referral reasons?',1,CURRENT_TIMESTAMP),

('qt-03','Parish breakdown','Records and risk by parish','analytics',
 'SELECT r.parish, COUNT(DISTINCT r.id) as records, COUNT(s.id) as screenings, SUM(CASE WHEN s.risk_level=''high'' THEN 1 ELSE 0 END) as high_risk FROM records r LEFT JOIN screenings s ON s.record_id=r.id WHERE r.is_deleted=0 GROUP BY r.parish ORDER BY records DESC',
 'How are records distributed across parishes? Which parishes have highest risk?',1,CURRENT_TIMESTAMP),

('qt-04','Age band analysis','Screening outcomes by age group','analytics',
 'SELECT r.age_band, COUNT(DISTINCT r.id) as records, ROUND(AVG(s.score_overall),1) as avg_score, SUM(CASE WHEN s.risk_level=''high'' THEN 1 ELSE 0 END) as high_risk FROM records r LEFT JOIN screenings s ON s.record_id=r.id WHERE r.is_deleted=0 GROUP BY r.age_band ORDER BY r.age_band',
 'How do risk scores vary across age groups?',1,CURRENT_TIMESTAMP),

('qt-05','Cohen''s kappa','AI vs clinician agreement statistics','analytics',
 'SELECT s.risk_level as ai_risk, cv.validated_risk as cli_risk, r.parish, r.setting FROM screenings s JOIN clinician_validations cv ON cv.screening_id=s.id JOIN records r ON r.id=s.record_id WHERE cv.kappa_eligible=1',
 'What is the agreement between AI and clinician outputs? What is kappa?',1,CURRENT_TIMESTAMP),

('qt-06','High risk pending referrals','High-risk cases without completed referral','screening',
 'SELECT r.id, r.age_band, r.parish, r.setting, s.score_overall, s.completed_at FROM records r JOIN screenings s ON s.record_id=r.id LEFT JOIN referrals rf ON rf.screening_id=s.id WHERE s.risk_level=''high'' AND (rf.id IS NULL OR rf.status=''pending'') AND r.is_deleted=0 ORDER BY s.completed_at DESC LIMIT 50',
 'Which high-risk cases still need clinician referral?',1,CURRENT_TIMESTAMP),

('qt-07','Setting comparison','Risk and domain patterns by setting','analytics',
 'SELECT r.setting, COUNT(DISTINCT r.id) as records, ROUND(AVG(s.score_overall),1) as avg_score, ROUND(AVG(s.score_attention),1) as avg_attn, ROUND(AVG(s.score_asd),1) as avg_asd FROM records r JOIN screenings s ON s.record_id=r.id WHERE r.is_deleted=0 GROUP BY r.setting',
 'How do screening outcomes differ between schools, clinics, and deaf-inclusive sites?',1,CURRENT_TIMESTAMP),

('qt-08','Phase comparison','Compare archival vs pilot data','analytics',
 'SELECT r.phase, r.data_source, COUNT(DISTINCT r.id) as records, ROUND(AVG(s.score_overall),1) as avg_score FROM records r LEFT JOIN screenings s ON s.record_id=r.id WHERE r.is_deleted=0 GROUP BY r.phase, r.data_source',
 'How do Phase 1 archival records compare to Phase 2 pilot data?',1,CURRENT_TIMESTAMP),

('qt-09','Instrument usage','Which instruments are most used','analytics',
 'SELECT r.instruments, COUNT(*) as count FROM records r WHERE r.is_deleted=0 GROUP BY r.instruments ORDER BY count DESC LIMIT 20',
 'What assessment instruments are used most frequently?',1,CURRENT_TIMESTAMP),

('qt-10','Consent status','Consent completeness across records','analytics',
 'SELECT c.status, c.consent_type, COUNT(*) as count FROM consents c GROUP BY c.status, c.consent_type ORDER BY c.consent_type, c.status',
 'What is the status of consent forms across the dataset?',1,CURRENT_TIMESTAMP),

('qt-11','Trend over time','Monthly screening volume trend','analytics',
 'SELECT strftime(''%Y-%m'', s.completed_at) as month, COUNT(*) as screenings, SUM(CASE WHEN risk_level=''high'' THEN 1 ELSE 0 END) as high_risk FROM screenings s GROUP BY month ORDER BY month',
 'What is the trend in screenings and high-risk cases over time?',1,CURRENT_TIMESTAMP),

('qt-12','Domain by parish','Primary domain breakdown per parish','analytics',
 'SELECT r.parish, r.primary_domain, COUNT(*) as count FROM records r WHERE r.is_deleted=0 AND r.primary_domain IS NOT NULL GROUP BY r.parish, r.primary_domain ORDER BY r.parish, count DESC',
 'Which domains are most common in each parish?',1,CURRENT_TIMESTAMP),

('qt-13','Score percentiles','Descriptive stats for all domain scores','analytics',
 'SELECT COUNT(*) as n, ROUND(AVG(score_attention),1) as avg_attn, ROUND(AVG(score_asd),1) as avg_asd, ROUND(AVG(score_emotional),1) as avg_emo, ROUND(AVG(score_trauma),1) as avg_trauma, ROUND(AVG(score_overall),1) as avg_overall, ROUND(MIN(score_overall),1) as min_overall, ROUND(MAX(score_overall),1) as max_overall FROM screenings WHERE score_overall IS NOT NULL',
 'What are the mean, min, max scores across all domains?',1,CURRENT_TIMESTAMP),

('qt-14','Deaf-inclusive analysis','Records from deaf/blind-inclusive settings','bias',
 'SELECT r.parish, r.age_band, r.primary_domain, s.risk_level, s.score_overall FROM records r JOIN screenings s ON s.record_id=r.id WHERE r.setting=''deaf-inclusive'' AND r.is_deleted=0 ORDER BY s.score_overall DESC',
 'What do screenings from deaf-inclusive settings show?',1,CURRENT_TIMESTAMP),

('qt-15','Rural vs urban','Compare rural and urban parish outcomes','bias',
 'SELECT CASE WHEN r.parish IN (''Portland'',''St. Elizabeth'',''St. Ann'',''Westmoreland'') THEN ''Rural'' ELSE ''Urban'' END as area_type, COUNT(DISTINCT r.id) as records, ROUND(AVG(s.score_overall),1) as avg_score, SUM(CASE WHEN s.risk_level=''high'' THEN 1 ELSE 0 END) as high_risk FROM records r LEFT JOIN screenings s ON s.record_id=r.id WHERE r.is_deleted=0 GROUP BY area_type',
 'How do rural and urban parishes compare in screening outcomes?',1,CURRENT_TIMESTAMP);
