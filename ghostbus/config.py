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


# Every agency Ghost Bus loads. Each agency numbers its own routes/stops/trips, so all IDs except
# Miami-Dade's get a prefix ("bct:1") to keep them from colliding. Miami-Dade stays unprefixed so
# existing history keeps matching. A realtime feed is optional per agency: agencies without one
# still show schedules, but they're left out of ghost detection (no feed doesn't mean no bus).
def _agency(key, name, prefix, static_url, rt_url="", rt_key="", rt_header="Authorization"):
    env = key.upper()
    return {
        "key": key, "name": name, "prefix": prefix,
        "static_url": os.environ.get(f"{env}_GTFS_URL", static_url),
        "rt_url": os.environ.get(f"{env}_RT_URL", rt_url),
        "rt_key": os.environ.get(f"{env}_RT_KEY", rt_key),
        "rt_header": os.environ.get(f"{env}_RT_AUTH_HEADER", rt_header),
    }


AGENCIES = [
    _agency("mdt", "Miami-Dade Transit", "", GTFS_STATIC_URL,
            GTFS_RT_VEHICLES_URL if GTFS_RT_API_KEY else "", GTFS_RT_API_KEY, GTFS_RT_AUTH_HEADER),
    _agency("bct", "Broward County Transit", "bct:", "https://www.broward.org/bct/documents/google_transit.zip"),
    _agency("pt", "Palm Tran", "pt:", "http://www.palmtran.org/feed/google_transit.zip"),
    _agency("kw", "Key West Transit", "kw:", "http://data.trilliumtransit.com/gtfs/keywest-fl-us/keywest-fl-us.zip"),
]
# Short label shown before non-Miami route numbers so riders can tell "BCT 1" from Miami's "1".
ROUTE_LABEL = {"mdt": "", "bct": "BCT", "pt": "Palm Tran", "kw": "Key West"}
RT_AGENCIES = [a for a in AGENCIES if a["rt_url"]]

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "15"))
TZ = ZoneInfo(os.environ.get("AGENCY_TZ", "America/New_York"))
