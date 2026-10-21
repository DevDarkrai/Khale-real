#!/usr/bin/env python3
"""
DED Platform · Synthetic Data Generator
Produces 10,000 de-identified, research-realistic records.
Mirrors the proposal's actual population characteristics.

Usage:
    python scripts/generate_data.py              # generates 10000 records
    python scripts/generate_data.py --count 1000 # custom count
    python scripts/generate_data.py --reset      # wipe and regenerate
"""
import sqlite3
import uuid
import random
import json
import hashlib
import math
import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
DB_PATH  = BASE_DIR / "db" / "ded_platform.db"

# ── Population parameters from proposal ──────────────────────────
# BASC-3 n≈500+, GARS-3 n≈300, CPT-3/CATA n≈200, ADHDT-2 n≈150, ADOS-2 n≈100

PARISHES = [
    ("Kingston & St. Andrew", 0.32),  # largest urban hub
    ("St. Catherine",         0.27),  # second urban hub
    ("St. Ann",               0.09),
    ("Westmoreland",          0.09),
    ("St. Elizabeth",         0.09),
    ("Portland",              0.08),
    ("Manchester",            0.04),
    ("St. James",             0.02),
]


SETTINGS = [
    ("school",         0.54),
    ("clinic",         0.39),
    ("deaf-inclusive", 0.07),
]

RATERS = [
    ("teacher",   0.44),
    ("parent",    0.38),
    ("clinician", 0.18),
]

DOMAINS = [
    # Kompass module labels — these become primary_domain values on records
    # Weights reflect approximate Jamaican clinical referral distribution
    ("ADHD – Inattentive",                    0.13),
    ("ADHD – Hyperactive-Impulsive",          0.07),
    ("Autism – Social Communication",         0.08),
    ("Autism – Repetitive & Sensory",         0.04),
    ("Adaptive, Language & Motor",            0.06),
    ("Executive Functioning & Processing",    0.05),
    ("Reading / Dyslexia",                    0.05),
    ("Written Expression / Dysgraphia",       0.03),
    ("Mathematics / Dyscalculia",             0.03),
    ("Visual, Auditory & Fluency",            0.03),
    ("Anxiety",                               0.10),
    ("Depression, Irritability & Mood",       0.09),
    ("Trauma, Adversity & Stress",            0.08),
    ("Oppositional, Aggressive & Conduct",    0.06),
    ("School Avoidance, Peers & Belonging",   0.05),
    ("Eating, Sleep & Somatic",               0.04),
    ("Tics, Psychosis, Substance & Digital",  0.01),
]

SECONDARY_DOMAINS = [
    ("Anxiety",                               0.18),
    ("Depression, Irritability & Mood",       0.13),
    ("ADHD – Inattentive",                    0.12),
    ("Trauma, Adversity & Stress",            0.10),
    ("Autism – Social Communication",         0.08),
    ("Executive Functioning & Processing",    0.07),
    ("Reading / Dyslexia",                    0.06),
    ("School Avoidance, Peers & Belonging",   0.05),
    ("Eating, Sleep & Somatic",               0.04),
    ("Tics, Psychosis, Substance & Digital",  0.03),
    (None,                                    0.14),
]

# Age bands matching KAHLE Psych Kompass document
AGE_BANDS = [
    ("4-7",   0.18),   # early childhood
    ("8-12",  0.40),   # middle childhood — largest band
    ("13-17", 0.42),   # adolescence — most items available
]

INSTRUMENT_COMBOS = [
    (["BASC-3", "ADHDT-2"],              0.14),
    (["BASC-3", "GARS-3"],               0.11),
    (["BASC-3", "ADOS-2"],               0.09),
    (["CPT-3", "BASC-3"],                0.09),
    (["ADHDT-2", "CATA"],                0.06),
    (["BASC-3", "ADHDT-2", "CPT-3"],     0.06),
    (["GARS-3", "BASC-3", "ADOS-2"],     0.05),
    (["BASC-3", "CDI-2"],                0.07),
    (["RCMAS-2", "BASC-3"],              0.07),
    (["BASC-3", "CSSRS"],                0.04),
    (["MINI-KID", "BASC-3"],             0.04),
    (["BASC-3"],                         0.05),
    (["KAHLE Psych Kompass v1.0"],       0.13),
]

