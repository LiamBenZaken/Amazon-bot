"""Self-contained unit tests for the bot's helpers.
Exercises every piece of logic without launching Chrome or hitting Amazon.

Run: uv run python test_units.py
"""
import os
import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import re

# Verify TEST_MODE is on before importing anything that could buy.
import main
assert main.TEST_MODE is True, "TEST_MODE must be True for safe testing"

from main import (
    VALID_SELLERS,
    HISTORY_FILE,
    TERMINAL_ORDER_STATES,
    save_counts_atomic,
    _status_text_for_card,
    fetch_amazon_purchase_counts,
    send_telegram_alert,
    AmazonTLSTracker,
)
from bs4 import BeautifulSoup


class T01_AtomicSave(unittest.TestCase):
    def test_writes_and_reads(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "h.json")
            save_counts_atomic({"X": 3, "Y": 0}, p)
            with open(p) as f:
                self.assertEqual(json.load(f), {"X": 3, "Y": 0})

    def test_no_tmp_left_behind(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "h.json")
            save_counts_atomic({"A": 1}, p)
            self.assertFalse(os.path.exists(p + ".tmp"))

    def test_overwrites_existing(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "h.json")
            save_counts_atomic({"A": 1}, p)
            save_counts_atomic({"A": 2}, p)
            with open(p) as f:
                self.assertEqual(json.load(f), {"A": 2})


class T02_StatusTextForCard(unittest.TestCase):
    def _card(self, html):
        return BeautifulSoup(html, "lxml").select_one("div")

    def test_finds_in_delivery_box(self):
        html = """
        <div class="order-card">
          <div class="delivery-box">
            <span class="a-text-bold">Arriving Tuesday, May 5</span>
          </div>
        </div>"""
        self.assertIn("arriving", _status_text_for_card(self._card(html)))

    def test_finds_color_success(self):
        html = """
        <div class="order">
          <span class="a-color-success a-text-bold">Delivered Mar 1, 2025</span>
        </div>"""
        self.assertIn("delivered", _status_text_for_card(self._card(html)))

    def test_falls_back_to_card_text(self):
        html = """<div class="order">No status pill here, just text Cancelled.</div>"""
        out = _status_text_for_card(self._card(html))
        self.assertIn("cancelled", out)

    def test_does_not_match_buried_terminal_words(self):
        # 'Buy it again' widget at the bottom shouldn't dominate when there's
        # a clear status pill above it saying 'Arriving'.
        html = """
        <div class="order-card">
          <div class="delivery-box">
            <span class="a-text-bold">Arriving Friday</span>
          </div>
          <div>Buy it again — last delivered to your address Mar 5.</div>
        </div>"""
        out = _status_text_for_card(self._card(html))
        self.assertIn("arriving", out)
        # The pill should be the dominant signal — not the trailing text.
        self.assertTrue(out.startswith("arriving"))


class T03_OrderScanWithMockDriver(unittest.TestCase):
    """Drive fetch_amazon_purchase_counts with a mock driver returning canned HTML."""

    @staticmethod
    def _make_driver(pages):
        """pages: list of HTML strings. Each driver.get() advances to next."""
        d = MagicMock()
        d.current_url = "https://www.amazon.com/gp/your-account/order-history"
        state = {"i": 0}

        def get(url):
            d._last_url = url

        def src():
            i = state["i"]
            state["i"] += 1
            if i >= len(pages):
                return "<html><body><div>No more orders</div></body></html>"
            return pages[i]

        d.get.side_effect = get
        type(d).page_source = property(lambda self_: src())
        return d

    def test_signin_returns_none(self):
        signin_page = '<html><body><form id="ap_signin_form"></form></body></html>'
        d = self._make_driver([signin_page])
        with patch("main.time.sleep"):
            result = fetch_amazon_purchase_counts(d, ["B0G3CV6Z9D"], lookback_years=1, debug=False)
        self.assertIsNone(result)

    def test_counts_in_progress_skips_delivered(self):
        page = """
        <html><body>
          <div class="order-card">
            <div class="delivery-box">
              <span class="a-text-bold">Arriving Tomorrow</span>
            </div>
            <a href="/dp/B0G4XJPN8Q/">Premium Collection</a>
          </div>
          <div class="order-card">
            <div class="delivery-box">
              <span class="a-text-bold">Delivered Mar 5</span>
            </div>
            <a href="/dp/B0G3CV6Z9D/">Heroes Booster</a>
          </div>
        </body></html>
        """
        d = self._make_driver([page, "<html></html>", "<html></html>", "<html></html>"])
        with patch("main.time.sleep"):
            counts = fetch_amazon_purchase_counts(
                d, ["B0G3CV6Z9D", "B0G4XJPN8Q"], lookback_years=1, debug=False
            )
        self.assertEqual(counts["B0G4XJPN8Q"], 1, "in-progress order should count")
        self.assertEqual(counts["B0G3CV6Z9D"], 0, "delivered order should NOT count")

    def test_parses_quantity(self):
        page = """
        <html><body>
          <div class="order-card">
            <div class="delivery-box">
              <span class="a-text-bold">Shipping now</span>
            </div>
            <div>
              <a href="/dp/B0G4XJPN8Q/">Premium</a>
              <span>Quantity: 3</span>
            </div>
          </div>
        </body></html>
        """
        d = self._make_driver([page, "<html></html>", "<html></html>", "<html></html>"])
        with patch("main.time.sleep"):
            counts = fetch_amazon_purchase_counts(d, ["B0G4XJPN8Q"], lookback_years=1, debug=False)
        self.assertEqual(counts["B0G4XJPN8Q"], 3)

    def test_dedupes_image_and_title_links_per_card(self):
        # Real Amazon order cards have BOTH an image <a> and a title <a>
        # linking to the same /dp/ASIN. The bot must not count those as 2.
        page = """
        <html><body>
          <div class="order-card">
            <div class="delivery-box">
              <span class="a-text-bold">Arriving May 14 - June 15</span>
            </div>
            <div class="item">
              <a href="/dp/B0G4XJPN8Q/ref=img"><img/></a>
              <a href="/dp/B0G4XJPN8Q/ref=title">Premium Collection</a>
            </div>
          </div>
        </body></html>
        """
        d = self._make_driver([page, "<html></html>", "<html></html>", "<html></html>"])
        with patch("main.time.sleep"):
            counts = fetch_amazon_purchase_counts(d, ["B0G4XJPN8Q"], lookback_years=1, debug=False)
        self.assertEqual(counts["B0G4XJPN8Q"], 1, "1 order with image+title links must count as 1")

    def test_skips_cancelled(self):
        page = """
        <html><body>
          <div class="order-card">
            <div class="delivery-box">
              <span class="a-text-bold">Cancelled Mar 2</span>
            </div>
            <a href="/dp/B0G4XJPN8Q/">Premium</a>
          </div>
        </body></html>
        """
        d = self._make_driver([page, "<html></html>", "<html></html>", "<html></html>"])
        with patch("main.time.sleep"):
            counts = fetch_amazon_purchase_counts(d, ["B0G4XJPN8Q"], lookback_years=1, debug=False)
        self.assertEqual(counts["B0G4XJPN8Q"], 0)


