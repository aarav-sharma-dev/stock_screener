#!/usr/bin/env python3
"""Detect newly signaled tickers vs the previous screen snapshot and alert.

Compares current ``output/screen_*.csv`` (+ optional ``today_focus.csv``) to
``output/signal_snapshot.json``. First run only seeds the baseline (no flood).

Alerts (any that are configured):
  - stdout (always)
  - macOS Notification Center (default on Darwin; ``--no-desktop`` to skip)
  - Slack incoming webhook if ``SCREENER_SLACK_WEBHOOK`` or ``SLACK_WEBHOOK_URL`` is set

Per-mode Slack / alert filters:
  - ``--modes athdip,support`` or env ``SCREENER_SLACK_MODES=athdip,support``
  - Only new symbols touching those modes (or focus rows tagged with them) alert
  - Omit / empty = all modes (legacy behavior)

Optional Schwab live last price on alert lines (default **on** when OAuth
token exists; disable with ``--no-schwab-quotes`` or
``SCREENER_SLACK_SCHWAB_QUOTES=0``).

When ``output/institutions_13f_history.csv`` exists, a new ticker held by
two or more tracked managers — or inside one manager's top 10 weights —
gets a ``13F:`` note. Refresh that file with
``python src/stock_screener/data/institutions_history.py 8``.

Near-real-time: re-run the daily screen on an interval (e.g. Cursor ``/loop 30m``
or cron), then this script — the screener itself is batch, not a live stream.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from _venv_bootstrap import reexec_venv_if_needed  # noqa: E402

reexec_venv_if_needed(ROOT)

import pandas as pd  # noqa: E402
import requests  # noqa: E402

OUTPUT = ROOT / "output"
SNAPSHOT = OUTPUT / "signal_snapshot.json"
MODES = (
    "support",
    "catalyst",
    "fundamentals",
    "politicians",
    "trump",
    "athdip",
    "pead",
)


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


def _fnum(x) -> float | None:
    if x is None or (isinstance(x, float) and x != x):
        return None
    try:
        if pd.isna(x):
            return None
    except Exception:  # noqa: BLE001
        pass
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _tickers_from_csv(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        df = pd.read_csv(path)
    except Exception:  # noqa: BLE001
        return []
    if "ticker" not in df.columns:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for t in df["ticker"].astype(str):
        t = t.strip().upper()
        if not t or t == "NAN" or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _metrics_from_modes(output: Path) -> dict[str, dict]:
    """Best-effort RSI / politician flow per ticker from screen_*.csv."""
    metrics: dict[str, dict] = {}
    for mode in MODES:
        path = output / f"screen_{mode}.csv"
        if not path.exists():
            continue
        try:
            df = pd.read_csv(path)
        except Exception:  # noqa: BLE001
            continue
        if "ticker" not in df.columns:
            continue
        for _, row in df.iterrows():
            t = str(row.get("ticker") or "").strip().upper()
            if not t:
                continue
            m = metrics.setdefault(t, {})
            rsi = _fnum(row.get("rsi14"))
            if rsi is not None:
                m.setdefault("rsi14", rsi)
            if mode == "athdip":
                rsi4 = _fnum(row.get("rsi_4h"))
                # ScreenRow defaults rsi_4h=50 for non-athdip; only trust athdip rows
                if rsi4 is not None:
                    m["rsi4h"] = rsi4
            if mode == "pead":
                ear = _fnum(row.get("ear_pct"))
                sur = _fnum(row.get("surprise_pct"))
                if ear is not None:
                    m["ear"] = ear
                if sur is not None:
                    m["surprise"] = sur
            if mode == "politicians":
                buys = _fnum(row.get("buy_count"))
                sells = _fnum(row.get("sell_count"))
                if buys is not None:
                    m["pol_buys"] = int(buys)
                if sells is not None:
                    m["pol_sells"] = int(sells)
    return metrics


def classify_signal(
    kind: str,
    tags: str,
    *,
    pol_buys: int = 0,
    pol_sells: int = 0,
) -> str:
    """Long-only watchlist lean: BUY / SELL / MIXED."""
    kind_l = (kind or "").lower()
    tags_l = (tags or "").lower()
    pol_sell_lean = "politicians" in tags_l and pol_sells > pol_buys
    long_modes = any(
        m in tags_l
        for m in ("athdip", "pead", "support", "catalyst", "fundamentals", "trump")
    )
    if kind_l == "conflict" or (pol_sell_lean and long_modes):
        return "MIXED"
    if pol_sell_lean and not long_modes:
        return "SELL"
    return "BUY"


def _fmt_rsi(rsi14: float | None, rsi4h: float | None) -> str:
    parts: list[str] = []
    if rsi14 is not None:
        parts.append(f"RSI {rsi14:.0f}")
    if rsi4h is not None:
        parts.append(f"4h {rsi4h:.0f}")
    return " / ".join(parts)


def collect_current(output: Path = OUTPUT) -> dict:
    by_mode: dict[str, list[str]] = {}
    for mode in MODES:
        path = output / f"screen_{mode}.csv"
        if not path.exists() and mode == "support":
            alt = output / "screen.csv"
            if alt.exists():
                path = alt
        by_mode[mode] = _tickers_from_csv(path)

    metrics = _metrics_from_modes(output)
    focus_rows: list[dict] = []
    focus_path = output / "today_focus.csv"
    if focus_path.exists():
        try:
            df = pd.read_csv(focus_path)
            for _, row in df.iterrows():
                t = str(row.get("ticker") or "").strip().upper()
                if not t:
                    continue
                kind = str(row.get("kind") or "")
                tags = str(row.get("tags") or "")
                buys = int(_fnum(row.get("polBuys")) or metrics.get(t, {}).get("pol_buys") or 0)
                sells = int(
                    _fnum(row.get("polSells")) or metrics.get(t, {}).get("pol_sells") or 0
                )
                signal = str(row.get("signal") or "").strip().upper()
                if signal not in {"BUY", "SELL", "MIXED"}:
                    signal = classify_signal(
                        kind, tags, pol_buys=buys, pol_sells=sells
                    )
                rsi14 = _fnum(row.get("rsi14"))
                if rsi14 is None:
                    rsi14 = metrics.get(t, {}).get("rsi14")
                rsi4h = _fnum(row.get("rsi4h"))
                if rsi4h is None:
                    rsi4h = metrics.get(t, {}).get("rsi4h")
                focus_rows.append(
                    {
                        "ticker": t,
                        "kind": kind,
                        "signal": signal,
                        "why": str(row.get("why") or "")[:160],
                        "tags": tags,
                        "rsi14": rsi14,
                        "rsi4h": rsi4h,
                        "pol_buys": buys,
                        "pol_sells": sells,
                    }
                )
        except Exception:  # noqa: BLE001
            focus_rows = []

    union: list[str] = []
    seen: set[str] = set()
    for mode in MODES:
        for t in by_mode.get(mode) or []:
            if t not in seen:
                seen.add(t)
                union.append(t)

    ticker_meta: dict[str, dict] = {}
    for t in union:
        m = metrics.get(t) or {}
        modes = [mode for mode, ts in by_mode.items() if t in (ts or [])]
        tags = " · ".join(modes)
        buys = int(m.get("pol_buys") or 0)
        sells = int(m.get("pol_sells") or 0)
        ticker_meta[t] = {
            "signal": classify_signal("", tags, pol_buys=buys, pol_sells=sells),
            "rsi14": m.get("rsi14"),
            "rsi4h": m.get("rsi4h"),
            "ear": m.get("ear"),
            "surprise": m.get("surprise"),
            "modes": modes,
        }

    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "by_mode": by_mode,
        "focus": [r["ticker"] for r in focus_rows],
        "focus_detail": focus_rows,
        "ticker_meta": ticker_meta,
        "all": union,
    }


def load_snapshot(path: Path = SNAPSHOT) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def save_snapshot(data: dict, path: Path = SNAPSHOT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def diff_new(prev: dict, cur: dict) -> dict[str, list[str]]:
    """Return new tickers overall and per mode / focus."""
    prev_all = set(prev.get("all") or [])
    prev_focus = set(prev.get("focus") or [])
    prev_modes = prev.get("by_mode") or {}

    new_all = [t for t in cur.get("all") or [] if t not in prev_all]
    new_focus = [t for t in cur.get("focus") or [] if t not in prev_focus]
    new_by_mode: dict[str, list[str]] = {}
    for mode, tickers in (cur.get("by_mode") or {}).items():
        before = set(prev_modes.get(mode) or [])
        added = [t for t in tickers if t not in before]
        if added:
            new_by_mode[mode] = added
    return {"all": new_all, "focus": new_focus, "by_mode": new_by_mode}


def parse_modes_filter(raw: str | None) -> set[str] | None:
    """Return allowed mode names, or None for no filter."""
    if raw is None:
        return None
    parts = [p.strip().lower() for p in raw.replace(";", ",").split(",") if p.strip()]
    if not parts:
        return None
    unknown = [p for p in parts if p not in MODES and p != "focus"]
    if unknown:
        print(f"Warning: ignoring unknown modes {unknown}", file=sys.stderr)
    allowed = {p for p in parts if p in MODES}
    return allowed or None


def filter_new_by_modes(new: dict, cur: dict, modes: set[str] | None) -> dict:
    """Keep only new symbols that appear in one of ``modes`` (or focus tags)."""
    if not modes:
        return new
    detail = {r["ticker"]: r for r in (cur.get("focus_detail") or [])}

    def _touches(ticker: str) -> bool:
        if any(ticker in (new.get("by_mode") or {}).get(m, []) for m in modes):
            return True
        if any(ticker in ((cur.get("by_mode") or {}).get(m) or []) for m in modes):
            # newly in focus but already in a watched mode screen
            return ticker in (new.get("focus") or [])
        tags = (detail.get(ticker) or {}).get("tags") or ""
        tags_l = tags.lower()
        return any(m in tags_l for m in modes)

    focus = [t for t in (new.get("focus") or []) if _touches(t)]
    all_t = [t for t in (new.get("all") or []) if _touches(t)]
    by_mode = {
        m: ts
        for m, ts in (new.get("by_mode") or {}).items()
        if m in modes and ts
    }
    return {"all": all_t, "focus": focus, "by_mode": by_mode}


def _modes_for(ticker: str, cur: dict) -> list[str]:
    return [m for m, ts in (cur.get("by_mode") or {}).items() if ticker in (ts or [])]


def format_alert(
    new: dict,
    cur: dict,
    *,
    schwab_last: dict[str, float] | None = None,
    modes_label: str | None = None,
    institution_notes: dict[str, str] | None = None,
) -> str:
    lines: list[str] = []
    header = f"Screener new signals · {cur.get('as_of')}"
    if modes_label:
        header += f" · modes={modes_label}"
    lines.append(header)
    meta = cur.get("ticker_meta") or {}
    prices = schwab_last or {}
    notes = institution_notes or {}

    def _px(t: str) -> str:
        v = prices.get(t)
        return f" · Schwab ${v:.2f}" if v is not None else ""

    if new.get("focus"):
        lines.append("")
        lines.append("New in Today's focus:")
        detail = {r["ticker"]: r for r in (cur.get("focus_detail") or [])}
        for t in new["focus"]:
            d = detail.get(t) or {}
            why = d.get("why") or ""
            kind = d.get("kind") or ""
            signal = d.get("signal") or meta.get(t, {}).get("signal") or "BUY"
            rsi = _fmt_rsi(d.get("rsi14"), d.get("rsi4h"))
            modes = ", ".join(_modes_for(t, cur)) or (d.get("tags") or "")
            bit = f"  • {t} · {signal}"
            if kind:
                bit += f" [{kind}]"
            bit += _px(t)
            if rsi:
                bit += f" · {rsi}"
            if modes:
                bit += f" · {modes}"
            if notes.get(t):
                bit += f" · {notes[t]}"
            if why:
                bit += f" — {why}"
            lines.append(bit)
    if new.get("all"):
        lines.append("")
        lines.append("New in any mode:")
        for t in new["all"]:
            m = meta.get(t) or {}
            signal = m.get("signal") or "BUY"
            rsi = _fmt_rsi(m.get("rsi14"), m.get("rsi4h"))
            modes = ", ".join(_modes_for(t, cur)) or "?"
            bit = f"  • {t} · {signal}"
            bit += _px(t)
            if rsi:
                bit += f" · {rsi}"
            if m.get("ear") is not None:
                bit += f" · EAR {m['ear']:+.1f}%"
                if m.get("surprise") is not None:
                    bit += f" / surprise {m['surprise']:+.1f}%"
            bit += f" · {modes}"
            if notes.get(t):
                bit += f" · {notes[t]}"
            lines.append(bit)
    if new.get("by_mode"):
        lines.append("")
        lines.append("By mode:")
        for mode, tickers in new["by_mode"].items():
            bits = []
            for t in tickers:
                m = meta.get(t) or {}
                rsi = _fmt_rsi(m.get("rsi14"), m.get("rsi4h"))
                label = t
                if rsi:
                    label += f" ({rsi})"
                bits.append(label)
            lines.append(f"  {mode}: {', '.join(bits)}")
    if not new.get("all") and not new.get("focus"):
        lines.append("No new symbols vs last snapshot.")
    return "\n".join(lines)


def notify_desktop(title: str, body: str) -> None:
    if platform.system() != "Darwin":
        return
    short = body.replace("\n", " · ")[:180]
    script = (
        f'display notification {json.dumps(short)} with title {json.dumps(title)}'
    )
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=5)
    except Exception:  # noqa: BLE001
        pass


def notify_slack(text: str) -> bool:
    url = (
        os.getenv("SCREENER_SLACK_WEBHOOK")
        or os.getenv("SLACK_WEBHOOK_URL")
        or ""
    ).strip()
    if not url:
        return False
    try:
        resp = requests.post(url, json={"text": text}, timeout=15)
        return resp.status_code < 300
    except Exception as exc:  # noqa: BLE001
        print(f"Slack webhook failed: {exc}", file=sys.stderr)
        return False


def _schwab_ready() -> bool:
    """True when API creds + token file exist (quotes will work without browser)."""
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from stock_screener.data.schwab import schwab_configured, schwab_env

        if not schwab_configured():
            return False
        return Path(schwab_env()["token_path"]).exists()
    except Exception:  # noqa: BLE001
        return False


def _resolve_schwab_quotes_flag(
    *,
    cli_enable: bool | None,
    cli_disable: bool,
) -> bool:
    """Default on when Schwab token is present; env/CLI can override."""
    if cli_disable:
        return False
    if cli_enable:
        return True
    env = (os.getenv("SCREENER_SLACK_SCHWAB_QUOTES") or "").strip().lower()
    if env in {"0", "false", "no", "off"}:
        return False
    if env in {"1", "true", "yes", "on"}:
        return True
    return _schwab_ready()


def run(
    *,
    desktop: bool = True,
    slack: bool = True,
    dry_run: bool = False,
    modes: set[str] | None = None,
    schwab_quotes: bool = False,
) -> int:
    _load_dotenv()
    cur = collect_current()
    if not cur.get("all") and not cur.get("focus"):
        print("No screen CSVs / today_focus found under output/. Run the screener first.")
        return 1

    prev = load_snapshot()
    if prev is None:
        if not dry_run:
            save_snapshot(cur)
        print(
            f"Baseline saved ({len(cur['all'])} mode tickers, "
            f"{len(cur['focus'])} focus). No alert on first run."
        )
        print(f"Snapshot → {SNAPSHOT}")
        return 0

    new = filter_new_by_modes(diff_new(prev, cur), cur, modes)
    modes_label = ",".join(sorted(modes)) if modes else None

    schwab_last: dict[str, float] = {}
    if schwab_quotes:
        tickers = sorted(set(new.get("all") or []) | set(new.get("focus") or []))
        if tickers:
            try:
                sys.path.insert(0, str(ROOT / "src"))
                from stock_screener.data.schwab import enrich_last_prices

                schwab_last = enrich_last_prices(tickers)
                if schwab_last:
                    print(f"Schwab quotes: {len(schwab_last)}/{len(tickers)}")
            except Exception as exc:  # noqa: BLE001
                print(f"Schwab quotes skipped: {exc}", file=sys.stderr)

    institution_notes: dict[str, str] = {}
    note_tickers = sorted(set(new.get("all") or []) | set(new.get("focus") or []))
    if note_tickers:
        try:
            sys.path.insert(0, str(ROOT / "src"))
            from stock_screener.data.institutions_history import notes_for_tickers

            institution_notes = notes_for_tickers(note_tickers)
        except Exception as exc:  # noqa: BLE001
            print(f"13F notes skipped: {exc}", file=sys.stderr)

    msg = format_alert(
        new,
        cur,
        schwab_last=schwab_last or None,
        modes_label=modes_label,
        institution_notes=institution_notes or None,
    )
    print(msg)

    has_new = bool(new.get("all") or new.get("focus") or new.get("by_mode"))
    if has_new and not dry_run:
        if desktop:
            n = len(set(new.get("all") or []) | set(new.get("focus") or []))
            notify_desktop(
                "Screener · new signals",
                f"{n} new symbol(s): {', '.join((new.get('focus') or new.get('all') or [])[:8])}",
            )
        if slack:
            ok = notify_slack(f"```{msg}```")
            if ok:
                print("Slack webhook: sent")
            elif (
                os.getenv("SCREENER_SLACK_WEBHOOK") or os.getenv("SLACK_WEBHOOK_URL")
            ):
                print("Slack webhook: failed", file=sys.stderr)
        save_snapshot(cur)
        print(f"Updated snapshot → {SNAPSHOT}")
    elif not dry_run:
        save_snapshot(cur)
        print(f"Snapshot refreshed (no new symbols) → {SNAPSHOT}")

    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--no-desktop", action="store_true", help="Skip macOS notification")
    p.add_argument("--no-slack", action="store_true", help="Skip Slack webhook")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Diff only; do not write snapshot or send alerts",
    )
    p.add_argument(
        "--reset",
        action="store_true",
        help="Delete snapshot and re-seed baseline from current CSVs",
    )
    p.add_argument(
        "--modes",
        default=None,
        help=(
            "Comma-separated modes to alert on "
            f"(default: env SCREENER_SLACK_MODES or all). Choices: {','.join(MODES)}"
        ),
    )
    q = p.add_mutually_exclusive_group()
    q.add_argument(
        "--schwab-quotes",
        dest="schwab_quotes",
        action="store_true",
        help="Force-enable Schwab last prices on alert lines",
    )
    q.add_argument(
        "--no-schwab-quotes",
        dest="schwab_quotes",
        action="store_false",
        help="Disable Schwab quote enrichment (default: on when token configured)",
    )
    p.set_defaults(schwab_quotes=None)
    args = p.parse_args(argv)
    _load_dotenv()
    modes_raw = args.modes
    if modes_raw is None:
        modes_raw = os.getenv("SCREENER_SLACK_MODES") or ""
    modes = parse_modes_filter(modes_raw)
    schwab_q = _resolve_schwab_quotes_flag(
        cli_enable=True if args.schwab_quotes is True else None,
        cli_disable=args.schwab_quotes is False,
    )
    if args.reset and SNAPSHOT.exists():
        SNAPSHOT.unlink()
        print(f"Removed {SNAPSHOT}")
    return run(
        desktop=not args.no_desktop,
        slack=not args.no_slack,
        dry_run=args.dry_run,
        modes=modes,
        schwab_quotes=schwab_q,
    )


if __name__ == "__main__":
    raise SystemExit(main())
