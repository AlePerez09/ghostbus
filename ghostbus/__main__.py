"""One command runs everything:  python -m ghostbus
(database setup on first boot + bus data feed + API + web app on $PORT, default 8000)"""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("ghostbus.api:app", host=os.environ.get("HOST", "0.0.0.0"),
                port=int(os.environ.get("PORT", "8000")), proxy_headers=False,
                access_log=False)  # no per-request logs: they would record riders' approximate locations  # we read client IPs ourselves (api.client_ip)
