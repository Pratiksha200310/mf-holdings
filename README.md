# mf-holdings

Downloads the official monthly portfolio disclosures of six Indian AMCs and turns them into one
clean CSV per AMC and month, a JSON report that checks each file against itself, and an offline
HTML grid of what was extracted.

AMCs, in the brief's order: Quant, Sundaram, HDFC, Mahindra Manulife, Jio BlackRock, Bandhan.

```
python -m holdings 2026-09 --amc quant
python -m holdings 2026-07 2026-08 2026-09              # every AMC, three months
python -m holdings 2026-09 --amc hdfc --amc bandhan
python -m holdings 2026-09 --offline                     # re-run from the download cache only
```

The tool prints one `OK` or `FAIL` line per AMC and month, rebuilds `out/index.html`, and exits
with status 1 if anything failed. One AMC failing does not stop the others.

## Setup

Python 3.11, 3.12 or 3.13. Python 3.14 is not supported yet: the pinned Playwright depends on a
`greenlet` release that has no 3.14 wheel, so `pip install` would try to compile it.

macOS / Linux:

```
git clone https://github.com/Pratiksha200310/mf-holdings.git
cd mf-holdings
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium      # only needed for Bandhan
```

Windows (PowerShell):

```
git clone https://github.com/Pratiksha200310/mf-holdings.git
cd mf-holdings
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1                 # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements.txt
python -m playwright install chromium
```

A full run of three months downloads about 650 files at one per second per site, so it takes
15 to 20 minutes the first time and under a minute after that (everything is cached). The
committed `out/` is overwritten by a run; use `--out somewhere-else` to keep it untouched.

Bandhan is the only AMC that uses a browser, and only to get its list of files (see
[NOTES.md](NOTES.md#bandhan)). If Chromium lives somewhere else, set `HOLDINGS_CHROMIUM` to the
binary. If `HTTPS_PROXY` is set, the browser uses it too.

## Outputs

`out/<YYYY-MM>/<amc>.csv`, one row per holding line, with these columns:

| column | notes |
|---|---|
| amc | `quant`, `sundaram`, `hdfc`, `mahindra`, `jioblackrock`, `bandhan` |
| scheme_name | as printed in the file (footnote markers removed) |
| month_end | the date printed in the file, checked against the month you asked for |
| instrument_name | as printed |
| isin | from the file. Derivatives get their underlying's ISIN. Placeholders (`DUMMY…`, `DBXXX…`) are blanked |
| ticker | NSE symbol, else BSE code, looked up from the exchange lists by ISIN. Index futures get the index symbol |
| industry_or_rating | industry for equity and derivatives, rating for debt |
| asset_type | `equity`, `debt`, `derivative`, `cash`, `other` |
| quantity | negative for short positions, as printed |
| market_value_lakhs | converted to ₹ lakhs from the unit in the file's header |
| pct_of_nav | in percent (4.25 means 4.25%). Files that print fractions are scaled |

`out/<YYYY-MM>/<amc>_report.json` holds:

- `scheme_count`, and `expected_scheme_count` where the site gives an independent count;
- for each scheme, `pct_of_nav_total`. Some AMCs leave hedging derivatives out of NAV, so the
  report gives the total both with and without them, and says which one the AMC's Grand Total uses;
- `schemes_outside_97_103`;
- `security_lines_without_identifier`, meaning lines that are not cash and have neither an ISIN
  nor a ticker;
- `isins_failing_check_digit` and `physical_commodity_lines`;
- `source_file_warnings`, which lists everything odd found in the AMC's own files (dropped
  duplicate or subtotal lines, Sub Totals that do not add up, lines with "$" instead of a %, and
  so on).

`out/index.html` is a grid of AMC × month built from the reports. It is plain HTML with no
scripts, so it opens offline.

## How it works

```
holdings/
  __main__.py      command line
  base.py          AMC interface: discover(month) -> [Document]; parse(path, month) -> [Scheme]
  amcs/<amc>.py    one module per AMC: how to find the files, and that AMC's layout quirks
  fetch.py         every HTTP request: max 1 request/second per site, on-disk cache, browser-like headers
  browser.py       headless Chromium (Bandhan's file list only)
  excel.py         .xlsx/.xls reading, number and unit parsing
  statement.py     generic parser for the SEBI portfolio statement layout
  identifiers.py   ISIN check digit, NSE/BSE lists, derivative -> underlying resolution
  pipeline.py      enrich identifiers, validate, write CSV + report atomically
  dashboard.py     out/index.html
```

These rules come from the brief:

- **No hard-coded file links.** Every module finds the month's files the way the site does: by
  calling the site's listing page or API and matching on the month. When a site hides its API
  behind a build hash, an encryption key or a server-action id, the module reads that value from
  the site's own HTML or JavaScript on each run.
- **Plain HTTP first.** Five of the six AMCs work with `requests`. Bandhan uses a browser for its
  file list only; NOTES.md shows why plain HTTP fails there.
- **Politeness.** At most one request per second per site (`files.` and `cms.hdfcfund.com` count as one site),
  browser requests included. Every file is cached under `.cache/`.
  There are no logins and no CAPTCHA handling.
- **Fail loudly, no partial CSVs.** A month is written only if all of these hold:
  - every listed file parsed;
  - every sheet's lines add up to the file's own Grand Total, to within 0.1% (either with or
    without derivatives);
  - the scheme count matches the site's own count where one exists;
  - no scheme appears twice.

  Anything unexpected raises an error that names the file and sheet. CSV and report are written
  to a temp file and renamed into place.
- **Data from AMC sites only.** The only other sources are the NSE equity/SME/ETF lists and the
  BSE bhavcopy, used to map ISINs to tickers.

To add a seventh AMC, write one new file in `holdings/amcs/` that implements `discover` and
`parse` (usually a `Layout` plus a `classify` function for `statement.parse_statement`), then add
it to `REGISTRY`.

## Tests

```
python -m unittest discover -s tests        # or: pytest
```

The tests use small samples in `tests/samples/`, which hold one or two real schemes per AMC.
Each sample was picked because it contains one of that AMC's traps. Tests also cover the listing
parsers, the decryption and the identifier helpers. They make no network calls.
`tests/make_samples.py` rebuilds the samples from the cache.

## Results for July–September 2026

| AMC | Jul | Aug | Sep | notes |
|---|---|---|---|---|
| Quant | 29 | 29 | 31 | 2 Aug lines (QIP allotment) have no ISIN in the file |
| Sundaram | 40 | 40 | 40 | 4 interest-rate-swap lines have no ISIN, as is normal for swaps |
| HDFC | 109 | 110 | 111 | 7 Sep lines carry a `DUMMY` ISIN (HEG Graphite demerger) |
| Mahindra Manulife | 27 | 27 | 27 | no independent scheme count on the site |
| Jio BlackRock | 14 | 15 | 15 | |
| Bandhan | 77 | 78 | 77 | swap legs and 3 overseas funds have no ISIN in the file |

Every scheme in every month totals between 97% and 103% of NAV.

## AI use

This tool was built with Claude (Anthropic) through Claude Code. The AI did the site research,
wrote the code and tests, and drafted these documents. The work ran in a cloud sandbox and was
checked against the AMCs' live files. All design choices and trade-offs are recorded in NOTES.md.
