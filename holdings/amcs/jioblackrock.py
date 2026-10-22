"""Jio BlackRock Mutual Fund (jioblackrockamc.com).

Discovery: Statutory Disclosure -> Disclosures -> Monthly Portfolio Disclosure
(/statutory-disclosure/disclosures/monthly-portfolio-disclosure) is a Next.js App Router page. Its
document list comes from a React *server action*: the browser POSTs to the page URL with a
`Next-Action: <id>` header and a JSON array of arguments, and gets a React Flight (text/x-component)
reply whose line `1:` is JSON:

  POST /statutory-disclosure/disclosures/monthly-portfolio-disclosure
       Next-Action: 70f23aee...     body: ["monthly-portfolio-disclosure",
                                           {"year": "FI2026-2027", "month": "September"}, "MF"]
  -> 1:{"data": [{"title": "JioBlackRock Flexi Cap Fund-Monthly-Portfolio-30-09-2026",
                   "file": {"url": "https://cdnstorage-...azurefd.net/brcms/....xlsx"}}, ...]}

The action id is a hash generated at build time, so it changes with every deploy. We read it from
the page's own JavaScript chunk, where the client stub is declared as
`createServerReference("<id>", ..., "getDisclosureL3Data")`. Plain HTTP works; no browser needed.
(A headless browser actually renders this page as "Oops! Something went wrong".)

Files: one workbook per scheme, plus (from August) a "JioBlackRock Mutual Fund-Monthly-Portfolio"
workbook that bundles every scheme behind an "Index" sheet. In July the bundle was published under
the Money Market Fund's title instead, so the Money Market Fund exists only inside it. We therefore
parse every listed file, then keep one copy of each scheme, and fail if two copies of the same
scheme disagree.

Layout traps: futures are listed in a second table *after* the Grand Total (the Grand Total
excludes them), and the "Money Market Instruments" heading sometimes carries the previous section's
total in its value columns, which would otherwise be read as a holding.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import quote

from ..base import AMC, Document
from ..excel import col, num, read_workbook, text
from ..model import Holding, LayoutError, NotPublishedError, Scheme
from ..excel import lakhs_factor
from ..statement import ISIN_LIKE, Layout, parse_date, parse_statement

BASE = "https://www.jioblackrockamc.com"
SLUG = "monthly-portfolio-disclosure"
PAGE = f"{BASE}/statutory-disclosure/disclosures/{SLUG}"
CHUNK_RE = re.compile(r'(/_next/static/chunks/app/[^"\\]*?statutory-disclosure/[^"\\]*?page-[0-9a-f]+\.js)')
ACTION_RE = re.compile(r'createServerReference\)\("([0-9a-f]{40,})",[^)]*?"getDisclosureL3Data"\)')
TITLE_RE = re.compile(r"^(?P<scheme>.+?)\s*-\s*Monthly-Portfolio-(?P<d>\d{2})-(?P<m>\d{2})-(?P<y>\d{4})\s*$", re.I)
BUNDLE_TITLE = "jioblackrock mutual fund"
MAJOR = ("equity & equity related", "debt instruments", "money market instruments", "government securities",
         "alternative investment fund", "mutual fund units", "real estate investment trust", "reverse repo",
         "infrastructure investment trust", "exchange traded fund")
SUBS = ("(a) listed", "(b) unlisted", "(b) privately placed", "(c) securitised", "(c) securitized",
        "certificate of deposit", "commercial paper", "treasury bill")


def parse_flight(raw: str) -> dict:
    """React Flight reply -> the JSON value of row 1 (the action's return value)."""
    for line in raw.splitlines():
        if line.startswith("1:"):
            return json.loads(line[2:])
    raise LayoutError(f"jioblackrock: server action reply has no result row: {raw[:120]!r}")


def classify(major: str, sub: str, name: str, industry: str = "") -> str:
    m, s, n = major.lower(), sub.lower(), name.lower()
    if n.startswith(("treps", "reverse repo", "net receivables", "margin")) or m == "reverse repo":
        return "cash"
    if m.startswith(("alternative investment", "mutual fund units", "real estate investment",
                     "infrastructure investment", "exchange traded")):
        return "other"
    if m.startswith("equity"):
        return "equity"
    if m.startswith(("debt", "money market", "government securities")):
        return "debt"
    raise LayoutError(f"jioblackrock: cannot classify {name!r} under {major!r} / {sub!r}")


LAYOUT = Layout(header_keys=["isin", "instrument", "% to net assets"],
                is_major=lambda t: t.lower().startswith(MAJOR), classify=classify)


def clear_heading_values(rows: list[list], sheet: str, warnings: list[str]) -> list[list]:
    """Blank the value cells of section headings (no ISIN, a known heading name)."""
    out = []
    for row in rows:
        r = list(row)
        name = text(r[1]) if len(r) > 1 else ""
        low = name.lower()
        is_heading = low in MAJOR + SUBS or (low.startswith("(") and low.startswith(SUBS))
        if is_heading and not ISIN_LIKE.match(text(r[2]) if len(r) > 2 else "") \
                and any(num(c) is not None for c in r[3:]):
            warnings.append(f"{sheet}: heading {name!r} carries values {[text(c) for c in r[3:7]]}; ignored")
            r = r[:2] + [None] * (len(r) - 2)
        out.append(r)
    return out


def parse_futures(rows: list[list], sheet: str) -> list[Holding]:
    """The 'Derivatives' table after the Grand Total: Name | Long / Short | Industry | Quantity |
    Market value | % to Net Assets."""
    gt = next((i for i, r in enumerate(rows) if len(r) > 1 and text(r[1]).lower().startswith("grand total")), None)
    if gt is None:
        return []
    h = next((i for i in range(gt + 1, len(rows)) if len(rows[i]) > 2
              and text(rows[i][1]).lower().startswith("name of the instrument")
              and "long" in text(rows[i][2]).lower()), None)
    if h is None:
        return []
    header = {text(c).lower(): j for j, c in enumerate(rows[h]) if text(c)}
    c_ind, c_qty = col(header, "industry"), col(header, "quantity")
    c_mv, c_pct = col(header, "market"), col(header, "% to net")
    if None in (c_qty, c_mv, c_pct):
        raise LayoutError(f"jioblackrock: {sheet}: derivatives table header {list(header)}")
    factor = lakhs_factor(next(k for k, j in header.items() if j == c_mv))
    out = []
    for r in rows[h + 1:]:
        r = list(r) + [None] * 8
        name = text(r[1])
        if not name or name.lower().startswith(("total", "sub total", "grand total")):
            break
        mv = num(r[c_mv])
        out.append(Holding(instrument_name=name, industry_or_rating=text(r[c_ind]) if c_ind else "",
                           asset_type="derivative", quantity=num(r[c_qty]),
                           market_value_lakhs=None if mv is None else mv * factor, pct_of_nav=num(r[c_pct])))
    return out


def _fingerprint(s: Scheme) -> list[tuple]:
    return sorted((h.isin, h.instrument_name, h.quantity, round(h.market_value_lakhs or 0, 2)) for h in s.holdings)


class JioBlackRock(AMC):
    key = "jioblackrock"
    name = "Jio BlackRock Mutual Fund"
    homepage = BASE

    def _action_id(self) -> str:
        page = self.fetcher.get(PAGE, cache=False).decode("utf-8", "replace")
        chunks = [c for c in CHUNK_RE.findall(page) if "%5Bl2Id%5D" in c or "[l2Id]" in c]
        if not chunks:
            raise LayoutError("jioblackrock: disclosure page no longer loads the [l2Id] page chunk")
        for c in dict.fromkeys(chunks):
            js = self.fetcher.get(BASE + c).decode("utf-8", "replace")  # content-hashed name: cacheable
            m = ACTION_RE.search(js)
            if m:
                return m.group(1)
        raise LayoutError("jioblackrock: getDisclosureL3Data server action not found in the page chunk")

    def discover(self, month: date) -> list[Document]:
        fy = month.year if month.month >= 4 else month.year - 1
        args = [SLUG, {"year": f"FI{fy}-{fy + 1}", "month": f"{month:%B}"}, "MF"]
        raw = self.fetcher.request(
            "POST", PAGE, data=json.dumps(args, separators=(",", ":")), cache=False,
            headers={"Accept": "text/x-component", "Next-Action": self._action_id(),
                     "Content-Type": "text/plain;charset=UTF-8", "Origin": BASE, "Referer": PAGE},
        ).decode("utf-8", "replace")
        items = parse_flight(raw).get("data") or []
        docs = []
        for it in items:
            title = (it.get("title") or "").strip()
            m = TITLE_RE.match(title)
            if not m:
                raise LayoutError(f"jioblackrock: unexpected document title {title!r}")
            if (int(m["y"]), int(m["m"])) != (month.year, month.month):
                raise LayoutError(f"jioblackrock: asked for {month:%B %Y} but got {title!r}")
            url = (it.get("file") or {}).get("url")
            if not url:
                raise LayoutError(f"jioblackrock: {title!r} has no file URL")
            docs.append(Document(url=quote(url, safe=":/?=&%"), label=title))
        if not docs:
            raise NotPublishedError(f"jioblackrock: no monthly portfolios listed for {month:%B %Y}")
        return docs

    def expected_scheme_count(self, month: date, docs: list[Document]) -> int | None:
        return self._expected

    def parse(self, path: Path, month: date) -> list[Scheme]:
        sheets = read_workbook(path)
        index = sheets.pop("Index", None)
        if index is not None:  # the bundle: Sr No. | Short Name | Scheme Name
            listed = [text(r[1]) for r in index[1:] if len(r) > 2 and text(r[1])]
            if set(listed) != set(sheets):
                raise LayoutError(f"jioblackrock: {path.name}: Index lists {sorted(listed)}, sheets are {sorted(sheets)}")
            self._expected = max(self._expected or 0, len(listed))
        elif len(sheets) != 1:
            raise LayoutError(f"jioblackrock: {path.name}: expected one sheet, got {list(sheets)}")
        schemes = []
        for sheet, rows in sheets.items():
            title = [text(r[1]) if len(r) > 1 else "" for r in rows[:3]]
            if len(title) < 3 or not title[0] or "portfolio statement as on" not in title[2].lower():
                raise LayoutError(f"jioblackrock: sheet {sheet}: unexpected title block {title}")
            month_end = parse_date(title[2])
            if (month_end.year, month_end.month) != (month.year, month.month):
                raise LayoutError(f"jioblackrock: sheet {sheet}: dated {month_end}, expected {month:%Y-%m}")
            warnings: list[str] = []
            scheme = parse_statement(clear_heading_values(rows, sheet, warnings), LAYOUT, sheet=sheet,
                                     scheme_name=re.sub(r"\s*\$+$", "", title[0]).strip(), month_end=month_end)
            futures = parse_futures(rows, sheet)
            if futures:
                if scheme.nav_excludes_derivatives is False and any(h.asset_type == "derivative" for h in scheme.holdings):
                    raise LayoutError(f"jioblackrock: {sheet}: derivatives both in the statement and after it")
                scheme.holdings.extend(futures)
                scheme.nav_excludes_derivatives = True
            scheme.warnings[:0] = warnings
            schemes.append(scheme)
        return schemes

    def fetch(self, month: date):
        self._expected = None
        docs = self.discover(month)
        by_name: dict[str, Scheme] = {}
        for doc in docs:
            found = self.parse(self.fetcher.download(doc.url), month)
            if len(found) > 1 and TITLE_RE.match(doc.label)["scheme"].strip().lower() != BUNDLE_TITLE:
                found[0].warnings.append(f"the file titled {doc.label!r} is the all-schemes workbook")
            for s in found:
                s.source = f"{doc.label} / {s.source}"
                prev = by_name.get(s.name)
                if prev is None:
                    by_name[s.name] = s
                elif _fingerprint(prev) != _fingerprint(s):
                    raise LayoutError(f"jioblackrock: {s.name} differs between {prev.source!r} and {s.source!r}")
        if self._expected is None:
            self._expected = len(docs)
        return list(by_name.values()), docs, self._expected
