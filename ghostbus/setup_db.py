"""Create tables, hypertables, compression and continuous aggregates. Safe to run once per database.

Usage: python -m ghostbus.setup_db
"""
from pathlib import Path

import psycopg

from .config import DATABASE_URL


def split_sql(sql: str):
    """Split on semicolons that are outside $$-quoted function bodies and comments."""
    out, buf, in_dollar = [], [], False
    for line in sql.splitlines():
        if not in_dollar and line.strip().startswith("--"):
            continue
        buf.append(line)
        if line.count("$$") % 2 == 1:
            in_dollar = not in_dollar
        if not in_dollar and line.rstrip().endswith(";"):
            stmt = "\n".join(buf).strip()
            if stmt.strip(";").strip():
                out.append(stmt)
            buf = []
    return out


def main():
    sql = Path(__file__).resolve().parent.parent.joinpath("db/schema.sql").read_text()
    # Continuous-aggregate DDL can't run inside a transaction block, so use autocommit.
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        for stmt in split_sql(sql):
            conn.execute(stmt)
    print("Schema ready.")


if __name__ == "__main__":
    main()
