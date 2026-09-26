"""Shared settings, read from environment variables (or a .env file)."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

# Minimal .env loader so the team doesn't need python-dotenv.
_env = Path(__file__).resolve().parent.parent / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:password@localhost:5432/tsdb")

# Miami-Dade publishes static GTFS publicly; realtime goes through Swiftly (needs an API key).
GTFS_STATIC_URL = os.environ.get(
    "GTFS_STATIC_URL", "https://www.miamidade.gov/transit/googletransit/current/google_transit.zip"
)
GTFS_RT_VEHICLES_URL = os.environ.get(
    "GTFS_RT_VEHICLES_URL", "https://api.goswift.ly/real-time/miami/gtfs-rt-vehicle-positions"
)
GTFS_RT_API_KEY = os.environ.get("GTFS_RT_API_KEY", "")
# Swiftly expects the key in the Authorization header; other agencies use a query param.
GTFS_RT_AUTH_HEADER = os.environ.get("GTFS_RT_AUTH_HEADER", "Authorization")

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "15"))
TZ = ZoneInfo(os.environ.get("AGENCY_TZ", "America/New_York"))
