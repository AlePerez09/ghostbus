"""Generate a small Miami-flavored GTFS zip for offline testing.

Usage: python tools/fake_gtfs.py fake_gtfs.zip
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ghostbus.fake_gtfs import main  # noqa: E402

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "fake_gtfs.zip")
