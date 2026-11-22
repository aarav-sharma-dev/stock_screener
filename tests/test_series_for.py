"""A missing ticker in a multi-symbol panel is an empty series, not every close."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stock_screener.data.prices import series_for  # noqa: E402


class SeriesFor(unittest.TestCase):
    def test_missing_ticker_is_empty(self):
        idx = pd.bdate_range("2026-01-02", periods=3)
        columns = pd.MultiIndex.from_product([["Close"], ["AAA", "BBB"]])
        prices = pd.DataFrame([[1.0, 2.0], [1.1, 2.1], [1.2, 2.2]], index=idx, columns=columns)
        missing = series_for(prices, "Close", "PSTG")
        self.assertIsInstance(missing, pd.Series)
        self.assertTrue(missing.empty)
        self.assertEqual(float(series_for(prices, "Close", "AAA").iloc[-1]), 1.2)


if __name__ == "__main__":
    unittest.main()
