#!/usr/bin/env python3
"""Rotate the Schwab refresh token and save it.

This is the unattended keepalive. It does not open a browser. When the
refresh token is already dead, Schwab rejects it and the existing login
alert fires; run ``scripts/schwab_auth.py`` on that machine to sign in again.

Weekday scans already rotate the token whenever they call Schwab. This
script is the weekend job so a quiet week still stays inside the 7-day
refresh-token window.

Prints only expiry metadata, never the token.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from _venv_bootstrap import reexec_venv_if_needed  # noqa: E402

reexec_venv_if_needed(ROOT)
sys.path.insert(0, str(ROOT / "src"))

from stock_screener.data.schwab import (  # noqa: E402
    SchwabAPIError,
    SchwabNotConfiguredError,
    _load_token,
    _save_token,
    notify_schwab_login,
    refresh_access_token,
    schwab_env,
)


def main() -> int:
    cfg = schwab_env()
    path = Path(cfg["token_path"])
    if not cfg["api_key"] or not cfg["app_secret"]:
        print("Schwab refresh failed: SCHWAB_API_KEY and SCHWAB_APP_SECRET are missing", file=sys.stderr)
        return 1
    token = _load_token(path)
    if not token:
        notify_schwab_login(f"No token at {path}")
        print(f"Schwab refresh failed: no token at {path}", file=sys.stderr)
        return 1
    try:
        fresh = refresh_access_token(token, cfg)
    except (SchwabAPIError, SchwabNotConfiguredError) as exc:
        print(f"Schwab refresh failed: {exc}", file=sys.stderr)
        return 1
    _save_token(path, fresh)
    expires_in = int(float(fresh.get("expires_in") or 0))
    rotated = "refresh_token" in fresh
    print(
        f"Schwab token refreshed. Access token expires in {expires_in}s. "
        f"Refresh token {'rotated' if rotated else 'kept'}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
