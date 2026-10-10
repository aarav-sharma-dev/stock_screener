#!/usr/bin/env python3
"""One-shot Schwab OAuth → writes ``.schwab_token.json``.

Prereqs: approved app on developer.schwab.com; ``SCHWAB_API_KEY`` +
``SCHWAB_APP_SECRET`` (+ matching ``SCHWAB_CALLBACK_URL``) in ``.env``.

If the local HTTPS callback is awkward, use ``--manual`` and paste the
``code=`` query param from the redirect URL.

Prefer the project venv::

  .venv/bin/python scripts/schwab_auth.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

ROOT = Path(__file__).resolve().parents[1]
_VENV_PY = ROOT / ".venv" / "bin" / "python"

sys.path.insert(0, str(ROOT / "scripts"))
from _venv_bootstrap import reexec_venv_if_needed  # noqa: E402

reexec_venv_if_needed(ROOT)
sys.path.insert(0, str(ROOT / "src"))

try:
    from stock_screener.data.schwab import (
        AUTH_URL,
        SchwabNotConfiguredError,
        exchange_authorization_code,
        get_client,
        interactive_oauth,
        schwab_env,
    )
except ModuleNotFoundError as exc:
    hint = (
        f"{_VENV_PY} scripts/schwab_auth.py"
        if _VENV_PY.exists()
        else "python3 -m venv .venv && .venv/bin/pip install -e . && .venv/bin/python scripts/schwab_auth.py"
    )
    print(f"Missing dependency: {exc.name}\nUse:\n  {hint}", file=sys.stderr)
    raise SystemExit(1) from exc


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--manual",
        action="store_true",
        help="Print authorize URL; paste redirect URL or code afterward",
    )
    p.add_argument("--code", default="", help="Authorization code (manual flow)")
    args = p.parse_args(argv)

    cfg = schwab_env()
    print("Schwab OAuth")
    print(f"  callback: {cfg['callback_url']}")
    print(f"  token →   {cfg['token_path']}")
    if not cfg["api_key"] or not cfg["app_secret"]:
        print("Error: set SCHWAB_API_KEY and SCHWAB_APP_SECRET in .env", file=sys.stderr)
        return 1

    try:
        if args.code.strip():
            exchange_authorization_code(args.code.strip())
        elif args.manual:
            params = {
                "client_id": cfg["api_key"],
                "redirect_uri": cfg["callback_url"],
                "response_type": "code",
            }
            url = f"{AUTH_URL}?{urlencode(params)}"
            print(f"\n1) Open:\n{url}\n")
            pasted = input("2) Paste full redirect URL (or just code= value): ").strip()
            if "code=" in pasted:
                qs = parse_qs(urlparse(pasted).query)
                code = (qs.get("code") or [""])[0]
            else:
                code = pasted
            if not code:
                print("No code provided", file=sys.stderr)
                return 1
            exchange_authorization_code(code)
        else:
            interactive_oauth()
    except (SchwabNotConfiguredError, Exception) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        print("Tip: retry with --manual if the local callback listener fails.", file=sys.stderr)
        return 1

    try:
        client = get_client(interactive=False)
        q = client.get_quotes(["SPY"])
        last = ((q.get("SPY") or {}).get("quote") or {}).get("lastPrice")
        print(f"OK · market data only (SPY last={last})")
    except Exception as exc:  # noqa: BLE001
        print(f"Token saved but quote probe failed: {exc}", file=sys.stderr)
    print("Token ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
