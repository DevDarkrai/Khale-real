#!/usr/bin/env python3
"""
KAHLE Psych Kompass · Start Script
Works on Windows (PowerShell), Mac, and Linux.

Usage:
    python start.py                    # start server (generates data if DB is empty)
    python start.py --generate 10000    # generate 10000 records then start
    python start.py --generate 10000 --reset  # wipe and regenerate
    python start.py --port 9000        # use custom port
"""
import sys
import os
import argparse
import socket
import subprocess
from pathlib import Path

BASE = Path(__file__).parent

# ── Render persistent-disk bootstrap ─────────────────────────────────────────
# On Render, DB_PATH points to /data/ded_platform.db (the mounted disk).
# On first deploy the disk is empty, so we copy the committed seed DB there.
_db_path_env = os.environ.get("DB_PATH", "")
if _db_path_env and not Path(_db_path_env).exists():
    import shutil
    _seed = BASE / "db" / "ded_platform.db"
    if _seed.exists():
        Path(_db_path_env).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_seed, _db_path_env)
        print(f"  ✓ Seeded database → {_db_path_env}")
# ─────────────────────────────────────────────────────────────────────────────

BANNER = """
╔══════════════════════════════════════════════════════╗
║   KAHLE Psych Kompass  v3.0.0           ║
║   Dr. KAD Morgan & Associates · Jamaica              ║
║   Ethics Ref: MOH-JA-2025-0047                       ║
╚══════════════════════════════════════════════════════╝
"""


def check(label, ok, fix=""):
    sym = "✓" if ok else "✗"
    line = f"  {sym} {label}"
    if not ok and fix:
        line += f"\n    → {fix}"
    print(line)
    return ok


def port_free(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def module_ok(name):
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def main():
    parser = argparse.ArgumentParser(description="KAHLE Psych Kompass start script")
    parser.add_argument("--port",     type=int, default=int(os.environ.get("PORT", 8000)))
    parser.add_argument("--generate", type=int, default=None,
                        help="Generate N synthetic records before starting")
    parser.add_argument("--reset",    action="store_true",
                        help="Wipe existing data before generating")
    parser.add_argument("--no-browser", action="store_true",
                        help="Don't open browser automatically")
    args = parser.parse_args()

    print(BANNER)

    # ── Pre-flight ────────────────────────────────────────────────
    print("Pre-flight checks:")
    ok = True
    ok &= check("Python 3.8+", sys.version_info >= (3, 8),
                f"Found {sys.version.split()[0]} — upgrade from python.org")
    ok &= check("sqlite3 available",   module_ok("sqlite3"))
    ok &= check("app/server.py exists",    (BASE / "app" / "server.py").exists())
    ok &= check("db/schema.sql exists",    (BASE / "db"  / "schema.sql").exists())
    ok &= check(f"Port {args.port} free", port_free(args.port),
                f"Port {args.port} in use. Try: python start.py --port 8001")

    # Check API key
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    env_file = BASE / ".env"
    if not api_key and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("ANTHROPIC_API_KEY="):
                api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    check("Anthropic API key",
          bool(api_key and not api_key.startswith("sk-ant-your")),
          "Set ANTHROPIC_API_KEY in .env or environment variable")

    if not ok:
        print("\n  Fix issues above, then re-run.")
        sys.exit(1)

    print()

    # ── Initialise DB ─────────────────────────────────────────────
    sys.path.insert(0, str(BASE / "app"))
    import server as srv
    srv.PORT = args.port

    print("Initialising database...")
    srv.init_db()

    db_path = srv.DB_PATH
    conn = __import__("sqlite3").connect(str(db_path))
    rec_count = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    conn.close()
    print(f"  Current records: {rec_count:,}")

    # ── Generate data (disabled in prod — data loaded from CSV import) ──
    if args.generate:
        target = args.generate or 10000
        print(f"\nGenerating {target:,} synthetic records...")
        from scripts.generate_data import generate_records
        conn2 = __import__("sqlite3").connect(str(db_path))
        conn2.row_factory = __import__("sqlite3").Row
        if args.reset and rec_count > 0:
            for tbl in ["clinician_validations","referrals","instrument_scores",
                        "consents","screenings","records"]:
                conn2.execute(f"DELETE FROM {tbl}")
            conn2.commit()
            print("  Existing data cleared.")
        pi_row = conn2.execute("SELECT id FROM users WHERE role='pi' LIMIT 1").fetchone()
        cl_row = conn2.execute("SELECT id FROM users WHERE role='clinician' LIMIT 1").fetchone()
        pi_id  = pi_row["id"] if pi_row else "u-pi"
        cl_id  = cl_row["id"] if cl_row else "u-pi"
        conn2.close()
        generate_records(target, db_path, pi_id, cl_id)

    # ── Print info ────────────────────────────────────────────────
    conn3 = __import__("sqlite3").connect(str(db_path))
    final_count = conn3.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    conn3.close()

    print(f"""
  Server:   http://localhost:{args.port}
  Records:  {final_count:,}
  Claude:   {"✓ configured" if api_key else "✗ not configured (set ANTHROPIC_API_KEY in .env)"}
  Database: {db_path}

  Demo credentials:
    PI:         dr.morgan  /  KADMorgan2025!
    Researcher: v.billett  /  Verol2025!
    Clinician:  dr.thorpe  /  Thorpe2025!

  Press Ctrl+C to stop.
  {"─" * 54}""")

    # ── Open browser ──────────────────────────────────────────────
    if not args.no_browser:
        import threading, webbrowser, time
        def open_browser():
            time.sleep(1.5)
            webbrowser.open(f"http://localhost:{args.port}/api/health")
        threading.Thread(target=open_browser, daemon=True).start()

    # ── Start server ──────────────────────────────────────────────
    srv.main()


if __name__ == "__main__":
    main()
