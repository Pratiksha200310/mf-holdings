"""HDFC Mutual Fund (hdfcfund.com).

Discovery: Statutory Disclosure -> Portfolio -> Monthly (/statutory-disclosure/portfolio/monthly-portfolio)
is a Next.js page. In the browser it calls the CMS API with a multipart form:

  POST https://cms.hdfcfund.com/en/hdfc/api/v2/disclosures/monthfortportfolio
       year=<financial year start, e.g. 2026 for Apr 2026 - Mar 2027>, type=monthly, month=<1-12>
  -> {"data": {"files": [{"title": "Monthly HDFC Small Cap Fund - 30 September 2026.xlsx",
                          "file": {"url": "https://files.hdfcfund.com/..."}}, ...]}}

www.hdfcfund.com sits behind Akamai, which returns 403 to a client that does not send browser-like
Accept headers; with them, plain `requests` works (no headless browser, no cookies, no CAPTCHA).

Files: one workbook per scheme (110 or so). The first sheet is the SEBI statement, but it leaves
derivatives out of the Grand Total; futures positions are on a second sheet "Derivative<code>"
with quantity and market value but no % of NAV. We add those lines as derivatives and compute
their % of NAV from the statement's Grand Total.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import unquote

from ..base import AMC, Document
from ..excel import col, num, read_workbook, text
from ..model import Holding, LayoutError, NotPublishedError, Scheme
from ..statement import Layout, parse_date, parse_statement

BASE = "https://www.hdfcfund.com"
API = "https://cms.hdfcfund.com/en/hdfc/api/v2/disclosures/monthfortportfolio"
HEADERS = {"Origin": BASE, "Referer": BASE + "/statutory-disclosure/portfolio/monthly-portfolio"}
MAJOR = ("EQUITY", "DEBT", "MONEY MARKET", "OTHERS", "DERIVATIVES")


def classify(major: str, sub: str, name: str, industry: str = "") -> str:
    m, s, n = major.upper(), sub.lower(), name.lower()
    if "treps" in s or "repo" in s or "net current assets" in s or "net current assets" in n \
            or "cash margin" in n or "fixed deposit" in s or "deposit" in s:
        return "cash"
    if "option" in s or "future" in s:
        return "derivative"
    if any(k in s for k in ("reit", "invit", "mutual fund units", "alternative investment",
                            "gold", "silver", "exchange traded")):
        return "other"
    if m.startswith("EQUITY"):
        return "equity"
    if m.startswith(("DEBT", "MONEY MARKET")):
        return "debt"
    if m.startswith("DERIVATIVES"):
        return "derivative"
    if m.startswith("OTHERS"):
        return "other"
    raise LayoutError(f"hdfc: cannot classify {name!r} under {major!r} / {sub!r}")


LAYOUT = Layout(header_keys=["isin", "instrument", "% to nav"],
                is_major=lambda t: t.upper().startswith(MAJOR), classify=classify)


def parse_derivatives(rows: list[list], nav_lakhs: float, sheet: str) -> tuple[list[Holding], list[str]]:
    """Futures tables (sections A and B) from the 'Derivative...' sheet. Options and swaps are
    reported as warnings: they are not in the statement and carry no market value to extract."""
    holdings: list[Holding] = []
    warnings: list[str] = []
    i = 0
    while i < len(rows):
        first = text(rows[i][0]) if rows[i] else ""
        m = re.match(r"^([A-E])\.\s*(.*)$", first)
        if not m:
            i += 1
            continue
        letter, title = m.groups()
        if title.rstrip(". ").lower().endswith(": nil") or title.lower().endswith("- nil"):
            i += 1
            continue
        # The table header is the next row that starts with "Underlying" / "Scheme name".
        j = next((k for k in range(i + 1, min(i + 4, len(rows)))
                  if text(rows[k][0]).lower() in ("underlying", "scheme name")), None)
        if j is None:
            i += 1
            continue
        header = {text(c).lower(): n for n, c in enumerate(rows[j]) if text(c)}
        if letter in "ABCD" and text(rows[j][0]).lower() != "underlying":
            # "Scheme Name | Total number of contracts ..." is the month's trading summary,
            # not open positions.
            i = j + 1
            continue
        if letter in ("A", "B"):
            c_ind = col(header, "industry")
            c_qty = col(header, "long / (short)", "long/(short)", "long")
            c_mv = col(header, "market value")
            if c_qty is None or c_mv is None:
                raise LayoutError(f"hdfc: {sheet}: futures table without quantity/market value: {list(header)}")
            k = j + 1
            while k < len(rows) and text(rows[k][0]) and num(rows[k][0]) is None:
                r = list(rows[k]) + [None] * 10
                mv = num(r[c_mv])
                holdings.append(Holding(
                    instrument_name=f"{text(r[0])} Futures",
                    industry_or_rating=text(r[c_ind]) if c_ind is not None else "",
                    asset_type="derivative",
                    quantity=num(r[c_qty]),
                    market_value_lakhs=mv,
                    pct_of_nav=None if mv is None or not nav_lakhs else mv / nav_lakhs * 100,
                ))
                k += 1
            i = k
            continue
        # C/D options, E swaps: count the lines and say so.
        k, n = j + 1, 0
        while k < len(rows) and text(rows[k][0]):
            n += 1
            k += 1
        if n:
            warnings.append(f"{sheet}: section {letter} ({title[:60]}) has {n} line(s) that are not "
                            f"holdings with a market value (options/swaps); not extracted")
        i = k
    return holdings, warnings


def _scheme_name(title: str) -> str:
    """'HDFC Arbitrage Fund (An open ended scheme investing in ...). (Formerly ...)' -> 'HDFC Arbitrage Fund'."""
    return re.split(r"\s*\((?:an|a)\s+(?:open|close|interval)", title, maxsplit=1, flags=re.I)[0].strip()


class HDFC(AMC):
    key = "hdfc"
    name = "HDFC Mutual Fund"
    homepage = BASE

    def discover(self, month: date) -> list[Document]:
        fy = month.year if month.month >= 4 else month.year - 1
        raw = self.fetcher.request("POST", API, form={"year": fy, "type": "monthly", "month": month.month},
                                   headers=HEADERS, cache=False)
        try:
            files = json.loads(raw)["data"]["files"]
        except (ValueError, KeyError, TypeError) as e:
            raise LayoutError(f"hdfc: portfolio API reply changed shape ({e})") from None
        if not files:
            raise NotPublishedError(f"hdfc: no monthly portfolio files for {month:%B %Y}")
        docs = []
        for f in files:
            title, url = unquote(f.get("title", "")), (f.get("file") or {}).get("url", "")
            # Scheme portfolios are titled "Monthly <scheme> - 30 September 2026.xlsx". The same list
            # sometimes carries other documents (e.g. "PortfolioOverlap31Aug2026.xlsx"): skip those.
            if not title.lower().startswith("monthly "):
                continue
            if not url:
                raise LayoutError(f"hdfc: file entry without a URL: {title!r}")
            # Guard against the API silently returning another month.
            if f"{month:%B} {month.year}".lower() not in title.lower():
                raise LayoutError(f"hdfc: asked for {month:%B %Y} but got {title!r}")
            docs.append(Document(url=url, label=title))
        if not docs:
            raise NotPublishedError(f"hdfc: no scheme portfolios among {len(files)} files for {month:%B %Y}")
        return docs

    def expected_scheme_count(self, month: date, docs: list[Document]) -> int | None:
        return len(docs)  # one workbook per scheme

    def parse(self, path: Path, month: date) -> list[Scheme]:
        sheets = read_workbook(path)
        main = [n for n in sheets if not n.lower().startswith("derivative")]
        deriv = [n for n in sheets if n.lower().startswith("derivative")]
        if len(main) != 1:
            raise LayoutError(f"hdfc: {path.name}: unexpected sheets {list(sheets)}")
        rows = sheets[main[0]]
        titles = [text(next((c for c in r if text(c)), "")) for r in rows[:4]]
        as_on = next((t for t in titles if "as on" in t.lower()), "")
        if not titles or not as_on:
            raise LayoutError(f"hdfc: {main[0]}: unexpected title block {titles}")
        month_end = parse_date(as_on)
        if (month_end.year, month_end.month) != (month.year, month.month):
            raise LayoutError(f"hdfc: {main[0]}: dated {month_end}, expected {month:%Y-%m}")
        scheme = parse_statement(rows, LAYOUT, sheet=main[0], scheme_name=_scheme_name(titles[0]),
                                 month_end=month_end)
        # Usually one "Derivative..." sheet; Multi-Asset has two (futures, and swaps).
        nav = sum(h.market_value_lakhs or 0 for h in scheme.holdings)
        for name in deriv:
            extra, warns = parse_derivatives(sheets[name], nav, name)
            if extra:
                scheme.holdings.extend(extra)
                scheme.nav_excludes_derivatives = True
            scheme.warnings.extend(warns)
        return [scheme]