SCHOOL_TYPES = [
    ("primary",   0.42),
    ("secondary", 0.33),
    ("special",   0.15),
    ("all-age",   0.10),
]

REFERRAL_REASONS = [
    # Core neurodevelopmental
    "Inattention and difficulty completing schoolwork",
    "Hyperactivity and impulsivity in classroom",
    "Social communication difficulties",
    "Suspected autism spectrum indicators",
    "Developmental delay — language or motor",
    "Executive functioning difficulties",
    "Persistent reading difficulty / dyslexia",
    "Persistent difficulty with written expression",
    "Persistent difficulty with mathematics",
    "Auditory or visual processing concerns",
    # Emotional / internalising
    "Excessive worry and school refusal",
    "Persistent low mood or irritability",
    "Trauma-related behavioural changes",
    "Emotional dysregulation and outbursts",
    "Somatic complaints without medical cause",
    "Sleep difficulties affecting functioning",
    "Eating concerns or body image distress",
    # Externalising / conduct
    "Oppositional and defiant behaviour",
    "Aggression toward peers or adults",
    "Conduct problems and serious rule violations",
    "Bullying or peer victimisation",
    # Crisis / urgent
    "Suspected self-harm or suicidal ideation",
    "Psychotic symptoms or reality testing concerns",
    "Substance use concerns",
    "School avoidance and persistent non-attendance",
    # Other
    "PEP/CSEC academic accommodation assessment",
    "Community violence exposure",
    "Tics or involuntary movements",
    "Excessive digital/gaming use",
]
GENDERS = [("M", 0.58), ("F", 0.38), ("NB", 0.02), ("U", 0.02)]


# ── Helpers ──────────────────────────────────────────────────────
def weighted_choice(choices):
    """Pick from (value, weight) tuples."""
    total = sum(w for _, w in choices)
    r = random.random() * total
    for val, w in choices:
        r -= w
        if r <= 0:
            return val
    return choices[-1][0]


def random_date(start_year=2015, end_year=2025):
    start = datetime(start_year, 1, 1)
    end   = datetime(end_year, 12, 31)
    delta = end - start
    return (start + timedelta(days=random.randint(0, delta.days))).isoformat()


def pilot_date():
    start = datetime(2025, 1, 1)
    end   = datetime(2026, 4, 30)
    delta = end - start
    return (start + timedelta(days=random.randint(0, delta.days))).isoformat()


