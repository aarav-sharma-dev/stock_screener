"""Charles Schwab Trader API (Individual) — OAuth + quotes/history.

Works on Python 3.9+ via ``requests`` (no ``schwab-py`` required for REST).

Setup:
  1. App at https://developer.schwab.com/products/trader-api--individual
  2. Callback URL must match ``SCHWAB_CALLBACK_URL`` (default ``https://127.0.0.1:8182/``)
  3. ``SCHWAB_API_KEY`` + ``SCHWAB_APP_SECRET`` in ``.env``
  4. ``python scripts/schwab_auth.py`` (browser login once)

Optional streamer: install ``schwab-py`` on Python ≥3.10, or use
``scripts/schwab_stream_watchlist.py`` which falls back to quote polling.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TOKEN_PATH = ROOT / ".schwab_token.json"
DEFAULT_CALLBACK = "https://127.0.0.1:8182/"

AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
MARKETDATA = "https://api.schwabapi.com/marketdata/v1"
TRADER = "https://api.schwabapi.com/trader/v1"


class SchwabNotConfiguredError(RuntimeError):
    """Missing credentials, token, or failed OAuth."""


class SchwabAPIError(RuntimeError):
    """HTTP / API error from Schwab."""


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def schwab_env() -> dict[str, str]:
    _load_dotenv()
    return {
        "api_key": (os.getenv("SCHWAB_API_KEY") or os.getenv("SCHWAB_CLIENT_ID") or "").strip(),
        "app_secret": (
            os.getenv("SCHWAB_APP_SECRET") or os.getenv("SCHWAB_CLIENT_SECRET") or ""
        ).strip(),
        "callback_url": (os.getenv("SCHWAB_CALLBACK_URL") or DEFAULT_CALLBACK).strip(),
        "token_path": (os.getenv("SCHWAB_TOKEN_PATH") or str(DEFAULT_TOKEN_PATH)).strip(),
        "account_id": (os.getenv("SCHWAB_ACCOUNT_ID") or "").strip(),
    }


def schwab_configured() -> bool:
    cfg = schwab_env()
    return bool(cfg["api_key"] and cfg["app_secret"])


def _basic_auth_header(api_key: str, app_secret: str) -> str:
    raw = f"{api_key}:{app_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _load_token(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _alert_day() -> str:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/Chicago")).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return datetime.now().strftime("%Y-%m-%d")


def _login_alert_path() -> Path:
    return ROOT / "output" / "schwab_login_alert.txt"


def notify_schwab_login(reason: str) -> None:
    """Slack once per Chicago day when the Schwab token cannot be refreshed."""
    day = _alert_day()
    marker = _login_alert_path()
    try:
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == day:
            logger.error("Schwab login still expired (%s)", reason[:200])
            return
    except OSError:
        pass
    text = (
        "Schwab login expired or was rejected. Price and fundamental screens "
        "will miss data until you re-run scripts/schwab_auth.py on the machine "
        f"that holds the token.\n{reason[:300]}"
    )
    logger.error(text)
    _load_dotenv()
    webhook = (
        os.getenv("SCREENER_SLACK_WEBHOOK") or os.getenv("SLACK_WEBHOOK_URL") or ""
    ).strip()
    if webhook:
        try:
            requests.post(webhook, json={"text": text}, timeout=15)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Schwab login Slack failed: %s", exc.__class__.__name__)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(day, encoding="utf-8")
    except OSError:
        pass


def clear_schwab_login_alert() -> None:
    try:
        _login_alert_path().unlink(missing_ok=True)
    except OSError:
        pass


def _save_token(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _token_expired(token: dict[str, Any], skew_sec: int = 60) -> bool:
    exp = token.get("expires_at")
    if exp is None:
        # Schwab access tokens ~30m; if missing stamp, refresh proactively
        return True
    return time.time() >= float(exp) - skew_sec


def _exchange_token(
    *,
    api_key: str,
    app_secret: str,
    data: dict[str, str],
) -> dict[str, Any]:
    headers = {
        "Authorization": _basic_auth_header(api_key, app_secret),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    resp = requests.post(TOKEN_URL, headers=headers, data=data, timeout=30)
    if resp.status_code >= 400:
        raise SchwabAPIError(f"Token error {resp.status_code}: {resp.text[:400]}")
    payload = resp.json()
    payload["expires_at"] = time.time() + float(payload.get("expires_in") or 1800)
    payload["obtained_at"] = time.time()
    return payload


def refresh_access_token(token: dict[str, Any], cfg: dict[str, str] | None = None) -> dict[str, Any]:
    cfg = cfg or schwab_env()
    refresh = token.get("refresh_token")
    if not refresh:
        notify_schwab_login("Token file has no refresh_token")
        raise SchwabNotConfiguredError("Token missing refresh_token; re-run schwab_auth.py")
    try:
        new = _exchange_token(
            api_key=cfg["api_key"],
            app_secret=cfg["app_secret"],
            data={"grant_type": "refresh_token", "refresh_token": refresh},
        )
    except SchwabAPIError as exc:
        text = str(exc).lower()
        if any(bit in text for bit in ("400", "401", "invalid_grant", "unsupported_token")):
            notify_schwab_login(str(exc))
        raise
    clear_schwab_login_alert()
    # Preserve refresh_token if response omits it
    if "refresh_token" not in new and refresh:
        new["refresh_token"] = refresh
    return new


def interactive_oauth(*, timeout_sec: float = 300.0) -> dict[str, Any]:
    """Browser OAuth with local callback listener; returns and saves token dict."""
    cfg = schwab_env()
    if not cfg["api_key"] or not cfg["app_secret"]:
        raise SchwabNotConfiguredError(
            "Set SCHWAB_API_KEY and SCHWAB_APP_SECRET in .env"
        )
    callback = urlparse(cfg["callback_url"])
    host = callback.hostname or "127.0.0.1"
    port = callback.port or (443 if callback.scheme == "https" else 80)
    # Local listener only supports http on loopback; Schwab apps often use https://127.0.0.1:8182/
    listen_port = port if port not in (80, 443) else 8182
    state = secrets.token_urlsafe(16)
    result: dict[str, Any] = {"code": None, "error": None, "state": None}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            qs = parse_qs(urlparse(self.path).query)
            result["code"] = (qs.get("code") or [None])[0]
            result["error"] = (qs.get("error") or [None])[0]
            result["state"] = (qs.get("state") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h3>Schwab auth OK - you can close this tab.</h3></body></html>"
            )

        def log_message(self, fmt, *args):  # noqa: A003
            return

    # Prefer binding HTTP on the callback port (Schwab redirects to https URL;
    # for 127.0.0.1 many apps accept http listener if callback registered as https —
    # if that fails, user can paste code manually via --manual in auth script).
    server = HTTPServer((host if host != "127.0.0.1" else "127.0.0.1", listen_port), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    params = {
        "client_id": cfg["api_key"],
        "redirect_uri": cfg["callback_url"],
        "response_type": "code",
        "state": state,
    }
    url = f"{AUTH_URL}?{urlencode(params)}"
    print(f"Open this URL if the browser does not launch:\n{url}\n")
    webbrowser.open(url)

    deadline = time.time() + timeout_sec
    while time.time() < deadline and result["code"] is None and result["error"] is None:
        time.sleep(0.2)
    server.server_close()

    if result["error"]:
        raise SchwabAPIError(f"OAuth error: {result['error']}")
    if not result["code"]:
        raise SchwabNotConfiguredError(
            "Timed out waiting for OAuth callback. "
            "Ensure callback URL matches the Schwab app and port is free."
        )
    if result["state"] and result["state"] != state:
        raise SchwabAPIError("OAuth state mismatch")

    token = _exchange_token(
        api_key=cfg["api_key"],
        app_secret=cfg["app_secret"],
        data={
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": cfg["callback_url"],
        },
    )
    _save_token(Path(cfg["token_path"]), token)
    return token


def exchange_authorization_code(code: str) -> dict[str, Any]:
    """Manual paste flow when local HTTPS callback is awkward."""
    cfg = schwab_env()
    token = _exchange_token(
        api_key=cfg["api_key"],
        app_secret=cfg["app_secret"],
        data={
            "grant_type": "authorization_code",
            "code": code.strip(),
            "redirect_uri": cfg["callback_url"],
        },
    )
    _save_token(Path(cfg["token_path"]), token)
    return token


class SchwabClient:
    """Thin REST client with automatic token refresh."""

    def __init__(self, token: dict[str, Any] | None = None):
        cfg = schwab_env()
        if not cfg["api_key"] or not cfg["app_secret"]:
            raise SchwabNotConfiguredError(
                "Set SCHWAB_API_KEY and SCHWAB_APP_SECRET in .env"
            )
        self.cfg = cfg
        self.token_path = Path(cfg["token_path"])
        self.token = token or _load_token(self.token_path)
        if not self.token:
            notify_schwab_login(f"No token at {self.token_path}")
            raise SchwabNotConfiguredError(
                f"No token at {self.token_path}. Run: python scripts/schwab_auth.py"
            )
        self._lock = threading.Lock()

    def _access_token(self) -> str:
        with self._lock:
            if _token_expired(self.token):
                self.token = refresh_access_token(self.token, self.cfg)
                _save_token(self.token_path, self.token)
            return str(self.token["access_token"])

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json_body: Any = None,
    ) -> Any:
        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "Accept": "application/json",
        }
        resp = requests.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json_body,
            timeout=45,
        )
        for attempt in range(3):
            if resp.status_code != 429:
                break
            wait = float(resp.headers.get("Retry-After") or (1 + attempt))
            time.sleep(min(max(wait, 0.5), 8))
            resp = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=45,
            )
        if resp.status_code == 401:
            # Force refresh once
            with self._lock:
                self.token = refresh_access_token(self.token, self.cfg)
                _save_token(self.token_path, self.token)
            headers["Authorization"] = f"Bearer {self.token['access_token']}"
            resp = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=45,
            )
        if resp.status_code >= 400:
            raise SchwabAPIError(f"{method} {url} → {resp.status_code}: {resp.text[:400]}")
        if not resp.content:
            return None
        return resp.json()

    def get_account_numbers(self) -> list[dict]:
        data = self.request("GET", f"{TRADER}/accounts/accountNumbers")
        return data if isinstance(data, list) else []

    def get_quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        if not symbols:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(symbols), 50):
            batch = [s.upper() for s in symbols[i : i + 50]]
            payload = self.request(
                "GET",
                f"{MARKETDATA}/quotes",
                params={"symbols": ",".join(batch), "fields": "quote,reference"},
            )
            if isinstance(payload, dict):
                for sym, row in payload.items():
                    if isinstance(row, dict):
                        out[str(sym).upper()] = row
        return out

    def get_price_history(
        self,
        symbol: str,
        *,
        period_type: str = "month",
        period: int = 1,
        frequency_type: str = "daily",
        frequency: int = 1,
        start_ms: int | None = None,
        end_ms: int | None = None,
        need_extended_hours_data: bool = False,
    ) -> dict:
        params: dict[str, Any] = {
            "periodType": period_type,
            "period": period,
            "frequencyType": frequency_type,
            "frequency": frequency,
            "needExtendedHoursData": str(need_extended_hours_data).lower(),
        }
        if start_ms is not None:
            params["startDate"] = start_ms
        if end_ms is not None:
            params["endDate"] = end_ms
        return self.request(
            "GET",
            f"{MARKETDATA}/pricehistory",
            params={**params, "symbol": symbol.upper()},
        ) or {}


def get_client(*, interactive: bool = False) -> SchwabClient:
    try:
        return SchwabClient()
    except SchwabNotConfiguredError:
        if not interactive:
            raise
        interactive_oauth()
        return SchwabClient()


def quotes(symbols: list[str], client: SchwabClient | None = None) -> dict[str, dict[str, Any]]:
    client = client or get_client()
    return client.get_quotes(symbols)


def instrument_fundamental(
    symbol: str, client: SchwabClient | None = None
) -> dict[str, Any]:
    """Fundamental block from ``/instruments?projection=fundamental``.

    Schwab's market data API has this snapshot. It does not include a news
    feed, the next earnings date, or an EPS surprise.
    """
    client = client or get_client()
    sym = to_schwab_symbol(symbol)
    payload = client.request(
        "GET",
        f"{MARKETDATA}/instruments",
        params={"symbol": sym, "projection": "fundamental"},
    )
    if not isinstance(payload, dict):
        return {}
    instruments = payload.get("instruments")
    if isinstance(instruments, list):
        for row in instruments:
            if not isinstance(row, dict):
                continue
            block = row.get("fundamental")
            if isinstance(block, dict) and block:
                return block
        if instruments and isinstance(instruments[0], dict):
            block = instruments[0].get("fundamental")
            return block if isinstance(block, dict) else {}
    for row in payload.values():
        if isinstance(row, dict):
            block = row.get("fundamental")
            if isinstance(block, dict) and block:
                return block
    return {}


def quote_last(symbol: str, client: SchwabClient | None = None) -> float | None:
    data = quotes([symbol], client=client).get(symbol.upper())
    if not data:
        return None
    q = data.get("quote") or data
    for key in ("lastPrice", "mark", "closePrice", "regularMarketLastPrice"):
        try:
            val = float((q or {}).get(key) or 0)
        except (TypeError, ValueError):
            continue
        if val > 0:
            return val
    return None


def _ms(ts) -> int:
    import pandas as pd

    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return int(t.timestamp() * 1000)


def _candles_to_frame(payload: dict):
    import pandas as pd

    candles = (payload or {}).get("candles") or []
    if not candles:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    rows = []
    for c in candles:
        ts = pd.to_datetime(c.get("datetime"), unit="ms", utc=True)
        rows.append(
            {
                "datetime": ts.tz_convert("America/New_York"),
                "Open": float(c.get("open", 0) or 0),
                "High": float(c.get("high", 0) or 0),
                "Low": float(c.get("low", 0) or 0),
                "Close": float(c.get("close", 0) or 0),
                "Volume": float(c.get("volume", 0) or 0),
            }
        )
    return pd.DataFrame(rows).set_index("datetime").sort_index()


def to_schwab_symbol(symbol: str) -> str:
    """Map a screener ticker onto a Schwab market-data symbol.

    Class shares use a slash (``BRK-B`` → ``BRK/B``). Index quotes use the
    Schwab ``$`` prefix.
    """
    s = symbol.strip().upper()
    if s in {"^VIX", "VIX", "$VIX"}:
        return "$VIX"
    if s in {"^GSPC", "^SPX", "SPX", "$SPX"}:
        return "$SPX"
    return s.replace("-", "/")


def _year_period_for_span(span_days: int) -> int:
    """Schwab ``periodType=year`` only accepts 1, 2, 3, 5, 10, 15, 20."""
    if span_days <= 370:
        return 1
    if span_days <= 2 * 370:
        return 2
    if span_days <= 3 * 370:
        return 3
    if span_days <= 5 * 370:
        return 5
    if span_days <= 10 * 370:
        return 10
    if span_days <= 15 * 370:
        return 15
    return 20


def price_history_daily(
    symbol: str,
    *,
    start=None,
    end=None,
    client: SchwabClient | None = None,
):
    import pandas as pd

    client = client or get_client()
    end = end or pd.Timestamp.now(tz="UTC")
    start = start or (end - pd.Timedelta(days=365))
    span_days = max(1, int((pd.Timestamp(end) - pd.Timestamp(start)).days))
    payload = client.get_price_history(
        symbol,
        period_type="year",
        period=_year_period_for_span(span_days),
        frequency_type="daily",
        frequency=1,
        start_ms=_ms(start),
        end_ms=_ms(end),
    )
    return _candles_to_frame(payload)


def price_history_30m(
    symbol: str,
    *,
    start=None,
    end=None,
    extended: bool = False,
    client: SchwabClient | None = None,
):
    import pandas as pd

    client = client or get_client()
    end = end or pd.Timestamp.now(tz="UTC")
    start = start or (end - pd.Timedelta(days=10))
    payload = client.get_price_history(
        symbol,
        period_type="day",
        period=10,
        frequency_type="minute",
        frequency=30,
        start_ms=_ms(start),
        end_ms=_ms(end),
        need_extended_hours_data=extended,
    )
    return _candles_to_frame(payload)


def to_rth_4h(bars_30m):
    """Resample 30m → RTH 4h (09:30 / 13:30 ET) — matches production athdip."""
    import pandas as pd

    if bars_30m.empty:
        return bars_30m
    df = bars_30m.copy()
    if df.index.tz is None:
        df.index = df.index.tz_localize("America/New_York")
    else:
        df.index = df.index.tz_convert("America/New_York")
    minutes = df.index.hour * 60 + df.index.minute
    rth = df[(minutes >= 9 * 60 + 30) & (minutes < 16 * 60)].copy()
    if rth.empty:
        return rth

    def _bucket(ts: pd.Timestamp) -> pd.Timestamp:
        m = ts.hour * 60 + ts.minute
        if m < 13 * 60 + 30:
            return ts.normalize() + pd.Timedelta(hours=9, minutes=30)
        return ts.normalize() + pd.Timedelta(hours=13, minutes=30)

    rth = rth.copy()
    rth["_bucket"] = [_bucket(ts) for ts in rth.index]
    agg = (
        rth.groupby("_bucket")
        .agg(
            Open=("Open", "first"),
            High=("High", "max"),
            Low=("Low", "min"),
            Close=("Close", "last"),
            Volume=("Volume", "sum"),
        )
        .sort_index()
    )
    agg.index.name = "datetime"
    return agg


def price_history_rth_4h(symbol: str, *, client: SchwabClient | None = None, **kwargs):
    return to_rth_4h(price_history_30m(symbol, client=client, extended=False, **kwargs))


def enrich_last_prices(symbols: list[str]) -> dict[str, float]:
    if not schwab_configured():
        return {}
    try:
        client = get_client(interactive=False)
        raw = quotes(symbols, client=client)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Schwab quote enrich skipped: %s", exc)
        return {}
    out: dict[str, float] = {}
    for sym, data in raw.items():
        q = data.get("quote") or data
        for key in ("lastPrice", "mark", "closePrice"):
            try:
                val = float((q or {}).get(key) or 0)
            except (TypeError, ValueError):
                continue
            if val > 0:
                out[sym] = val
                break
    return out
