"""Canvas markdown for the paper book. No Slack calls."""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import paper_book as pb  # noqa: E402
import paper_book_canvas as pc  # noqa: E402


def _state():
    return {
        "max_positions": 10,
        "books": {
            "hold10": {
                "open": [{"ticker": "NVDA", "entry_date": "2026-11-16", "entry_px": 156.14}],
                "closed": [
                    {
                        "ticker": "AMD",
                        "entry_date": "2026-11-02",
                        "exit_date": "2026-11-16",
                        "net_ret": 0.078,
                    }
                ],
            },
            "rsi50": {"open": [], "closed": []},
        },
    }


class Markdown(unittest.TestCase):
    def test_summary_and_lots_render(self):
        md = pc.build_markdown(_state(), {("hold10", "NVDA"): 125.53}, "2026-11-20")
        self.assertIn("# Summary", md)
        self.assertIn("| Hold 10 sessions | 1 | 1 / 1 | +7.80% | 1 |", md)
        self.assertIn("| NVDA | 2026-11-16 | 156.14 | 125.53 | -19.76% |", md)
        self.assertIn("| AMD | 2026-11-02 | 2026-11-16 | +7.80% |", md)
        self.assertIn("None.", md)  # empty RSI book
        self.assertIn("Not live fills", md)

    def test_all_four_books_appear_in_the_canvas(self):
        md = pc.build_markdown(_state(), {}, "2026-11-20")
        self.assertIn("| Hold 10 sessions |", md)
        self.assertIn("| RSI(14) above 50 |", md)
        self.assertIn("| Hold 10, 20-day low only |", md)
        self.assertIn("| RSI(14) above 50, 20-day low only |", md)
        self.assertIn("# Hold 10 + 20-day low: open lots", md)
        self.assertIn("# RSI 50 + 20-day low: closed", md)

    def test_marks_come_from_the_ledger_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "book.csv"
            with path.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["book", "status", "ticker", "last_px"])
                w.writeheader()
                w.writerow({"book": "hold10", "status": "open", "ticker": "NVDA", "last_px": "125.53"})
                w.writerow({"book": "hold10", "status": "closed", "ticker": "AMD", "last_px": ""})
            got = pc.last_prices(path)
        self.assertEqual(got, {("hold10", "NVDA"): 125.53})


class Token(unittest.TestCase):
    def test_reads_export_style_env_file(self):
        import os

        old = os.environ.pop("SLACK_BOT_TOKEN", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                env = Path(tmp) / "env"
                env.write_text(
                    "# comment\nexport FOO=1\nexport SLACK_BOT_TOKEN=xoxb-test-value\n",
                    encoding="utf-8",
                )
                orig = pc.ENV_PATH
                pc.ENV_PATH = env
                try:
                    self.assertEqual(pc._token(), "xoxb-test-value")
                finally:
                    pc.ENV_PATH = orig
        finally:
            if old is not None:
                os.environ["SLACK_BOT_TOKEN"] = old


class Publish(unittest.TestCase):
    def test_no_token_is_reported_not_raised(self):
        import os

        old = os.environ.pop("SLACK_BOT_TOKEN", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                orig = pc.ENV_PATH
                pc.ENV_PATH = Path(tmp) / "missing.env"
                try:
                    ok, msg = pc.publish("# x", path=Path(tmp) / "c.json")
                finally:
                    pc.ENV_PATH = orig
        finally:
            if old is not None:
                os.environ["SLACK_BOT_TOKEN"] = old
        self.assertFalse(ok)
        self.assertIn("no Slack bot token", msg)


if __name__ == "__main__":
    unittest.main()