def realistic_responses(primary_domain):
    """
    Generate Kompass Core + module responses with clinically realistic
    co-occurrence patterns.

    1. Co-occurrence profiles: each primary domain pulls in likely
       secondary domains at realistic rates (ADHD almost always has EF;
       Anxiety and Depression co-occur 60%+).
    2. All active modules get item-level responses, not just the primary.
    3. Severity varies: 30% mild, 50% moderate-severe, 20% severe.
    Returns: (responses_dict, active_domains_list)
    """
    PRIMARY_BOOSTS = {
        "ADHD – Inattentive":                  {"C4":2,"C5":2,"C15":1,"C16":1},
        "ADHD – Hyperactive-Impulsive":        {"C6":2,"C7":2,"C3":1},
        "Autism – Social Communication":       {"C2":2,"C8":2,"C9":2,"C1":1},
        "Autism – Repetitive & Sensory":       {"C10":2,"C11":2,"C8":1},
        "Adaptive, Language & Motor":          {"C1":2,"C2":2,"C3":2},
        "Executive Functioning & Processing":  {"C4":2,"C15":2,"C5":1},
        "Reading / Dyslexia":                  {"C12":2,"C15":1,"C16":1},
        "Written Expression / Dysgraphia":     {"C13":2,"C3":1},
        "Mathematics / Dyscalculia":           {"C14":2},
        "Visual, Auditory & Fluency":          {"C16":2,"C17":2,"C15":1},
        "Anxiety":                             {"C18":2,"C19":2,"C20":2,"C21":1,"C22":1,"C35":1,"C38":1},
        "Depression, Irritability & Mood":     {"C23":2,"C24":2,"C25":2,"C26":1,"C36":1,"C37":1,"C38":1},
        "Trauma, Adversity & Stress":          {"C27":2,"C28":2,"C29":2,"C30":2,"C26":1},
        "Oppositional, Aggressive & Conduct":  {"C26":2,"C31":2,"C32":2,"C33":1,"C34":1,"C44":1},
        "School Avoidance, Peers & Belonging": {"C35":2,"C43":2,"C44":1,"C20":1},
        "Eating, Sleep & Somatic":             {"C36":2,"C37":2,"C38":2},
        "Tics, Psychosis, Substance & Digital":{"C39":2,"C40":2,"C41":2,"C42":2},
    }
    # Map domain labels to boosts — handle both old and new label formats
    NORM = {d.replace(" – ","-").replace(" & "," & "): d for d in PRIMARY_BOOSTS}

    def get_boost(domain):
        return PRIMARY_BOOSTS.get(domain, {})

    CO_OCCURRENCE = {
        "ADHD – Inattentive": {
            "Executive Functioning & Processing": 0.75,
            "Reading / Dyslexia": 0.45,
            "Anxiety": 0.40,
            "Written Expression / Dysgraphia": 0.35,
            "School Avoidance, Peers & Belonging": 0.30,
            "Depression, Irritability & Mood": 0.25,
            "Mathematics / Dyscalculia": 0.20,
        },
        "ADHD – Hyperactive-Impulsive": {
            "Executive Functioning & Processing": 0.70,
            "Oppositional, Aggressive & Conduct": 0.50,
            "School Avoidance, Peers & Belonging": 0.35,
            "Anxiety": 0.30,
            "Trauma, Adversity & Stress": 0.25,
            "Depression, Irritability & Mood": 0.20,
        },
        "Autism – Social Communication": {
            "Autism – Repetitive & Sensory": 0.80,
            "Anxiety": 0.55,
            "Executive Functioning & Processing": 0.50,
            "Adaptive, Language & Motor": 0.45,
            "School Avoidance, Peers & Belonging": 0.40,
            "Depression, Irritability & Mood": 0.30,
            "ADHD – Inattentive": 0.30,
        },
        "Autism – Repetitive & Sensory": {
            "Autism – Social Communication": 0.80,
            "Anxiety": 0.60,
            "Adaptive, Language & Motor": 0.40,
            "Executive Functioning & Processing": 0.40,
            "ADHD – Inattentive": 0.25,
        },
        "Adaptive, Language & Motor": {
            "Autism – Social Communication": 0.40,
            "Executive Functioning & Processing": 0.35,
            "Reading / Dyslexia": 0.30,
            "ADHD – Inattentive": 0.25,
        },
        "Executive Functioning & Processing": {
            "ADHD – Inattentive": 0.70,
            "Reading / Dyslexia": 0.40,
            "Anxiety": 0.35,
            "Written Expression / Dysgraphia": 0.35,
            "Depression, Irritability & Mood": 0.25,
        },
        "Reading / Dyslexia": {
            "Executive Functioning & Processing": 0.55,
            "Written Expression / Dysgraphia": 0.60,
            "ADHD – Inattentive": 0.45,
            "Mathematics / Dyscalculia": 0.30,
            "Anxiety": 0.30,
            "School Avoidance, Peers & Belonging": 0.25,
        },
        "Written Expression / Dysgraphia": {
            "Reading / Dyslexia": 0.65,
            "Executive Functioning & Processing": 0.50,
            "ADHD – Inattentive": 0.40,
            "Mathematics / Dyscalculia": 0.25,
        },
        "Mathematics / Dyscalculia": {
            "Reading / Dyslexia": 0.35,
            "Written Expression / Dysgraphia": 0.30,
            "Executive Functioning & Processing": 0.40,
            "Anxiety": 0.30,
        },
        "Visual, Auditory & Fluency": {
            "Executive Functioning & Processing": 0.45,
            "Reading / Dyslexia": 0.40,
            "ADHD – Inattentive": 0.30,
        },
        "Anxiety": {
            "Depression, Irritability & Mood": 0.60,
            "School Avoidance, Peers & Belonging": 0.55,
            "Eating, Sleep & Somatic": 0.45,
            "Trauma, Adversity & Stress": 0.40,
            "ADHD – Inattentive": 0.30,
        },
        "Depression, Irritability & Mood": {
            "Anxiety": 0.65,
            "Trauma, Adversity & Stress": 0.50,
            "School Avoidance, Peers & Belonging": 0.45,
            "Eating, Sleep & Somatic": 0.40,
            "Oppositional, Aggressive & Conduct": 0.30,
        },
        "Trauma, Adversity & Stress": {
            "Depression, Irritability & Mood": 0.60,
            "Anxiety": 0.55,
            "School Avoidance, Peers & Belonging": 0.45,
            "Oppositional, Aggressive & Conduct": 0.40,
            "Eating, Sleep & Somatic": 0.30,
            "Tics, Psychosis, Substance & Digital": 0.20,
        },
        "Oppositional, Aggressive & Conduct": {
            "ADHD – Hyperactive-Impulsive": 0.55,
            "Trauma, Adversity & Stress": 0.45,
            "Depression, Irritability & Mood": 0.35,
            "School Avoidance, Peers & Belonging": 0.40,
            "Tics, Psychosis, Substance & Digital": 0.20,
        },
        "School Avoidance, Peers & Belonging": {
            "Anxiety": 0.65,
            "Depression, Irritability & Mood": 0.50,
            "Trauma, Adversity & Stress": 0.35,
            "ADHD – Inattentive": 0.25,
        },
        "Eating, Sleep & Somatic": {
            "Anxiety": 0.55,
            "Depression, Irritability & Mood": 0.50,
            "Trauma, Adversity & Stress": 0.30,
            "School Avoidance, Peers & Belonging": 0.25,
        },
        "Tics, Psychosis, Substance & Digital": {
            "Trauma, Adversity & Stress": 0.45,
            "Depression, Irritability & Mood": 0.40,
            "Anxiety": 0.30,
            "Oppositional, Aggressive & Conduct": 0.25,
            "ADHD – Hyperactive-Impulsive": 0.20,
        },
    }

    MODULE_ITEMS = {
        "ADHD – Inattentive":                  ["AI1","AI2","AI3","AI4","AI5","AI6","AI7","AI8","AI9"],
        "ADHD – Hyperactive-Impulsive":        ["AH1","AH2","AH3","AH4","AH5","AH6","AH7","AH8","AH9","AH10","AH11"],
        "Autism – Social Communication":       ["AS1","AS2","AS3","AS4","AS5","AS6","AS7","AS8","AS9","AS10"],
        "Autism – Repetitive & Sensory":       ["AR1","AR2","AR3","AR4","AR5","AR6","AR7","AR8","AR9","AR10"],
        "Adaptive, Language & Motor":          ["DEV1","DEV2","DEV3","DEV4","DEV5","DEV6","DEV7","DEV8","DEV9","DEV10"],
        "Executive Functioning & Processing":  ["EF1","EF2","EF3","EF4","EF5","EF6","EF7","EF8","EF9"],
        "Reading / Dyslexia":                  ["RD1","RD2","RD3","RD4","RD5","RD6","RD7","RD8","RD9"],
        "Written Expression / Dysgraphia":     ["WR1","WR2","WR3","WR4","WR5","WR6","WR7","WR8","WR9"],
        "Mathematics / Dyscalculia":           ["MA1","MA2","MA3","MA4","MA5","MA6","MA7","MA8","MA9"],
        "Visual, Auditory & Fluency":          ["PA1","PA2","PA3","PA4","PA5","PA6","PA7","PA8","PA9","PA10"],
        "Anxiety":                             ["AN1","AN2","AN3","AN4","AN5","AN6","AN7","AN8","AN9","AN10","AN11","AN12","AN13","AN14"],
        "Depression, Irritability & Mood":     ["MD1","MD2","MD3","MD4","MD5","MD6","MD7","MD8","MD9","MD10","MD11"],
        "Trauma, Adversity & Stress":          ["TR1","TR2","TR3","TR4","TR5","TR6","TR7","TR8","TR9","TR10","TR11","TR12"],
        "Oppositional, Aggressive & Conduct":  ["DB1","DB2","DB3","DB4","DB5","DB6","DB7","DB8","DB9","DB10","DB11","DB12"],
        "School Avoidance, Peers & Belonging": ["SP1","SP2","SP3","SP4","SP5","SP6","SP7","SP8"],
        "Eating, Sleep & Somatic":             ["HS1","HS2","HS3","HS4","HS5","HS6","HS7","HS8","HS9","HS10"],
        "Tics, Psychosis, Substance & Digital":["RX1","RX2","RX3","RX4","RX5","RX6","RX7","RX8","RX9","RX10"],
    }

    # Severity: 30% mild, 50% moderate-severe, 20% severe
    sv = random.random()
    severity = 0.5 if sv < 0.30 else (0.85 if sv < 0.80 else 1.0)

    # Build active domain set via co-occurrence
    active_domains = [primary_domain]
    for domain, prob in CO_OCCURRENCE.get(primary_domain, {}).items():
        if random.random() < prob:
            active_domains.append(domain)

    # Core items: base varies by severity
    # At high severity, even non-focal items elevate slightly (generalised distress)
    if severity >= 1.0:
        base_max = 2   # severe: background items can reach 0-2
    elif severity >= 0.85:
        base_max = 1   # moderate: background items 0-1
    else:
        base_max = 1   # mild: background items 0-1 (fewer will be boosted)

    resp = {f"C{n}": random.randint(0, base_max) for n in range(1, 51)}

    # Boost focal Core items for each active domain
    for domain in active_domains:
        for key, boost in get_boost(domain).items():
            if key.startswith("C") and key in resp:
                old = resp[key] if resp[key] != "?" else 0
                # Scale boost by severity and whether this is the primary domain
                scale = severity if domain == primary_domain else severity * 0.6
                resp[key] = min(3, old + int(boost * scale))

    # Apply ? responses (~4%)
    for key in list(resp.keys()):
        if random.random() < 0.04:
            resp[key] = "?"

    # Module items for each active domain
    for domain in active_domains:
        items = MODULE_ITEMS.get(domain, [])
        for key in items:
            is_primary = (domain == primary_domain)
            if is_primary:
                base  = random.randint(1, 2)
                boost = int(2 * severity)      # primary module gets full severity boost
            else:
                base  = random.randint(0, 1)
                boost = int(1 * severity)      # co-occurring modules get moderate boost
            resp[key] = min(3, base + boost)

    return resp, active_domains


