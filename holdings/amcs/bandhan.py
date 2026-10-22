"""Bandhan Mutual Fund (bandhanmutual.com). The one AMC that needs a headless browser.

Where the files are: Statutory Disclosures -> Disclosure of Detailed Scheme Portfolios ->
"Monthly and Half Yearly" (/statutory-disclosures/scheme-portfolios/monthly-half-yearly). The page
asks for a year, a month and a scheme, then lists that scheme's file.

Why plain HTTP does not work here (checked; see NOTES.md):
  - every page is the same 3 KB React shell; the HTML has no links or data;
  - the list comes from POST https://pnservices.bandhanmutual.com/.../dashboard/cms-call, whose
    request and reply bodies are both encrypted by an 11 MB obfuscated bundle (not WebCrypto, no
    readable key), so the call cannot be reproduced without reverse-engineering that code;
  - the WordPress CMS behind it (cmsnew.bandhanmutual.com) answers its REST API with 401 "Only
    authenticated users", its feeds with 403, and its post pages carry no file links.

What we do: open the page in headless Chromium and let the site's own code make that call. The
page's first request filters by one scheme; an init script widens the request object *before the
site encrypts it* (same type and subcategory, our year and month, no scheme filter, a page size
that holds the whole month), and reads the reply after the site decrypts it. No login, no CAPTCHA;
one page visit per month, with third-party trackers, images and fonts blocked. The reply is cached
so `--offline` and the tests do not need a browser. The workbooks themselves are plain downloads.

Files: one workbook per scheme, one sheet. Traps:
  - % to NAV is a fraction, and lines under 0.01% print "$" instead of a number; we compute those
    from market value / NAV.
  - "Net Current Assets" is printed after "Cash Margin - Derivatives", "Cash / Bank Balance" and
    "Net Receivables/Payables" and equals their sum; it is a subtotal, so we drop it when it does.
  - Physical gold/silver bars carry made-up codes in the ISIN column ("DBXXXGOLD100").
  - Foreign ETFs / funds have no ISIN in the file (only an internal code such as "14137311USD").
  - The "Derivative Disclosure" tables after the Grand Total have names shifted against their
    contract codes in some files; we take derivatives from the statement itself.
"""
from __future__ import annotations

import html
import json
import re
from datetime import date
from pathlib import Path

from .. import browser
from ..base import AMC, Document
from ..excel import num, read_workbook, text
from ..model import LayoutError, NotPublishedError, Scheme
from ..statement import ISIN_LIKE, Layout, parse_date, parse_statement

BASE = "https://bandhanmutual.com"
PAGE = BASE + "/statutory-disclosures/scheme-portfolios/monthly-half-yearly"
PAGE_SIZE = 500

HOOK = """
((year, month, perPage) => {
  const parse = JSON.parse, stringify = JSON.stringify;
  window.__holdings = [];
  JSON.stringify = function (v, r, s) {
    if (v && v.type === 'SCHEME_PORTFOLIOS' && v.data && v.data.subcategory) {
      v = {type: v.type, data: {subcategory: v.data.subcategory, page: 1, financial_year: year,
                                month: month, posts_per_page: perPage}};
    }
    return stringify.call(this, v, r, s);
  };
  JSON.parse = function (t, r) {
    const v = parse.call(this, t, r);
    // Our widened request is the only one with this page size. For a month that is not out yet
    // the site answers it with no posts and a blank month, so match on the page size too.
    if (v && typeof v === 'object' && Array.isArray(v.data) && ((v.selected_month === month
        && String(v.selected_year) === year) || (v.posts_per_page === perPage && !v.data.length)))
      window.__holdings.push(t);
    return v;
  };
})(%s, %s, %d);
"""
RESULT = "() => window.__holdings || []"

MAJOR = ("equity & equity related", "debt instruments", "money market instruments", "derivatives", "others",
         "mutual fund units", "exchange traded funds", "international exchange traded funds",
         "international mutual fund units", "gold", "silver", "corporate debt market development fund",
         "reits", "invit", "real estate investment trust", "infrastructure investment trust")
CASH_NAMES = ("cash margin", "cash / bank", "cash/bank", "net receivables", "net current assets", "triparty repo",
              "reverse repo", "amc repo clearing", "treps")
NCA = "net current assets"


def classify(major: str, sub: str, name: str, industry: str = "") -> str:
    m, s, n = major.lower(), sub.lower(), name.lower()
    where = f"{m} | {s}"
    if n.startswith(CASH_NAMES) or "treps" in s or "reverse repo" in s:
        return "cash"
    if m == "derivatives" or "future" in s or "option" in s or "interest rate swap" in where + n:
        return "derivative"
    if any(k in where for k in ("mutual fund units", "exchange traded", "corporate debt market development",
                                "reit", "invit", "investment trust")) or m in ("gold", "silver"):
        return "other"
    if m.startswith("equity"):
        return "equity"
    if m.startswith(("debt", "money market")):
        return "debt"
    raise LayoutError(f"bandhan: cannot classify {name!r} under {major!r} / {sub!r}")


LAYOUT = Layout(header_keys=["isin", "instrument", "% to nav"],
                is_major=lambda t: t.lower() in MAJOR, classify=classify)


