"""Mahindra Manulife Mutual Fund (mahindramanulife.com).

Discovery: the site is a React single-page app. Its "Downloads" page loads the whole download
tree from one API call:

  GET https://investorapi.mahindramanulife.com/api/v1/web/preLogin/downloads
  -> {"payload": "<base64>"}

The payload is AES-256-CBC encrypted (PKCS7, base64). The key and IV are string constants in the
site's public JavaScript bundle (/assets/index-<hash>.js); the bundle decrypts the reply in the
visitor's browser. No login or CAPTCHA is involved. We read the bundle name from the homepage,
then the API base, key and IV from the bundle, so a redeploy that rotates any of them still works
as long as the code keeps the same shape. Decrypted, the tree is:

  MANDATORY DISCLOSURES > Portfolio Disclosure > Monthly Portfolio Disclosure > <year>
     files: [{"title": "Monthly Portfolio Disclosure - September, 2026", "fileUrl": "...xlsx"}]

Files: one workbook, one sheet per scheme (MMF01, MMF02, ...). Row 2 has the date, row 3 the
scheme name. After "Margin placed with CCIL" and "Net Receivables / (Payables)" most sheets print
a second "Net Receivables / (Payables)" line that is the sum of the two. Counting it double-counts
cash and makes % of NAV look wrong, so we drop it (see `drop_net_receivables_subtotal`).
"""
from __future__ import annotations

import base64
import json
import re
from datetime import date
from pathlib import Path

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..base import AMC, Document
from ..excel import num, read_workbook, text
from ..model import LayoutError, NotPublishedError, Scheme
from ..statement import ISIN_LIKE, Layout, month_names, parse_date, parse_statement

BASE = "https://www.mahindramanulife.com"
HEADERS = {"Accept": "application/json", "platform": "web", "Origin": BASE, "Referer": BASE + "/"}
BUNDLE_RE = re.compile(r'src="(/assets/index-[^"]+\.js)"')
API_RE = re.compile(r'"(https://investorapi\.[a-z.]+/api/v\d+/web)"')
# const jE="<32 chars>",DE="<16 chars>",Ib=ja.enc.Utf8.parse(jE),Eb=ja.enc.Utf8.parse(DE)
KEY_RE = re.compile(r'(\w+)="([^"\\]{32})",(\w+)="([^"\\]{16})",\w+=\w+\.enc\.Utf8\.parse\(\1\),'
                    r'\w+=\w+\.enc\.Utf8\.parse\(\3\)')
PATH = ["MANDATORY DISCLOSURES", "Portfolio Disclosure", "Monthly Portfolio Disclosure"]
# Top-level headings. Sub-headings ("(a) Listed ...", "Treasury Bill/...", "Commercial Paper",
# "Index / Stock Futures") keep the heading above them.
MAJOR = ("equity & equity related", "debt instruments", "money market instruments", "derivatives", "others",
         "reits", "invit", "mutual fund units", "alternative investment fund", "exchange traded funds",
         "foreign securities", "preference shares", "treps", "reverse repo")


def classify(major: str, sub: str, name: str, industry: str = "") -> str:
    m, s, n = major.lower(), sub.lower(), name.lower()
    where = f"{m} | {s}"
    if m in ("treps", "reverse repo") or "net receivables" in n or "margin" in n \
            or "treps" in n or "fixed deposit" in where:
        return "cash"
    if m.startswith("derivatives") or "future" in s or "option" in s:
        return "derivative"
    if any(k in where for k in ("reit", "invit", "mutual fund units", "alternative investment",
                                "corporate debt market development", "exchange traded", "overseas etf")):
        return "other"
    if m.startswith("preference shares"):
        return "debt"  # non-convertible preference shares carry a credit rating
    if m.startswith("equity"):
        return "equity"
    if m.startswith(("debt", "money market")):
        return "debt"
    if m.startswith("others"):
        return "other"
    raise LayoutError(f"mahindra: cannot classify {name!r} under {major!r} / {sub!r}")


LAYOUT = Layout(header_keys=["isin", "instrument", "% to net assets"],
                is_major=lambda t: t.lower().startswith(MAJOR), classify=classify)


def decrypt(payload: str, key: bytes, iv: bytes) -> str:
    d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    raw = d.update(base64.b64decode(payload)) + d.finalize()
    u = padding.PKCS7(128).unpadder()
    return (u.update(raw) + u.finalize()).decode("utf-8")


