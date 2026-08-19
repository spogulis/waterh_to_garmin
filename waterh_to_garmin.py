#!/usr/bin/env python3
"""Sync daily water intake from WaterH to Garmin Connect.

Config is read from environment variables, falling back to a local
`waterh-garmin.env` file (KEY=VALUE lines) next to this script. See
`.env.example` for the keys.

Safe to run repeatedly: it tops Garmin up to match WaterH's daily total
(reads Garmin's current value and adds only the difference), so re-runs
never double-count.
"""
import base64
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from garminconnect import Garmin

# --- config -----------------------------------------------------------------
WATERH_TZ = os.environ.get("WATERH_TZ", "Europe/Riga")   # local day boundaries
TZ = ZoneInfo(WATERH_TZ)
WATERH_TOKEN_URL = "https://api.waterh.com/account/oauth/token/"
WATERH_API = "https://api.waterh.com"
ENV_PATH = Path(__file__).with_name("waterh-garmin.env")
# Where python-garminconnect stores its refreshable session. In Docker this is
# set to a mounted volume (e.g. /data) so the token survives restarts.
GARMIN_TOKENSTORE = Path(
    os.environ.get("GARMIN_TOKENSTORE", str(Path.home() / ".garminconnect"))
)
COMMON_HEADERS = {
    "user-agent": "okhttp/4.12.0",
    "x-app-version": "3.5.1",
    "time-zone": WATERH_TZ,
}
REQUIRED = ("WATERH_EMAIL", "WATERH_PASSWORD", "WATERH_CLIENT_ID", "WATERH_CLIENT_SECRET")


def load_config():
    """Read the .env file (if present), then let real env vars override it."""
    cfg = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in (*REQUIRED, "GARMIN_EMAIL", "GARMIN_PASSWORD"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    missing = [k for k in REQUIRED if not cfg.get(k)]
    if missing:
        raise SystemExit(
            f"Missing config: {', '.join(missing)} "
            f"(set them in {ENV_PATH.name} or as environment variables)"
        )
    return cfg


def waterh_login(cfg):
    r = requests.post(
        WATERH_TOKEN_URL,
        json={
            "email": cfg["WATERH_EMAIL"],
            "password": cfg["WATERH_PASSWORD"],
            "grant_type": "password",
            "client_id": cfg["WATERH_CLIENT_ID"],
            "client_secret": cfg["WATERH_CLIENT_SECRET"],
        },
        headers={**COMMON_HEADERS, "content-type": "application/json; charset=UTF-8"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def account_id_from_token(token):
    """The WaterH account id is the JWT 'sub' claim."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)          # pad base64url
    return json.loads(base64.urlsafe_b64decode(payload))["sub"]


def waterh_daily_ml(token, account_id):
    """Return {date: total_water_ml} for the last 7 local days."""
    now = datetime.now(TZ)
    end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=7)
    r = requests.get(
        f"{WATERH_API}/account/{account_id}/log",
        params={
            "period": "Weekly",
            "date_finished_from": int(start.timestamp()),
            "date_finished_to": int(end.timestamp()),
        },
        headers={**COMMON_HEADERS, "authorization": f"Bearer {token}", "accept-encoding": "gzip"},
        timeout=30,
    )
    r.raise_for_status()
    daily = {}
    for m in r.json().get("metrics") or []:
        # trunc_date is local midnight expressed in UTC -> convert back to local date
        dt = datetime.fromisoformat(m["trunc_date"].replace("Z", "+00:00")).astimezone(TZ)
        daily[dt.date()] = float(m.get("total_water") or 0)
    return daily


def garmin_connect():
    """Resume the cached, self-refreshing Garmin session. Never logs in with a
    password here (that endpoint is aggressively rate-limited). Create the
    session once with garmin_bootstrap.py."""
    if not GARMIN_TOKENSTORE.exists():
        raise SystemExit(
            f"No Garmin session at {GARMIN_TOKENSTORE}. "
            "Bootstrap one first: python3 garmin_bootstrap.py 'ST-...'"
        )
    g = Garmin()
    g.login(str(GARMIN_TOKENSTORE))
    return g


def sync_day(garmin, cdate, waterh_ml):
    cdate_str = cdate.isoformat()
    current = garmin.get_hydration_data(cdate_str) or {}
    garmin_ml = current.get("valueInML") or 0
    delta = waterh_ml - garmin_ml
    if delta >= 1:
        garmin.add_hydration_data(value_in_ml=float(delta), cdate=cdate_str)
    return garmin_ml, max(delta, 0)


def garmin_status():
    """Read today's hydration state from Garmin for the phone widget.

    Garmin's auto-increasing daily goal is base goal + estimated sweat loss
    from activities; the API keeps goalInML at the base value and reports
    sweat loss separately, so the effective goal is computed here.
    """
    garmin = garmin_connect()
    today = datetime.now(TZ).date()
    data = garmin.get_hydration_data(today.isoformat()) or {}
    intake = float(data.get("valueInML") or 0)
    goal_base = float(data.get("goalInML") or 0)
    sweat = float(data.get("sweatLossInML") or 0)
    goal = goal_base + sweat
    return {
        "date": today.isoformat(),
        "intake_ml": round(intake),
        "goal_base_ml": round(goal_base),
        "sweat_loss_ml": round(sweat),
        "goal_ml": round(goal),
        "percent": round(100 * intake / goal) if goal > 0 else 0,
        "last_entry_local": data.get("lastEntryTimestampLocal"),
    }


def run_sync(dry_run=False):
    """Do the sync and return a list of human-readable result lines."""
    cfg = load_config()
    token = waterh_login(cfg)
    account_id = account_id_from_token(token)
    daily = waterh_daily_ml(token, account_id)

    out = []
    if dry_run:
        for d in sorted(daily):
            out.append(f"{d}: {daily[d]:.0f} ml (dry-run, no Garmin write)")
        return out

    garmin = garmin_connect()
    today = datetime.now(TZ).date()
    for cdate in (today, today - timedelta(days=1)):   # today + reconcile yesterday
        ml = daily.get(cdate, 0)
        if ml <= 0:
            out.append(f"{cdate}: no WaterH data")
            continue
        before, added = sync_day(garmin, cdate, ml)
        out.append(f"{cdate}: WaterH={ml:.0f}ml  Garmin_before={before:.0f}ml  added={added:.0f}ml")
    return out


def main():
    for line in run_sync(dry_run="--dry-run" in sys.argv):
        print(line)


if __name__ == "__main__":
    main()
