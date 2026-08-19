# WaterH → Garmin

Sync your daily water intake from a [WaterH](https://www.waterh.com/) smart
bottle into [Garmin Connect](https://connect.garmin.com/), automatically and
headlessly, with an optional one-tap manual sync from your phone.

Neither WaterH nor Garmin offers a public API for this, so this project talks to
their **private** endpoints. It's a personal, single-user tool — use it with your
own accounts and at your own risk. Nothing here contains anyone's credentials;
you supply your own via a local `.env` file.

---

## What it does

```
WaterH bottle ──BLE──▶ WaterH app ──▶ WaterH cloud (api.waterh.com)
                                            │
                                   this tool reads today's total (ml)
                                            │
                                            ▼
                            Garmin Connect  ◀── writes the difference
                              (connectapi.garmin.com hydration log)
```

- Reads your daily water total from WaterH's cloud (OAuth2 password grant).
- Writes it to Garmin's hydration log using a **self-refreshing** token.
- **Idempotent:** it reads Garmin's current value for the day and adds only the
  difference, so running it every hour never double-counts. It also reconciles
  *yesterday* on each run to catch late-arriving data.

> **Latency note:** WaterH's cloud only updates when the bottle syncs to the
> phone app over Bluetooth, so the cloud total (and therefore Garmin) lags your
> actual sips until the app has synced.

---

## How it works, and why it's built this way

Two hard problems had to be solved; both solutions are the interesting part.

1. **Getting data out of WaterH.** There's no export or public API. The intake
   data is fetched by the app from `api.waterh.com` over HTTPS. You recover the
   endpoint by intercepting your *own* app traffic once (Part A).
2. **Getting data into Garmin.** Garmin's third-party login is aggressively rate
   limited (HTTP 429, **per account, lasting 24–72h**) and its mobile app pins
   certificates. The trick is to **never use the password login**: log in once in
   a normal browser, capture a one-time *service ticket*, and exchange it for a
   durable token that refreshes itself forever (Part B).

---

## Prerequisites

- Python 3.10+ (for local capture/bootstrap) and/or Docker (for the server).
- An Android phone + a computer, for the one-time WaterH traffic capture.
- A WaterH account/bottle and a Garmin Connect account.

---

## Part A — Recover your WaterH API access (one time)

Goal: capture your WaterH **login** request and one **intake** request so you
learn the endpoints and your OAuth client credentials.

WaterH pins nothing unusual, so a non-rooted phone works:

1. **Patch the app to allow inspection.** Download the WaterH APK/bundle and run
   [`apk-mitm`](https://github.com/shroudedcode/apk-mitm) on it — this rebuilds
   it trusting user certificates and disables standard pinning. Install the
   patched build (uninstall the store version first so signatures match).
2. **Capture decrypted traffic** with [PCAPdroid](https://emanuele-f.github.io/PCAPdroid/)
   + its **mitm add-on** (install its CA as a user certificate; enable TLS
   decryption; target the WaterH app).
3. **Exercise the app** while capturing: log in, let the bottle sync, open the
   history screens.
4. **Read two requests** from PCAPdroid's connection view:
   - **Login:** `POST https://api.waterh.com/account/oauth/token/` with a JSON
     body containing `email`, `password`, `grant_type=password`, `client_id`,
     `client_secret`. → copy `client_id` and `client_secret` into your `.env`.
   - **Intake:** `GET https://api.waterh.com/account/{id}/log?period=Weekly&date_finished_from=…&date_finished_to=…`
     (unix seconds), `Authorization: Bearer …`. The response looks like:
     ```json
     {"data": null, "metrics": [{"total": 120, "total_water": 120, "trunc_date": "2026-08-17T21:00:00.000000Z"}]}
     ```
     `total_water` is ml per day; `trunc_date` is local midnight expressed in UTC.

That's all this tool needs — your account id is derived automatically from the
login token, so only `client_id`/`client_secret` (plus your email/password) go
in `.env`.

> Tip: a modern phone app that stores tokens in `localStorage` or uses the
> system trust store makes this easy; if your capture shows only encrypted
> blobs, the mitm CA wasn't trusted — recheck step 2.

---

## Part B — Get a durable Garmin token (one time)

Garmin's login endpoint is rate limited per account and can stay blocked for
days once you trigger it, and it can't be bypassed by changing IP. **So we don't
use it.** Instead we reuse your working browser session to mint a token.

1. **Log in to Garmin in a normal browser** at
   [connect.garmin.com](https://connect.garmin.com). (Ordinary browser login is
   not the throttled path.)
2. Open **DevTools → Network**, enable **Preserve log**, then visit this URL in
   the same browser:
   ```
   https://sso.garmin.com/mobile/sso/en_US/sign-in?clientId=GCM_ANDROID_DARK&service=https://mobile.integration.garmin.com/gcm/android
   ```
   Your existing session makes it redirect toward
   `https://mobile.integration.garmin.com/gcm/android?ticket=ST-…`. **That page
   won't load** — that's fine; the ticket is the point.
3. Copy the `ST-…` value from the address bar or the Network entry.
4. **Immediately** (tickets expire in seconds) exchange it:
   ```bash
   python3 garmin_bootstrap.py 'ST-....-cas'
   ```
   This writes `garmin_tokens.json` to `GARMIN_TOKENSTORE` (default
   `~/.garminconnect/`). It prints your name (or `None` if your Garmin profile
   has no display name — harmless).

The saved token contains a **refresh token** (rolls, ~30 days). As long as the
service runs at least monthly, it refreshes itself and you never repeat this.

> `curl_cffi` must be installed for the exchange/refresh to pass Garmin's
> Cloudflare TLS fingerprinting (it's in `requirements.txt`). On Python 3.10 you
> also need `typing_extensions`.

---

## Configuration

```bash
cp .env.example .env
# edit .env with your WaterH creds + captured client_id/secret, your time zone,
# and a long random SYNC_KEY:  python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```

---

## Run it

### Locally (CLI)

```bash
pip install -r requirements.txt
python3 waterh_to_garmin.py --dry-run   # prints WaterH totals, writes nothing
python3 waterh_to_garmin.py             # syncs today + yesterday to Garmin
```

### With Docker (recommended for a server)

The container runs the hourly scheduler **and** the manual `/sync` endpoint.

```bash
mkdir -p data
cp ~/.garminconnect/garmin_tokens.json data/   # the token you bootstrapped in Part B
docker compose up -d --build
docker compose logs -f
```

The token lives in the mounted `./data` volume and is refreshed in place.

---

## Manual sync from your phone (home-screen button)

The service exposes `GET /sync?key=SYNC_KEY`, which runs a sync and shows the
result. To make it a one-tap button:

1. Make sure your phone can reach the server (same LAN, or a VPN like Tailscale,
   or an HTTPS reverse proxy — see Security).
2. In your phone browser, open:
   ```
   http://YOUR_SERVER:8000/sync?key=YOUR_SYNC_KEY
   ```
   You should see “✅ Synced” with the ml totals.
3. Browser menu → **Add to Home screen**. You now have an icon that syncs on tap
   and shows the result.

---

## Widget API

For the companion Android home-screen widget (or anything else that wants
machine-readable state), the service also exposes:

- `GET /status?key=SYNC_KEY` → today's Garmin hydration state as JSON:
  ```json
  {"date": "2026-08-19", "intake_ml": 1250, "goal_base_ml": 2400,
   "sweat_loss_ml": 550, "goal_ml": 2950, "percent": 42,
   "last_entry_local": "2026-08-19T13:40:00.0"}
  ```
  `goal_ml` is the **dynamic** goal: Garmin's base goal plus the estimated
  sweat loss from your activities (Garmin's auto-increase keeps `goalInML` at
  the base value and reports sweat loss separately, so the effective goal is
  computed here).
- `GET /sync?key=SYNC_KEY&format=json` → runs a sync and returns
  `{"ok": true, "result": ["…per-day lines…"]}` instead of the HTML page.
- `GET /add?key=SYNC_KEY&ml=200` → logs a manual intake for today (the
  widget's coffee buttons) straight into Garmin and returns fresh status JSON.

Manual additions coexist with the bottle sync: the tool remembers how much
WaterH data it has already pushed per day (`sync_state.json`, stored next to
the Garmin token) and only ever adds the WaterH *increase*, so it never
swallows amounts you logged directly in Garmin.

---

## Scheduling

The Docker service syncs every `SYNC_INTERVAL_SECONDS` (default hourly) via a
built-in background thread — no cron needed. To disable it (e.g. manual-only),
set `RUN_SCHEDULER=0`.

Prefer systemd instead of Docker? Point a user timer at
`python waterh_to_garmin.py`; enable `loginctl enable-linger` so it runs without
a login session.

---

## Security notes

- `.env`, `garmin_tokens.json`, and `data/` hold live credentials — they're
  `.gitignore`d. Keep them `chmod 600` and never commit them.
- The `/sync` endpoint is guarded only by `SYNC_KEY` in the URL. Prefer **not**
  exposing it to the public internet: keep it on your LAN, put it behind a VPN
  (Tailscale/WireGuard), or front it with HTTPS (Caddy/nginx) so the key isn't
  sent in clear text.

---

## Troubleshooting

- **Garmin `429` / "rate limited":** you (or a library) hit the password login.
  Stop all attempts — retries extend the ban (24–72h). Use the Part B browser
  bootstrap, which avoids that endpoint entirely.
- **`curl_cffi not available`:** `pip install curl_cffi`; on Python 3.10 also
  `pip install typing_extensions`.
- **Garmin write seems doubled:** it shouldn't — the tool adds only the
  difference to the current daily value. If it does, open an issue.
- **`No Garmin session…`:** run `garmin_bootstrap.py` (Part B) and make sure the
  resulting `garmin_tokens.json` is where `GARMIN_TOKENSTORE` points (in Docker,
  `./data/`).
- **Idle > ~30 days:** the refresh token lapses; redo Part B once.

---

## Caveats

Both APIs are unofficial and undocumented; either vendor can change them and
break this at any time. This is a hobby integration, not a product. Run it only
against your own accounts.

## Credits

- [`python-garminconnect`](https://github.com/cyberjunky/python-garminconnect)
  for the Garmin DI-OAuth client.
- Community reverse-engineering of the Garmin auth flow
  ([peloton-to-garmin #837](https://github.com/philosowaffle/peloton-to-garmin/issues/837),
  [garth](https://github.com/matin/garth)).