def drop_nca_subtotal(rows: list[list], sheet: str, warnings: list[str]) -> list[list]:
    """Drop 'Net Current Assets' when it equals the sum of the two or more cash lines printed
    right above it (since the last Subtotal/Total)."""
    out: list[list] = []
    run: list[float] = []
    for row in rows:
        r = list(row) + [None] * 7
        name, mv = text(r[1]), num(r[5])
        low = name.lower()
        if low.startswith(("subtotal", "sub total", "total", "grand total")):
            run = []
        elif low.startswith(NCA) and mv is not None and len(run) >= 2 and abs(mv - sum(run)) <= 0.02:
            warnings.append(f"{sheet}: dropped {name!r} = {mv} (the sum of the {len(run)} cash lines above it)")
            run = []
            continue
        elif name and mv is not None and not ISIN_LIKE.match(text(r[2])):
            run.append(mv)
        elif name:
            run = []
        out.append(row)
    return out


def parse_listing(raw: str, month: date) -> list[Document]:
    d = json.loads(raw)
    posts = d.get("data") or []
    if not posts and not d.get("total_posts"):
        return []
    if d.get("total_posts") is not None and int(d["total_posts"]) > len(posts):
        raise LayoutError(f"bandhan: listing has {d['total_posts']} posts but returned {len(posts)}")
    docs = []
    for post in posts:
        acf = post.get("acf_fields") or {}
        if (acf.get("month") or "").lower() != f"{month:%B}".lower() or str(post.get("financial_year")) != str(month.year):
            raise LayoutError(f"bandhan: asked for {month:%B %Y}, listing has {acf.get('month')} {post.get('financial_year')}")
        files = [f for f in acf.get("disclosure_files") or [] if f.get("url", "").lower().endswith((".xlsx", ".xls"))]
        if len(files) != 1:
            raise LayoutError(f"bandhan: {post.get('title')!r} has {len(files)} workbooks")
        docs.append(Document(url=files[0]["url"], label=html.unescape(acf.get("document_name") or post.get("title", ""))))
    return docs


class Bandhan(AMC):
    key = "bandhan"
    name = "Bandhan Mutual Fund"
    homepage = BASE

    def _listing(self, month: date) -> str:
        cached = self.fetcher.cache_dir / "bandhanmutual.com" / f"monthly-listing-{month:%Y-%m}.json"
        if self.fetcher.offline:
            if not cached.exists():
                raise FileNotFoundError(f"offline mode and no cached Bandhan listing for {month:%Y-%m}")
            return cached.read_text()
        got = browser.collect(PAGE, HOOK % (json.dumps(str(month.year)), json.dumps(f"{month:%B}"), PAGE_SIZE), RESULT)
        raw = got[-1]
        if json.loads(raw).get("data"):  # an empty month is not cached: it may be published later
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_text(raw)
        return raw

    def discover(self, month: date) -> list[Document]:
        raw = self._listing(month)
        docs = parse_listing(raw, month)
        if not docs:
            months = json.loads(raw).get("months") or []
            raise NotPublishedError(f"bandhan: no monthly portfolios listed for {month:%B %Y}"
                                    f" (site's month list: {', '.join(months)})")
        return docs

    def expected_scheme_count(self, month: date, docs: list[Document]) -> int | None:
        return len(docs)

    def parse(self, path: Path, month: date) -> list[Scheme]:
        sheets = read_workbook(path)
        if len(sheets) != 1:
            raise LayoutError(f"bandhan: {path.name}: expected one sheet, got {list(sheets)}")
        (sheet, rows), = sheets.items()
        title = [text(r[1]) if len(r) > 1 else "" for r in rows[:2]]
        if len(title) < 2 or "portfolio statement as on" not in title[0].lower() or not title[1]:
            raise LayoutError(f"bandhan: {sheet}: unexpected title block {title}")
        month_end = parse_date(title[0])
        if (month_end.year, month_end.month) != (month.year, month.month):
            raise LayoutError(f"bandhan: {sheet}: dated {month_end}, expected {month:%Y-%m}")
        warnings: list[str] = []
        scheme = parse_statement(drop_nca_subtotal(rows, sheet, warnings), LAYOUT, sheet=sheet,
                                 scheme_name=title[1], month_end=month_end)
        # "$" = under 0.01% of NAV: compute it. NAV = the lines the Grand Total counts.
        nav = sum(h.market_value_lakhs or 0 for h in scheme.holdings
                  if not (scheme.nav_excludes_derivatives and h.asset_type == "derivative"))
        filled = 0
        for h in scheme.holdings:
            if h.pct_of_nav is None and h.market_value_lakhs is not None and nav:
                h.pct_of_nav = h.market_value_lakhs / nav * 100
                filled += 1
            if h.asset_type == "other" and h.isin.upper().startswith("DBXXX"):
                h.isin = ""  # placeholder code for physical gold/silver bars, not an ISIN
        if filled:
            warnings.append(f"{sheet}: {filled} line(s) print '$' (< 0.01% of NAV); computed from market value")
        scheme.warnings[:0] = warnings
        return [scheme]
