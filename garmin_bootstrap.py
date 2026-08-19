#!/usr/bin/env python3
"""Bootstrap a Garmin tokenstore from a browser-captured CAS service ticket.

Garmin's password login is aggressively rate-limited (HTTP 429, per-account,
lasting days). This avoids it entirely: you log in via your browser (which
works), capture a one-time service ticket, and exchange it here for a durable,
self-refreshing token.

See the README ("Part B — Get a Garmin token") for how to capture the ticket.
Service tickets expire in SECONDS, so run this immediately after capturing:

    python3 garmin_bootstrap.py 'ST-....-cas'

The resulting session is written to GARMIN_TOKENSTORE (default ~/.garminconnect).
Copy that file to your server's tokenstore volume to deploy.
"""
import sys

from garminconnect import Garmin

from waterh_to_garmin import GARMIN_TOKENSTORE


def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip().startswith("ST-"):
        sys.exit("Usage: python3 garmin_bootstrap.py 'ST-...ticket...'")
    ticket = sys.argv[1].strip()

    g = Garmin()                       # no email/password -> no rate-limited login
    c = g.client
    c._exchange_service_ticket(ticket)  # mints di_token + di_refresh_token
    c.dump(str(GARMIN_TOKENSTORE))

    print(f"Saved tokenstore to {GARMIN_TOKENSTORE} (client_id={c.di_client_id})")
    try:
        print("Authenticated as:", g.get_full_name())
    except Exception as e:
        print("Tokens saved, but a verification call failed:", e)


if __name__ == "__main__":
    main()
