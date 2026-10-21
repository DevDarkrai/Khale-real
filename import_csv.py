"""
Import KADM_BASC3_import.csv into the prod database.
Run from the ded-project-prod/ directory:
    python scripts/import_csv.py path/to/KADM_BASC3_import.csv
"""
import sys, os, uuid, json, sqlite3, csv
from pathlib import Path

CSV_PATH = sys.argv[1] if len(sys.argv) > 1 else "KADM_BASC3_import.csv"
DB_PATH  = Path(__file__).parent.parent / "db" / "kompass.db"
SCHEMA   = Path(__file__).parent.parent / "db" / "schema.sql"

print(f"Database : {DB_PATH}")
print(f"CSV      : {CSV_PATH}")

# Init DB from schema
db = sqlite3.connect(str(DB_PATH))
db.row_factory = sqlite3.Row
db.executescript(SCHEMA.read_text())
db.commit()

# Clear existing records and screenings (fresh prod import)
db.execute("DELETE FROM clinician_validations")
db.execute("DELETE FROM screenings")
db.execute("DELETE FROM records")
db.commit()
print("Cleared existing records/screenings.")

records_inserted   = 0
screenings_inserted = 0

with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    for row in reader:
        rec_id      = row.get("study_id") or str(uuid.uuid4())
        screening_id = str(uuid.uuid4())

        # Parse instruments field — may be a JSON array or comma-separated string
        instruments_raw = row.get("instruments", "[]")
        try:
            instruments = json.loads(instruments_raw)
        except Exception:
            instruments = [x.strip() for x in instruments_raw.split(",") if x.strip()]

        # Record row
        db.execute("""
            INSERT OR REPLACE INTO records
              (id, age_band, parish, setting, rated_by, instruments,
               primary_domain, secondary_domain, phase, data_source,
               gender_code, year_of_assessment, school_type, created_at,
               is_deleted)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
        """, (
            rec_id,
            row.get("age_band", ""),
            row.get("parish", ""),
            row.get("setting", ""),
            row.get("rated_by", ""),
            json.dumps(instruments),
            row.get("primary_domain") or None,
            row.get("secondary_domain") or None,
            int(row.get("phase", 1) or 1),
            row.get("data_source", "archival"),
            row.get("gender_code") or None,
            int(row.get("year_of_assessment", 0) or 0) or None,
            row.get("school_type") or None,
            row.get("created_at") or None,
        ))
        records_inserted += 1

        # Screening row
        risk = row.get("ai_risk_level") or None

        def safe_float(v):
            try: return float(v) if v not in (None, "", "nan") else None
            except: return None

        db.execute("""
            INSERT INTO screenings
              (id, record_id, score_attention, score_asd, score_emotional,
               score_trauma, score_overall, risk_level, is_complete)
            VALUES (?,?,?,?,?,?,?,?,1)
        """, (
            screening_id,
            rec_id,
            safe_float(row.get("score_attention")),
            safe_float(row.get("score_asd")),
            safe_float(row.get("score_emotional")),
            safe_float(row.get("score_trauma")),
            safe_float(row.get("score_overall")),
            risk,
        ))
        screenings_inserted += 1

        # Clinician validation if present
        if row.get("clinician_validated_risk"):
            val_id = str(uuid.uuid4())
            # Need a clinician user — use the admin user id if present, else a placeholder
            clin_row = db.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()
            clin_id  = clin_row["id"] if clin_row else "system"
            db.execute("""
                INSERT INTO clinician_validations
                  (id, screening_id, clinician_id, validated_risk, confidence_level)
                VALUES (?,?,?,?,?)
            """, (
                val_id,
                screening_id,
                clin_id,
                row.get("clinician_validated_risk"),
                int(float(row.get("clinician_confidence") or 0)) or None,
            ))

db.commit()
db.close()

print(f"\n✓ Imported {records_inserted} records and {screenings_inserted} screenings.")
print("Prod database ready.")
