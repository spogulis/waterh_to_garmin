#!/usr/bin/env python3
"""Tiny web service around the sync:

- GET /sync?key=SECRET  -> runs the sync now, shows the result (for a phone shortcut)
- GET /health           -> "ok" (no auth)
- a background thread    -> runs the sync every SYNC_INTERVAL_SECONDS

Intended for personal, single-user deployment behind your own network / HTTPS.
"""
import hmac
import os
import threading
import time
import traceback
from datetime import datetime

from flask import Flask, Response, request

from waterh_to_garmin import TZ, run_sync

SYNC_KEY = os.environ.get("SYNC_KEY", "")
INTERVAL = int(os.environ.get("SYNC_INTERVAL_SECONDS", "3600"))
PORT = int(os.environ.get("PORT", "8000"))
RUN_SCHEDULER = os.environ.get("RUN_SCHEDULER", "1") == "1"

app = Flask(__name__)


def do_sync():
    try:
        lines = run_sync()
        return True, "\n".join(lines) if lines else "no data"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}"


@app.get("/health")
def health():
    return Response("ok\n", mimetype="text/plain")


@app.get("/sync")
def sync():
    if not SYNC_KEY or not hmac.compare_digest(request.args.get("key", ""), SYNC_KEY):
        return Response("forbidden\n", status=403, mimetype="text/plain")
    ok, msg = do_sync()
    title = "✅ Synced" if ok else "❌ Sync failed"
    html = f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WaterH → Garmin</title></head>
<body style="font-family:system-ui,-apple-system,sans-serif;max-width:640px;margin:2rem auto;padding:0 1rem">
<h2>{title}</h2>
<pre style="white-space:pre-wrap;background:#f4f4f5;padding:1rem;border-radius:10px;font-size:15px">{msg}</pre>
<p><a href="/sync?key={request.args.get('key','')}"
      style="display:inline-block;padding:.7rem 1.2rem;background:#2563eb;color:#fff;border-radius:10px;text-decoration:none">↻ Sync again</a></p>
</body></html>"""
    return Response(html, status=200 if ok else 500, mimetype="text/html")


def scheduler():
    time.sleep(5)  # let the web server bind first
    while True:
        ok, msg = do_sync()
        first = msg.splitlines()[0] if msg else ""
        print(f"[{datetime.now(TZ):%Y-%m-%d %H:%M:%S}] scheduled sync: "
              f"{'ok' if ok else 'FAIL'} — {first}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    if RUN_SCHEDULER:
        threading.Thread(target=scheduler, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, use_reloader=False, threaded=True)