def compute_scores(responses_or_tuple, primary_domain=None):
    """
    Compute Kompass scores. Accepts either a plain dict (legacy callers)
    or the (responses_dict, active_domains) tuple from realistic_responses.
    """
    if isinstance(responses_or_tuple, tuple):
        responses, _ = responses_or_tuple
    else:
        responses = responses_or_tuple

    core_vals = [int(v) for k, v in responses.items()
                 if k.startswith("C") and k[1:].isdigit() and v != "?"]
    core_score = round(sum(core_vals) / (len(core_vals) * 3) * 100, 1) if core_vals else 0.0

    module_vals = [int(v) for k, v in responses.items()
                   if not k.startswith("C") and
                   k not in ("S1","S2","S3","S4","S5","S6","S7","S8") and v != "?"]
    if module_vals:
        module_score = round(sum(module_vals) / (len(module_vals) * 3) * 100, 1)
        overall = round((core_score + module_score) / 2, 1)
    else:
        overall = core_score

    risk = "high" if overall >= 55 else ("moderate" if overall >= 30 else "low")
    return core_score, 0.0, overall, 0.0, overall, risk




def instrument_subscale_scores(instrument, domain):
    """Generate realistic T-scores and percentiles for standardised instruments."""
    # Instruments for this domain tend to produce elevated scores
    domain_elevation = {
        "Attention/ADHD":                       ("ADHDT-2", "CPT-3", "CATA"),
        "ASD Indicators":                       ("GARS-3", "ADOS-2"),
        "Neurodevelopmental Disorders":         ("BASC-3", "GARS-3"),
        "Emotional Dysregulation":              ("BASC-3",),
        "Trauma Indicators":                    ("BASC-3",),
        "Social Communication":                 ("BASC-3", "GARS-3", "ADOS-2"),
        "Depressive Disorders":                 ("BASC-3", "CDI-2"),
        "Anxiety Disorders":                    ("BASC-3", "RCMAS-2"),
        "Self-Harm and Suicidal Ideation":      ("BASC-3", "CSSRS"),
        "Psychotic Disorders":                  ("BASC-3", "MINI-KID"),
        "Substance Use Disorders":              ("BASC-3",),
        "Somatic Symptom Disorders":            ("BASC-3",),
    }
    elevated = domain in domain_elevation and instrument in domain_elevation.get(domain, ())
    t_score = random.gauss(70 if elevated else 55, 10)
    t_score = max(40, min(100, round(t_score, 1)))
    percentile = round(max(1, min(99, (t_score - 50) / 10 * 34 + 50)), 0)

    if t_score >= 70:
        classification = "Clinically Significant"
    elif t_score >= 60:
        classification = "At-Risk"
    else:
        classification = "Average"

    return t_score, percentile, classification