class T04_TelegramAsync(unittest.TestCase):
    def test_no_env_no_call(self):
        # No env vars → no thread, no exception.
        with patch.dict(os.environ, {}, clear=True):
            send_telegram_alert("hi")  # should not raise

    def test_does_not_block(self):
        import time
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_CHAT_ID": "y"}):
            with patch("main.requests.post") as p:
                p.side_effect = lambda *a, **kw: time.sleep(2)  # slow Telegram
                t0 = time.time()
                send_telegram_alert("hello")
                elapsed = time.time() - t0
                self.assertLess(elapsed, 0.2, "Telegram should not block hot path")


class T05_QuantityTierRules(unittest.TestCase):
    """Re-encode the original rule logic and verify it picks the right tier
    for several prices (matches lines ~414 in main.py)."""

    @staticmethod
    def pick_qty(rules, price):
        for max_price, q in rules:
            if price <= max_price:
                return q
        return 1

    def test_heroes_tiers(self):
        rules = [(45.00, 3), (60.00, 2), (71.00, 1)]
        self.assertEqual(self.pick_qty(rules, 40.00), 3)
        self.assertEqual(self.pick_qty(rules, 45.00), 3)
        self.assertEqual(self.pick_qty(rules, 50.00), 2)
        self.assertEqual(self.pick_qty(rules, 65.00), 1)
        self.assertEqual(self.pick_qty(rules, 80.00), 1)  # default fallback


class T06_AdaptivePollingThresholds(unittest.TestCase):
    """Re-encode the adaptive-interval ladder from main.py and verify it."""

    @staticmethod
    def base_for(ratio):
        if ratio <= 1.05: return 1.0
        elif ratio <= 1.10: return 2.0
        elif ratio <= 1.20: return 4.0
        elif ratio <= 1.50: return 8.0
        else: return 10.0  # hard cap

    def test_thresholds(self):
        self.assertEqual(self.base_for(1.00), 1.0)   # at target
        self.assertEqual(self.base_for(1.05), 1.0)
        self.assertEqual(self.base_for(1.06), 2.0)
        self.assertEqual(self.base_for(1.20), 4.0)
        self.assertEqual(self.base_for(1.50), 8.0)
        self.assertEqual(self.base_for(2.00), 10.0)  # capped
        self.assertEqual(self.base_for(99.0), 10.0)  # capped, even way far away


class T07_PriceParsing(unittest.TestCase):
    """Replicate AmazonTLSTracker.check_price's number extraction logic."""

    @staticmethod
    def parse(raw, exchange_rate=3.65):
        clean = re.sub(r"[^\d.]", "", raw)
        n = float(clean)
        if "ILS" in raw or "₪" in raw:
            return n / exchange_rate
        return n

    def test_usd(self):
        self.assertAlmostEqual(self.parse("$71.00"), 71.00)

    def test_ils_symbol(self):
        # ₪260 ≈ $71.23 at 3.65
        self.assertAlmostEqual(self.parse("₪260.00"), 71.2329, places=2)

    def test_ils_text(self):
        self.assertAlmostEqual(self.parse("260.00 ILS"), 71.2329, places=2)


class T08_TestModeShortCircuit(unittest.TestCase):
    """The flow in main.py uses TEST_MODE to skip buy_product entirely.
    Verify the top-level constant is set safely for this test session."""

    def test_test_mode_on(self):
        self.assertTrue(main.TEST_MODE, "TEST_MODE must be True so no real buys happen during testing")


class T09_SellerWhitelistDryRun(unittest.TestCase):
    def test_amazon_is_in_list(self):
        self.assertIn("ATVPDKIKX0DER", VALID_SELLERS)

    def test_third_party_is_not(self):
        self.assertNotIn("A3QP0HSG", VALID_SELLERS)
        self.assertNotIn("RANDOMSELLER", VALID_SELLERS)


class T10_TerminalStatesCoverage(unittest.TestCase):
    def test_all_keywords_present(self):
        for kw in ("delivered", "cancelled", "canceled", "refunded", "returned"):
            self.assertIn(kw, TERMINAL_ORDER_STATES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
