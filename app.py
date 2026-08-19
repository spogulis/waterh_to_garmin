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

from waterh_to_garmin import TZ, garmin_add, garmin_status, run_sync, set_manual_today

SYNC_KEY = os.environ.get("SYNC_KEY", "")
INTERVAL = int(os.environ.get("SYNC_INTERVAL_SECONDS", "3600"))
PORT = int(os.environ.get("PORT", "8000"))
RUN_SCHEDULER = os.environ.get("RUN_SCHEDULER", "1") == "1"

app = Flask(__name__)

# The scheduler thread and the /sync route must not run concurrently: two
# interleaved syncs would read the same state baseline and push the delta
# twice (and race on the state file).
SYNC_LOCK = threading.Lock()


def do_sync():
    try:
        with SYNC_LOCK:
            lines = run_sync()
        return True, "\n".join(lines) if lines else "no data"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}"


@app.get("/health")
def health():
    return Response("ok\n", mimetype="text/plain")


@app.get("/status")
def status():
    """Today's Garmin hydration state as JSON (for the Android widget)."""
    if not SYNC_KEY or not hmac.compare_digest(request.args.get("key", ""), SYNC_KEY):
        return Response("forbidden\n", status=403, mimetype="text/plain")
    try:
        return garmin_status()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}, 500


@app.get("/add")
def add():
    """Log a manual intake (widget coffee buttons) and return fresh status."""
    if not SYNC_KEY or not hmac.compare_digest(request.args.get("key", ""), SYNC_KEY):
        return Response("forbidden\n", status=403, mimetype="text/plain")
    try:
        ml = int(request.args.get("ml", ""))
    except ValueError:
        return {"error": "ml must be an integer"}, 400
    # Negative amounts undo an earlier manual addition (Garmin accepts
    # negative deltas and floors the day at 0).
    if ml == 0 or abs(ml) > 2000:
        return {"error": "ml must be non-zero and within ±2000"}, 400
    try:
        # Same lock as the sync: both rewrite the state file (the manual
        # ledger lives there), so their read-modify-write must not interleave.
        with SYNC_LOCK:
            garmin_add(ml)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}, 500
    try:
        return garmin_status()
    except Exception:
        # The add committed; report that instead of a misleading error and
        # let the widget refresh status itself.
        return {"added_ml": ml}


@app.get("/set_manual")
def set_manual():
    """Admin fix: set today's manual-intake ledger without touching Garmin."""
    if not SYNC_KEY or not hmac.compare_digest(request.args.get("key", ""), SYNC_KEY):
        return Response("forbidden\n", status=403, mimetype="text/plain")
    try:
        ml = int(request.args.get("ml", ""))
    except ValueError:
        return {"error": "ml must be an integer"}, 400
    if not 0 <= ml <= 5000:
        return {"error": "ml must be between 0 and 5000"}, 400
    try:
        with SYNC_LOCK:
            set_manual_today(ml)
        return garmin_status()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}, 500


@app.get("/sync")
def sync():
    if not SYNC_KEY or not hmac.compare_digest(request.args.get("key", ""), SYNC_KEY):
        return Response("forbidden\n", status=403, mimetype="text/plain")
    ok, msg = do_sync()
    if request.args.get("format") == "json":
        return {"ok": ok, "result": msg.splitlines()}, (200 if ok else 500)
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
