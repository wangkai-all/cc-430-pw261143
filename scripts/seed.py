#!/usr/bin/env python3
"""Generate the fictional upstream dataset into IMPACT_HOME (deterministic)."""
import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    ap = argparse.ArgumentParser(description="seed the fictional dataset")
    ap.add_argument("--home", help="IMPACT_HOME (default: ./data)")
    ap.add_argument("--reset", action="store_true", help="wipe IMPACT_HOME first")
    ap.add_argument("--n", type=int, default=600, help="number of details")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    if args.home:
        os.environ["IMPACT_HOME"] = args.home

    from impact import config, db, fiction

    if args.reset:
        shutil.rmtree(config.home(), ignore_errors=True)
    db.init_db()
    con = db.connect()
    n = fiction.seed_database(con, n=args.n, seed=args.seed)
    con.close()
    if n:
        print("seeded %d fictional details into %s (versions 1..%d)" % (n, config.home(), n))
    else:
        print("database already seeded, nothing to do")


if __name__ == "__main__":
    main()
