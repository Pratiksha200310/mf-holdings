"""Parser tests on small committed samples (tests/samples/, built by tests/make_samples.py).

No network: every test reads a local file or an inline string.  Run with
    python -m unittest discover -s tests        (or: pytest)
"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from holdings.amcs import REGISTRY  # noqa: E402
from holdings.amcs import bandhan, jioblackrock, mahindra, sundaram  # noqa: E402
from holdings.excel import lakhs_factor, num  # noqa: E402
from holdings.fetch import Fetcher  # noqa: E402
from holdings.identifiers import isin_is_valid, strip_expiry  # noqa: E402
from holdings.model import Holding, LayoutError, Scheme  # noqa: E402
from holdings.pipeline import _match_hedged_equity  # noqa: E402
from holdings.statement import parse_date  # noqa: E402

SAMPLES = ROOT / "tests" / "samples"
JUL, AUG, SEP = date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)


def parse(key: str, sample: str, month: date) -> dict[str, Scheme]:
    amc = REGISTRY[key](Fetcher(Path(tempfile.mkdtemp()), offline=True))  # parse() never fetches
    amc._expected = None if key == "jioblackrock" else 0  # set by fetch() in normal runs
    return {s.name: s for s in amc.parse(SAMPLES / sample, month)}


def nav_total(s: Scheme) -> float:
    return sum(h.pct_of_nav or 0 for h in s.holdings
               if not (s.nav_excludes_derivatives and h.asset_type == "derivative"))


def types(s: Scheme) -> Counter:
    return Counter(h.asset_type for h in s.holdings)


class Helpers(unittest.TestCase):
    def test_num(self):
        self.assertEqual(num("1,234.50"), 1234.5)
        self.assertEqual(num("(12.5)"), -12.5)
        self.assertIsNone(num("NIL"))
        self.assertIsNone(num("$"))  # Bandhan's "less than 0.01%" marker

    def test_units(self):
        self.assertEqual(lakhs_factor("Market Value (Rs. in Lakhs)"), 1)
        self.assertEqual(lakhs_factor("Market value (Rs. in Crores)"), 100)
        with self.assertRaises(ValueError):
            lakhs_factor("Market value")

    def test_isin_check_digit(self):
        self.assertTrue(isin_is_valid("INE002A01018"))
        self.assertFalse(isin_is_valid("INE002A01019"))
        self.assertFalse(isin_is_valid("DBXXXGOLD100"))

    def test_dates(self):
        self.assertEqual(parse_date("Monthly Portfolio Statement as on September 30, 2026"), date(2026, 9, 30))
        self.assertEqual(parse_date("PORTFOLIO AS ON 31-Aug-2026"), date(2026, 8, 31))

    def test_strip_expiry(self):
        self.assertEqual(strip_expiry("Apollo Hospitals Enterprise Limited 9200 Call October 2026 Option"),
                         "Apollo Hospitals Enterprise Limited")
        self.assertEqual(strip_expiry("PGC LTD FUT 27-OCT-26"), "PGC LTD")
        self.assertEqual(strip_expiry("Nifty 50"), "Nifty 50")

    def test_hedged_future_split_across_expiries(self):
        eq = Holding("Power Grid Corporation of India Ltd", isin="INE752E01010", industry_or_rating="Power",
                     asset_type="equity", quantity=30400)
        f1 = Holding("PGC LTD FUT 27-OCT-26", industry_or_rating="Power", asset_type="derivative", quantity=-20000)
        f2 = Holding("PGC LTD FUT 23-NOV-26", industry_or_rating="Power", asset_type="derivative", quantity=-10400)
        other = Holding("NTPC Ltd", isin="INE733E01010", industry_or_rating="Power", asset_type="equity", quantity=20000)
        s = Scheme("x", date(2026, 9, 30), [eq, other, f1, f2])

        class NoTickers:
            def ticker_for(self, isin):
                return ""
        _match_hedged_equity(s, NoTickers())
        self.assertEqual((f1.isin, f2.isin), ("INE752E01010", "INE752E01010"))


class Quant(unittest.TestCase):
    def test_duplicated_rows_and_derivatives_outside_total(self):
        got = parse("quant", "quant_2026-09.xlsx", SEP)
        es = got["quant Equity Savings Fund"]
        # The file prints the TREPS and NCA blocks twice; one copy is dropped.
        self.assertEqual(sum("duplicated" in w for w in es.warnings), 2)
        self.assertTrue(es.nav_excludes_derivatives)
        self.assertEqual(types(es)["derivative"], 13)
        self.assertAlmostEqual(nav_total(es), 100, delta=0.05)
        self.assertAlmostEqual(nav_total(got["quant Silver ETF"]), 100, delta=0.05)


class Sundaram(unittest.TestCase):
    def test_workbook(self):
        got = parse("sundaram", "sundaram_2026-09.xlsx", SEP)
        self.assertEqual(len(got), 2)
        for s in got.values():
            self.assertAlmostEqual(nav_total(s), 100, delta=0.05)  # % printed as fractions, scaled
        self.assertEqual(types(got["Sundaram Overnight Fund"])["cash"], 3)

    def test_listing(self):
        js = ("'<div class=\"accordion-item\"><h2 class=\"accordion-header\">FY 2026-27</h2>"
              "<button class=\"nav-link\" data-bs-target=\"#m9\">Sep</button><div id=\"m9\">"
              "<a href=\"/x/eq.xlsx\">Monthly Portfolio Disclosure Equity &amp; Fund of Funds - Sep 2026</a>"
              "</div></div>'")
        self.assertEqual(sundaram.parse_listing(js),
                         [("FY 2026-27", "Sep", "Monthly Portfolio Disclosure Equity & Fund of Funds - Sep 2026", "/x/eq.xlsx")])


class HDFC(unittest.TestCase):
    def test_futures_from_derivative_sheet(self):
        (s,) = parse("hdfc", "hdfc_2026-09.xlsx", SEP).values()
        self.assertEqual(s.name, "HDFC Aggressive Hybrid Fund")
        self.assertEqual(types(s)["derivative"], 6)
        self.assertAlmostEqual(nav_total(s), 100, delta=0.05)
        self.assertTrue(any("Options" in w for w in s.warnings))  # reported, not silently dropped


class Mahindra(unittest.TestCase):
    def test_net_receivables_subtotal_dropped(self):
        got = parse("mahindra", "mahindra_2026-09.xlsx", SEP)
        for s in got.values():
            self.assertEqual(sum("dropped 'Net Receivables" in w for w in s.warnings), 1)
            self.assertAlmostEqual(nav_total(s), 100, delta=0.05)
        self.assertEqual(types(got["Mahindra Manulife Arbitrage Fund"])["derivative"], 35)

    def test_single_net_receivables_kept(self):
        (s,) = parse("mahindra", "mahindra_2026-07.xlsx", JUL).values()
        self.assertFalse(any("dropped" in w for w in s.warnings))
        self.assertAlmostEqual(nav_total(s), 100, delta=0.05)

    def test_decrypt_and_tree(self):
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        key, iv = b"k" * 32, b"i" * 16
        tree = [{"categoryName": "MANDATORY DISCLOSURES", "subcategories": [
            {"categoryName": "Portfolio Disclosure", "subcategories": [
                {"categoryName": "Monthly Portfolio Disclosure", "subcategories": [
                    {"categoryName": "2026", "subcategories": [], "files": [{"title": "t", "fileUrl": "u"}]}]}]}]}]
        p = padding.PKCS7(128).padder()
        raw = p.update(json.dumps(tree).encode()) + p.finalize()
        e = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        payload = base64.b64encode(e.update(raw) + e.finalize()).decode()
        self.assertEqual(mahindra.find_monthly_files(json.loads(mahindra.decrypt(payload, key, iv)), 2026),
                         [{"title": "t", "fileUrl": "u"}])
        self.assertEqual(mahindra.find_monthly_files(tree, 2031), [])


class JioBlackRock(unittest.TestCase):
    def test_reverse_repo_lines_are_holdings(self):
        (s,) = parse("jioblackrock", "jioblackrock_overnight_2026-09.xlsx", SEP).values()
        self.assertEqual(types(s)["cash"], 4)  # TREPS, 2 x Reverse Repo, Net Receivables
        self.assertAlmostEqual(nav_total(s), 100, delta=0.05)

    def test_futures_after_grand_total_and_valued_heading(self):
        (s,) = parse("jioblackrock", "jioblackrock_arbitrage_2026-09.xlsx", SEP).values()
        self.assertTrue(any("heading 'Money Market Instruments' carries values" in w for w in s.warnings))
        self.assertTrue(s.nav_excludes_derivatives)
        self.assertEqual(types(s)["derivative"], 151)
        self.assertAlmostEqual(nav_total(s), 100, delta=0.05)

    def test_flight_reply(self):
        raw = '0:{"a":"$@1"}\n1:{"data":[{"title":"x"}]}\n'
        self.assertEqual(jioblackrock.parse_flight(raw), {"data": [{"title": "x"}]})
        with self.assertRaises(LayoutError):
            jioblackrock.parse_flight('0:{"a":"$@1"}\n')


class Bandhan(unittest.TestCase):
    def test_nca_subtotal_and_dollar_lines(self):
        (s,) = parse("bandhan", "bandhan_bal_adv_2026-09.xlsx", SEP).values()
        self.assertTrue(any("dropped 'Net Current Assets'" in w for w in s.warnings))
        self.assertTrue(all(h.pct_of_nav is not None for h in s.holdings))
        self.assertTrue(s.nav_excludes_derivatives)
        self.assertAlmostEqual(nav_total(s), 100, delta=0.05)

    def test_physical_gold_has_no_fake_isin(self):
        (s,) = parse("bandhan", "bandhan_gold_etf_2026-09.xlsx", SEP).values()
        bars = [h for h in s.holdings if "BAR" in h.instrument_name.upper()]
        self.assertEqual(len(bars), 2)
        self.assertTrue(all(h.isin == "" for h in bars))

    def test_equal_swaps_are_not_duplicates(self):
        (s,) = parse("bandhan", "bandhan_floating_2026-08.xlsx", AUG).values()
        self.assertFalse(any("duplicated" in w for w in s.warnings))
        self.assertEqual(types(s)["derivative"], 16)

    def test_listing(self):
        post = {"financial_year": "2026", "title": "x", "acf_fields": {
            "month": "September", "document_name": "Bandhan Gold ETF 30 September 2026",
            "disclosure_files": [{"url": "https://h/f.xlsx"}]}}
        docs = bandhan.parse_listing(json.dumps({"data": [post], "total_posts": 1}), SEP)
        self.assertEqual([(d.url, d.label) for d in docs], [("https://h/f.xlsx", "Bandhan Gold ETF 30 September 2026")])
        with self.assertRaises(LayoutError):  # a page that does not hold the whole month
            bandhan.parse_listing(json.dumps({"data": [post], "total_posts": 2}), SEP)
        with self.assertRaises(LayoutError):  # the site answered for another month
            bandhan.parse_listing(json.dumps({"data": [post]}), AUG)
        # a month that is not out yet: no posts and a blank month, which discover() reports
        self.assertEqual(bandhan.parse_listing(json.dumps({"data": [], "total_posts": 0, "selected_month": ""}), AUG), [])


if __name__ == "__main__":
    unittest.main()
