"""Paper-book rules: trigger-day entry, hold-10 and RSI exits. No market data."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import paper_book as pb  # noqa: E402


def _cal(n: int, start: str = "2026-11-02") -> list[str]:
    import pandas as pd

    days = pd.bdate_range(start, periods=n)
    return [d.strftime("%Y-%m-%d") for d in days]


def _go(state, session, ranked, cal, bars, rsi=None, allow=True):
    return pb.advance(
        state,
        session=session,
        ranked=ranked,
        allow_entries=allow,
        calendar=cal,
        bars=bars,
        rsi_by_ticker=rsi or {},
    )


class ExitRules(unittest.TestCase):
    def test_hold10_and_time_stop(self):
        self.assertIsNone(pb.exit_reason("hold10", 9, None))
        self.assertEqual(pb.exit_reason("hold10", 10, None), "hold10")
        self.assertEqual(pb.exit_reason("hold10", 20, None), "time20")

    def test_rsi_needs_a_later_close_above_50(self):
        self.assertIsNone(pb.exit_reason("rsi50", 0, 60))
        self.assertIsNone(pb.exit_reason("rsi50", 1, 50))
        self.assertEqual(pb.exit_reason("rsi50", 1, 50.1), "rsi50")

    def test_round_trip_cost(self):
        # 10 bps each way on a flat trade is a loss.
        self.assertLess(pb.net_return(100.0, 100.0), 0)


class Signals(unittest.TestCase):
    def test_only_the_trigger_day_enters(self):
        rows = [
            {"ticker": "NEW", "composite": 10, "trigger_date": "2026-11-20"},
            {"ticker": "OLD", "composite": 90, "trigger_date": "2026-11-19"},
            {"ticker": "mid", "composite": 50, "trigger_date": "2026-11-20 00:00:00"},
        ]
        got = pb.ranked_signals(rows, "2026-11-20")
        self.assertEqual([r["ticker"] for r in got], ["MID", "NEW"])


class Book(unittest.TestCase):
    def test_new_name_enters_both_main_books_at_the_close(self):
        cal = _cal(3)
        state = pb.new_state()
        bars = {"CCC": {cal[1]: 20.0}}
        _go(state, cal[1], [{"ticker": "CCC", "score": 5}], cal, bars)
        for name in ("hold10", "rsi50"):
            opens = state["books"][name]["open"]
            self.assertEqual([p["ticker"] for p in opens], ["CCC"])
            self.assertEqual(opens[0]["entry_px"], 20.0)
            self.assertEqual(opens[0]["entry_date"], cal[1])
        # Three sessions is short of the 20-session window, so the gated books skip.
        for name in ("hold10_low20", "rsi50_low20"):
            self.assertEqual(state["books"][name]["open"], [])

    def test_low20_books_need_the_trigger_close_to_be_a_20_session_low(self):
        cal = _cal(21)
        # Flat history, then a new low on the last session.
        lows = {d: 100.0 for d in cal}
        lows[cal[-1]] = 90.0
        state = pb.new_state()
        bars = {"LOW": lows}
        _go(state, cal[-1], [{"ticker": "LOW", "score": 1}], cal, bars)
        for name in pb.BOOKS:
            self.assertEqual(
                [p["ticker"] for p in state["books"][name]["open"]], ["LOW"], name
            )

    def test_low20_books_skip_a_name_that_is_not_at_a_20_session_low(self):
        cal = _cal(21)
        # The prior session was lower, so today's close is not the 20-session low.
        px = {d: 100.0 for d in cal}
        px[cal[-2]] = 80.0
        px[cal[-1]] = 90.0
        state = pb.new_state()
        bars = {"NOT": px}
        _go(state, cal[-1], [{"ticker": "NOT", "score": 1}], cal, bars)
        self.assertEqual([p["ticker"] for p in state["books"]["hold10"]["open"]], ["NOT"])
        self.assertEqual([p["ticker"] for p in state["books"]["rsi50"]["open"]], ["NOT"])
        self.assertEqual(state["books"]["hold10_low20"]["open"], [])
        self.assertEqual(state["books"]["rsi50_low20"]["open"], [])

    def test_low20_books_use_the_same_exits_as_their_main_book(self):
        cal = _cal(31)
        # Steadily falling closes: the trigger session (index 19) is the 20-session low.
        bars = {"DRP": {d: 150.0 - i * 0.5 for i, d in enumerate(cal)}}
        rsi = {"DRP": {d: 40.0 for d in cal}}  # never above 50, so rsi50 never fires
        state = pb.new_state()
        _go(state, cal[19], [{"ticker": "DRP", "score": 1}], cal, bars, rsi)
        _go(state, cal[29], [], cal, bars, rsi, allow=False)
        self.assertEqual(
            [t["reason"] for t in state["books"]["hold10_low20"]["closed"]], ["hold10"]
        )
        self.assertEqual(state["books"]["rsi50_low20"]["open"][0]["ticker"], "DRP")
        self.assertEqual(state["books"]["rsi50_low20"]["closed"], [])

    def test_hold10_exits_on_the_tenth_session(self):
        cal = _cal(12)
        state = pb.new_state()
        bars = {"AAA": {d: 100.0 + i for i, d in enumerate(cal)}}
        rsi = {"AAA": {d: 40.0 for d in cal}}
        _go(state, cal[0], [{"ticker": "AAA", "score": 1}], cal, bars, rsi)
        _go(state, cal[10], [], cal, bars, rsi, allow=False)
        closed = state["books"]["hold10"]["closed"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["exit_date"], cal[10])
        self.assertEqual(closed[0]["held"], 10)
        self.assertEqual(closed[0]["reason"], "hold10")
        self.assertEqual(state["books"]["rsi50"]["open"][0]["ticker"], "AAA")

    def test_rsi_exits_and_can_reenter_same_close(self):
        cal = _cal(4)
        state = pb.new_state()
        bars = {"AAA": {d: 50.0 for d in cal}}
        rsi = {"AAA": {cal[0]: 30.0, cal[1]: 55.0, cal[2]: 55.0}}
        _go(state, cal[0], [{"ticker": "AAA", "score": 1}], cal, bars, rsi)
        _go(state, cal[1], [{"ticker": "AAA", "score": 1}], cal, bars, rsi)
        closed = state["books"]["rsi50"]["closed"]
        self.assertEqual(closed[0]["reason"], "rsi50")
        self.assertEqual(closed[0]["exit_date"], cal[1])
        self.assertEqual(state["books"]["rsi50"]["open"][0]["entry_date"], cal[1])
        # Still inside its 10-session hold, so the hold book keeps the first lot.
        self.assertEqual(state["books"]["hold10"]["open"][0]["entry_date"], cal[0])
        self.assertEqual(len(state["books"]["hold10"]["open"]), 1)

    def test_same_session_does_not_open_twice(self):
        cal = _cal(2)
        state = pb.new_state()
        bars = {"AAA": {cal[0]: 10.0}}
        ranked = [{"ticker": "AAA", "score": 1}]
        _go(state, cal[0], ranked, cal, bars)
        _go(state, cal[0], ranked, cal, bars)
        self.assertEqual(len(state["books"]["hold10"]["open"]), 1)

    def test_eleventh_name_is_skipped(self):
        cal = _cal(2)
        state = pb.new_state()
        names = [f"T{i:02d}" for i in range(11)]
        bars = {n: {cal[0]: 10.0} for n in names}
        ranked = [{"ticker": n, "score": 100 - i} for i, n in enumerate(names)]
        _go(state, cal[0], ranked, cal, bars)
        held = [p["ticker"] for p in state["books"]["hold10"]["open"]]
        self.assertEqual(held, names[:10])
        self.assertNotIn("T10", held)

    def test_old_state_files_drop_the_seed_fields(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "book.json"
            path.write_text(
                json.dumps({"seeded": True, "ineligible": ["X"], "books": {}}),
                encoding="utf-8",
            )
            state = pb.load_state(path)
        self.assertNotIn("ineligible", state)
        self.assertNotIn("seeded", state)
        self.assertEqual(set(state["books"]), set(pb.BOOKS))


class PriceSource(unittest.TestCase):
    def setUp(self):
        self._saved = (pb.fetch_frames, pb.fetch_yahoo_frames)

    def tearDown(self):
        pb.fetch_frames, pb.fetch_yahoo_frames = self._saved

    def test_schwab_is_used_when_it_works(self):
        pb.fetch_frames = lambda tickers: {"SPY": "schwab"}
        pb.fetch_yahoo_frames = lambda tickers: self.fail("should not call Yahoo")
        frames, note = pb.load_prices(["SPY"])
        self.assertEqual(frames, {"SPY": "schwab"})
        self.assertIsNone(note)

    def test_yahoo_fallback_says_schwab_failed(self):
        def broken(tickers):
            raise ConnectionError("token expired")

        pb.fetch_frames = broken
        pb.fetch_yahoo_frames = lambda tickers: {"SPY": "yahoo"}
        frames, note = pb.load_prices(["SPY"])
        self.assertEqual(frames, {"SPY": "yahoo"})
        self.assertIn("Schwab daily history failed (ConnectionError)", note)
        self.assertIn("used Yahoo daily closes", note)
        self.assertNotIn("token expired", note)  # no raw error text in the note

    def test_both_failing_raises(self):
        def broken(tickers):
            raise ConnectionError("x")

        pb.fetch_frames = broken
        pb.fetch_yahoo_frames = broken
        with self.assertRaises(RuntimeError):
            pb.load_prices(["SPY"])


if __name__ == "__main__":
    unittest.main()
