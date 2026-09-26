"""Compress every chunk older than N minutes right now (handy before a demo).

Usage: python -m ghostbus.compress_now --older-than 60
"""
import argparse

import psycopg

from .config import DATABASE_URL


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--older-than", type=int, default=60, help="minutes")
    args = ap.parse_args()
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        for ht in ("vehicle_positions", "stop_arrivals"):
            rows = conn.execute("SELECT compress_chunk(c, if_not_compressed => true) FROM show_chunks(%s, older_than => %s * interval '1 minute') c",
                                (ht, args.older_than)).fetchall()
            print(f"{ht}: {len(rows)} chunk(s) compressed")
        for ht in ("vehicle_positions", "stop_arrivals"):
            r = conn.execute("SELECT sum(before_compression_total_bytes), sum(after_compression_total_bytes) "
                             "FROM chunk_compression_stats(%s) WHERE compression_status = 'Compressed'", (ht,)).fetchone()
            if r[1]:
                r = (float(r[0]), float(r[1]))
                print(f"{ht}: {r[0] / 1e6:.1f} MB -> {r[1] / 1e6:.1f} MB  ({r[0] / r[1]:.1f}x smaller)")


if __name__ == "__main__":
    main()