def find_monthly_files(tree: list[dict], year: int) -> list[dict]:
    """Walk MANDATORY DISCLOSURES > ... > Monthly Portfolio Disclosure > <year> by name."""
    nodes = tree
    for name in PATH + [str(year)]:
        node = next((n for n in nodes if n.get("categoryName", "").strip().lower() == name.lower()), None)
        if node is None:
            if name == str(year):
                return []
            raise LayoutError(f"mahindra: download tree has no {name!r} under {' > '.join(PATH)}")
        nodes = node.get("subcategories") or []
    return node.get("files") or []


def drop_net_receivables_subtotal(rows: list[list], sheet: str, warnings: list[str]) -> list[list]:
    """Remove the 'Net Receivables / (Payables)' line that repeats the line above and equals the sum
    of the preceding run of cash lines without an ISIN (Margin + Net Receivables)."""
    out: list[list] = []
    run: list[tuple[str, float]] = []  # consecutive valued lines without an ISIN
    for row in rows:
        r = list(row) + [None] * 7
        name, isin, mv = text(r[1]), text(r[2]).replace(" ", ""), num(r[5])
        if name.lower().startswith(("sub total", "total", "grand total")):
            run = []
        elif name and mv is not None and not ISIN_LIKE.match(isin):
            if len(run) >= 2 and name.lower() == run[-1][0].lower() \
                    and abs(mv - sum(v for _, v in run)) <= 0.02:
                warnings.append(f"{sheet}: dropped {name!r} = {mv} (a subtotal of the "
                                f"{len(run)} lines above it, not a holding)")
                run = []
                continue
            run.append((name, mv))
        elif name:
            run = []
        out.append(row)
    return out


class MahindraManulife(AMC):
    key = "mahindra"
    name = "Mahindra Manulife Mutual Fund"
    homepage = BASE

    def _api(self) -> tuple[str, bytes, bytes]:
        home = self.fetcher.get(BASE + "/", cache=False).decode("utf-8", "replace")
        m = BUNDLE_RE.search(home)
        if not m:
            raise LayoutError("mahindra: homepage no longer loads /assets/index-*.js")
        js = self.fetcher.get(BASE + m.group(1)).decode("utf-8", "replace")  # hashed name: cache is safe
        api, keys = API_RE.search(js), KEY_RE.search(js)
        if not api or not keys:
            raise LayoutError("mahindra: could not find the API base or the AES key/IV in the JS bundle")
        return api.group(1), keys.group(2).encode(), keys.group(4).encode()

    def discover(self, month: date) -> list[Document]:
        api, key, iv = self._api()
        raw = self.fetcher.get(api + "/preLogin/downloads", headers=HEADERS, cache=False)
        try:
            tree = json.loads(decrypt(json.loads(raw)["payload"], key, iv))["data"]
        except (ValueError, KeyError, TypeError) as e:
            raise LayoutError(f"mahindra: downloads reply could not be decrypted/read ({e})") from None
        files = find_monthly_files(tree, month.year)
        label_re = re.compile(rf"monthly portfolio disclosure\s*-\s*({'|'.join(month_names(month.month))})\w*"
                              rf"\s*,?\s*{month.year}\s*$", re.I)
        docs = [Document(url=f["fileUrl"], label=f["title"].strip()) for f in files
                if label_re.search(f.get("title", "").strip()) and f.get("fileUrl")]
        if not docs:
            raise NotPublishedError(f"mahindra: no monthly portfolio for {month:%B %Y} "
                                    f"(listed for {month.year}: {[f.get('title') for f in files][:3]})")
        if len(docs) > 1:
            raise LayoutError(f"mahindra: {len(docs)} files match {month:%B %Y}: {[d.label for d in docs]}")
        return docs

    def parse(self, path: Path, month: date) -> list[Scheme]:
        schemes = []
        for sheet, rows in read_workbook(path).items():
            title = [text(r[1]) if len(r) > 1 else "" for r in rows[:4]]
            if len(title) < 3 or "portfolio statement as on" not in title[1].lower() or not title[2]:
                raise LayoutError(f"mahindra: sheet {sheet}: unexpected title block {title}")
            month_end = parse_date(title[1])
            if (month_end.year, month_end.month) != (month.year, month.month):
                raise LayoutError(f"mahindra: sheet {sheet}: dated {month_end}, expected {month:%Y-%m}")
            warnings: list[str] = []
            rows = drop_net_receivables_subtotal(rows, sheet, warnings)
            scheme = parse_statement(rows, LAYOUT, sheet=sheet, scheme_name=title[2], month_end=month_end)
            scheme.warnings[:0] = warnings
            schemes.append(scheme)
        return schemes