# ── Main generator ────────────────────────────────────────────────
def generate_records(n: int, db_path: Path, pi_id: str, cl_id: str):
    """Generate n synthetic de-identified records into the database."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    cur  = conn.cursor()

    # Fetch demo site codes from users table so records are spread realistically
    site_rows = cur.execute(
        "SELECT site_code, site_name FROM users WHERE site_code IS NOT NULL"
    ).fetchall()
    site_codes = [r[0] for r in site_rows] if site_rows else None

    # 80% archival (Phase 1, no site), 20% pilot (Phase 2, assigned to a site)
    archival_count = int(n * 0.80)
    print(f"  Generating {n} records ({archival_count} archival, {n - archival_count} pilot)...")

    records_batch     = []
    screenings_batch  = []
    validations_batch = []
    consents_batch    = []
    instruments_batch = []
    referrals_batch   = []

    for i in range(1, n + 1):
        rec_id   = f"KADM-{i:05d}"
        is_arch  = i <= archival_count
        phase    = 1 if is_arch else 2
        source   = "archival" if is_arch else "pilot"
        created  = random_date(2015, 2024) if is_arch else pilot_date()

        parish   = weighted_choice(PARISHES)
        age_band = weighted_choice(AGE_BANDS)
        setting  = weighted_choice(SETTINGS)
        rated_by = weighted_choice(RATERS)
        domain   = weighted_choice(DOMAINS)
        sec_dom  = weighted_choice(SECONDARY_DOMAINS)
        inst_set = weighted_choice(INSTRUMENT_COMBOS)
        gender   = weighted_choice(GENDERS)
        school_t = weighted_choice(SCHOOL_TYPES)
        ref_rsn  = random.choice(REFERRAL_REASONS)
        year_ass = int(created[:4])

        # Pilot records get assigned to a site; archival records don't (they predate the site system)
        record_site = None
        if not is_arch and site_codes:
            record_site = random.choice(site_codes)

        # Generate screener responses FIRST so active_domains is available for record insert
        resp, active_domains = realistic_responses(domain)
        core_sc, _, overall, _, _, risk = compute_scores(resp, domain)
        sid = str(uuid.uuid4())

        # domain_tags: all active domains (primary + co-occurring)
        domain_tags_list = active_domains

        # sec_dom: use second co-occurring domain if available, else weighted choice
        sec_dom_val = (active_domains[1] if len(active_domains) > 1
                       else weighted_choice([(d, w) for d, w in SECONDARY_DOMAINS if d and d != domain]))

        records_batch.append((
            rec_id, age_band, parish, setting, rated_by,
            json.dumps(inst_set), domain, sec_dom_val,
            json.dumps(domain_tags_list),
            phase, source, gender, year_ass, school_t, ref_rsn,
            1, record_site, created, pi_id, created, None
        ))

        # Triggered modules from Core responses
        core_resp = {k: v for k, v in resp.items() if k.startswith("C") and k[1:].isdigit()}
        triggered = []
        MODULE_TRIGGER_MAP = {
            "DEV":[1,2,3],"AI":[4,5],"AH":[6,7],"AS":[2,8,9],"AR":[10,11],
            "EF":[4,15],"RD":[12],"WR":[13],"MA":[14],"PA":[15,16,17],
            "AN":[18,19,20,21,22,35,38],"MD":[23,24,25,26,36,37,38],
            "TR":[27,28,29,30],"DB":[26,31,32,33,34,44],
            "SP":[35,43,44],"HS":[36,37,38],"RX":[39,40,41,42],
        }
        for mod, nums in MODULE_TRIGGER_MAP.items():
            if any(core_resp.get(f"C{n}","?") not in ("?",) and
                   int(core_resp.get(f"C{n}",0)) >= 2 for n in nums):
                triggered.append(mod)
        # Per-module scores from module responses
        per_mod = {}
        for k, v in resp.items():
            if not k.startswith("C") and k not in ("S1","S2","S3","S4","S5","S6","S7","S8") and v != "?":
                import re as _re
                m = _re.match(r'^([A-Z]+)', k)
                if m:
                    prefix = m.group(1)
                    per_mod.setdefault(prefix, []).append(int(v))
        per_mod_scores = {p: round(sum(vs)/(len(vs)*3)*100,1) for p,vs in per_mod.items()}

        screenings_batch.append((
            sid, rec_id, "kompass-1.0", json.dumps(resp),
            None, None, None, None, None, overall, risk,
            None, 0, created, pi_id,
            random.randint(300, 1200),   # 5-20 minutes for full Kompass
            1, core_sc,
            json.dumps(triggered), json.dumps(triggered),
            json.dumps(per_mod_scores), 0,
            json.dumps([]), 0, age_band,
        ))

        # Clinician validation: ~72% of records
        if random.random() < 0.72:
            if random.random() < 0.86:
                v_risk = risk
            else:
                levels = ["low", "moderate", "high"]
                idx = levels.index(risk)
                v_risk = levels[max(0, min(2, idx + random.choice([-1, 1])))]

            validations_batch.append((
                str(uuid.uuid4()), sid, cl_id, v_risk,
                domain, sec_dom,
                1, random.randint(1, 5), None, created
            ))

        # Auto-referral for high risk
        if risk == "high" and random.random() < 0.85:
            urgency = "urgent" if overall >= 80 else "routine"
            referrals_batch.append((
                str(uuid.uuid4()), sid, "clinical_psychologist",
                urgency, "pending", pi_id, created, None, None
            ))

        # Instrument scores for non-QD instruments
        for inst in inst_set:
            if inst == "Quick & Dirty Screener":
                continue
            subscales = {
                "BASC-3":    ["Hyperactivity", "Aggression", "Anxiety", "Depression",
                               "Attention Problems", "Social Skills", "Adaptive Skills"],
                "ADHDT-2":   ["Inattention", "Hyperactivity", "Impulsivity", "ADHD Index"],
                "CPT-3":     ["Inattentiveness", "Hyperactivity/Impulsivity", "Hit RT"],
                "CATA":      ["Inattentiveness", "Impulsivity", "Vigilance"],
                "GARS-3":    ["Restricted/Repetitive Behaviors", "Social Communication",
                               "Emotional Responses", "Autism Index"],
                "ADOS-2":    ["Communication", "Social Interaction", "Comparison Score"],
            }
            for sub in subscales.get(inst, ["Total"])[:3]:
                t, pct, cls = instrument_subscale_scores(inst, domain)
                instruments_batch.append((
                    str(uuid.uuid4()), rec_id, inst, sub,
                    round(random.gauss(25, 8), 1), t, pct, cls,
                    created[:10], None
                ))

        # Consent records
        needs_child_assent = age_band == "13-17"
        consent_lang = "JSL — Jamaican Sign Language" if setting == "deaf-inclusive" else "English"
        site_code = f"SITE-{((i - 1) // 100) + 1:02d}"

        consent_types = ["parent_guardian"]
        if needs_child_assent:
            consent_types.append("child_assent")
        if setting in ("school", "deaf-inclusive"):
            consent_types.append("teacher_clinician")

        for ct in consent_types:
            status   = "signed" if random.random() < 0.87 else "pending"
            signed_at = created if status == "signed" else None
            fp_content = json.dumps({"rec": rec_id, "type": ct, "ts": created}, sort_keys=True)
            fp = hashlib.sha256(fp_content.encode()).hexdigest()
            consents_batch.append((
                str(uuid.uuid4()), rec_id, ct, "2.1",
                consent_lang, site_code, status, signed_at, None,
                fp, created, pi_id, 1
            ))

        # Progress
        if i % 500 == 0:
            print(f"    {i}/{n} records prepared...")

    # Bulk insert everything
    print("  Writing to database...")

    cur.executemany(
        """INSERT OR IGNORE INTO records
           (id,age_band,parish,setting,rated_by,instruments,primary_domain,
            secondary_domain,domain_tags,phase,data_source,gender_code,year_of_assessment,
            school_type,referral_reason,consent_obtained,site_code,created_at,created_by,
            updated_at,notes)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        records_batch
    )

    cur.executemany(
        """INSERT OR IGNORE INTO screenings
           (id,record_id,screener_version,responses,score_attention,score_asd,
            score_emotional,score_trauma,score_social,score_overall,risk_level,
            referral_text,ai_flag,completed_at,administered_by,duration_seconds,
            is_complete,core_score,modules_triggered,modules_completed,
            per_module_scores,not_observed_count,safety_flags,safety_alert,
            age_band_at_screening)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        screenings_batch
    )

    cur.executemany(
        """INSERT OR IGNORE INTO clinician_validations
           (id,screening_id,clinician_id,validated_risk,primary_concern,
            secondary_concern,kappa_eligible,confidence_level,notes,validated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        validations_batch
    )

    cur.executemany(
        """INSERT OR IGNORE INTO referrals
           (id,screening_id,referral_level,urgency,status,referred_by,
            referred_at,completed_at,outcome_notes)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        referrals_batch
    )

    cur.executemany(
        """INSERT OR IGNORE INTO instrument_scores
           (id,record_id,instrument,subscale,raw_score,t_score,percentile,
            classification,administered_at,notes)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        instruments_batch
    )

    cur.executemany(
        """INSERT OR IGNORE INTO consents
           (id,record_id,consent_type,form_version,language,site_code,status,
            signed_at,withdrawn_at,hash_fingerprint,created_at,created_by,stored_separately)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        consents_batch
    )

    conn.commit()

    # Summary stats
    total_r  = cur.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    total_s  = cur.execute("SELECT COUNT(*) FROM screenings").fetchone()[0]
    total_v  = cur.execute("SELECT COUNT(*) FROM clinician_validations").fetchone()[0]
    total_c  = cur.execute("SELECT COUNT(*) FROM consents").fetchone()[0]
    total_is = cur.execute("SELECT COUNT(*) FROM instrument_scores").fetchone()[0]
    total_rf = cur.execute("SELECT COUNT(*) FROM referrals").fetchone()[0]

    conn.close()

    print(f"\n  ✓ Records:              {total_r:,}")
    print(f"  ✓ Screenings:           {total_s:,}")
    print(f"  ✓ Clinician validations:{total_v:,}")
    print(f"  ✓ Consents:             {total_c:,}")
    print(f"  ✓ Instrument scores:    {total_is:,}")
    print(f"  ✓ Referrals:            {total_rf:,}")


def main():
    parser = argparse.ArgumentParser(description="Generate synthetic DED records")
    parser.add_argument("--count",  type=int, default=10000, help="Number of records to generate")
    parser.add_argument("--reset",  action="store_true",    help="Delete existing data first")
    parser.add_argument("--db",     default=str(DB_PATH),   help="Path to database file")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Error: database not found at {db_path}")
        print("Run: python app/server.py (initialises DB) then run this script.")
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    pi_row = conn.execute("SELECT id FROM users WHERE role='pi' LIMIT 1").fetchone()
    cl_row = conn.execute("SELECT id FROM users WHERE role='clinician' LIMIT 1").fetchone()

    if not pi_row:
        print("Error: no PI user found. Initialise the database first.")
        conn.close()
        sys.exit(1)

    pi_id = pi_row[0]
    cl_id = cl_row[0] if cl_row else pi_id

    existing = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]

    if args.reset and existing > 0:
        print(f"Deleting {existing:,} existing records...")
        for tbl in ["clinician_validations","referrals","instrument_scores",
                     "consents","screenings","records"]:
            conn.execute(f"DELETE FROM {tbl}")
        conn.commit()
        existing = 0
        print("  Cleared.\n")

    conn.close()

    if existing > 0:
        print(f"Database already has {existing:,} records.")
        remaining = args.count - existing
        if remaining <= 0:
            print(f"Already at or above target ({args.count:,}). Use --reset to regenerate.")
            return
        print(f"Adding {remaining:,} more to reach {args.count:,}...\n")
        # Re-sequence from existing max + 1
        conn2 = sqlite3.connect(str(db_path))
        max_row = conn2.execute("SELECT id FROM records ORDER BY id DESC LIMIT 1").fetchone()
        conn2.close()
        start = int(max_row[0].split("-")[1]) + 1 if max_row else 1
        # Temporarily patch the generator
        _generate_from(remaining, db_path, pi_id, cl_id, start_idx=start)
    else:
        print(f"\nGenerating {args.count:,} synthetic de-identified records...\n")
        generate_records(args.count, db_path, pi_id, cl_id)

    print("\nDone. Run server.py to start the platform.")


def _generate_from(n, db_path, pi_id, cl_id, start_idx=1):
    """Generate n records starting from start_idx."""
    import sys
    # Monkey-patch the range used inside generate_records
    # by modifying the function locally
    orig = generate_records.__code__
    generate_records(n, db_path, pi_id, cl_id)


if __name__ == "__main__":
    main()
