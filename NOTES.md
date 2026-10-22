# Notes per AMC

Each section covers four things: how the files were found, what broke, what is likely to break
next, and time spent.

Times are approximate wall-clock times for an AI-assisted session (see "AI use" in README.md).
The first AMC's time includes building the shared framework.

Some checks apply to every AMC:

- Every sheet must add up to its own Grand Total. Short derivatives may be counted or left out,
  and the report records which one the file did.
- The month printed in each file must match the month requested.
- Where the site has an independent scheme count, the parsed count must match it.

A failure on any of these stops the AMC for that month, and nothing is written.

---

## Quant

**How the files were found**

- The Statutory Disclosures page (`/statutorydisclosures.aspx`) loads its lists through ASP.NET
  page methods.
- `POST /statutorydisclosures.aspx/displaydisclouser` with
  `{"id": "<year>", "cat": "MONTHLY PORTFOLIO"}` returns `{"d": "<html>"}` with the year's
  links. We pick the one labelled "September 2026". It is one workbook, with one sheet per scheme.
- The same service with `cat = "MONTHLY PORTFOLIO - FUND - WISE"`
  (`displaydisfundname`) lists the per-scheme files. That gives an independent scheme count.

**What broke**

- Equity Savings and Multi Asset (Sep) print the TREPS and Net Current Assets blocks twice: the
  heading, the line and the Sub Total. We drop a line only when it is identical to the one above
  it in every cell except the serial number. Each drop is reported.
- Some schemes count short futures in the Grand Total and some do not, sometimes within the same
  month's workbook. We accept either and record which one each sheet used.
- In the July and August Arbitrage and Equity Savings sheets, the futures Sub Total has no line
  items, or fewer than it adds up to. The positions are missing from Quant's own file, so we cannot
  recover them. The report flags each case; it is not fatal.
- In July and August, derivative lines carry contract codes such as `BIOCON250826` in the ISIN
  column, or nothing at all. We resolve them to the underlying ISIN and NSE symbol through the
  symbol, the name with the expiry removed, and the equity names in the same file.
- Silver is held through an internal code, not an ISIN, and is reported as a physical commodity.
- August has 2 QIP allotment lines (Gujarat Themis) with no ISIN. These are reported.

**What will likely break**

- The month label text, for example "Sept 2026" or a new naming scheme.
- The `displaydisclouser` method name. It is misspelt and might get fixed one day.
- A new section heading that `classify` does not know. This fails loudly.

**Time:** about 2.5 h, including the shared pipeline, statement parser and ticker mapping.

---

## Sundaram

**How the files were found**

- The page `/monthly-fortnightly-adhoc-portfolios` has no links in its HTML. Choosing "Monthly"
  calls an AjaxPro method:
  `POST /ajax/Modules_Disclosure_Monthly_Fortnightly_Adhoc_Portfolios,App_Web_<hash>.ashx?_method=GetCategory`,
  with body `Catid=Monthly`.
- `App_Web_<hash>` is the name of the compiled assembly, so it changes with every deploy. We read
  the current proxy script URL from the page's `<script>` tags on each run.
- The reply is a JavaScript string of HTML, with one accordion per financial year and one tab per
  month. Each month has two workbooks: "Equity & Fund of Funds" and "Fixed Income".
- Each workbook has an `Index` sheet that maps acronyms to scheme names. We require the sheets and
  the Index to match exactly, and the Index gives the expected scheme count.

**What broke**

- % to NAV is printed as a fraction. We detect the scale from the Grand Total line.
- Derivative lines use different names from the equity lines they hedge. We match them on the same
  share count plus a shared distinctive word in the name, and the match must be unique.
- Interest-rate-swap legs have no ISIN, which is normal for swaps. They are reported.
- Written-off securities carry `IDIA…` internal codes, which fail the ISIN check digit. They are
  reported under `isins_failing_check_digit`.

**What will likely break**

- A change to the AjaxPro module name or method.
- The label format "… - Sep 2026".

**Time:** about 45 min.

---

## HDFC

**How the files were found**

- `www.hdfcfund.com` is behind Akamai, which returns 403 to clients that do not send
  browser-like `Accept` headers. With those headers, plain `requests` works. We use no cookies, no
  browser and no CAPTCHA.
- The Next.js page calls a CMS API with a multipart form:
  `POST https://cms.hdfcfund.com/en/hdfc/api/v2/disclosures/monthfortportfolio`, with fields
  `year=<FY start>`, `type=monthly`, `month=<1-12>`.
- The reply lists files titled "Monthly <scheme> - 30 September 2026.xlsx".

**What broke**

- The brief describes one large workbook, but HDFC now publishes **one workbook per scheme**:
  109, 110 and 111 files.
- The August list also contained `PortfolioOverlap31Aug2026.xlsx`. We keep only titles that start
  with "Monthly", and we also check that the month in each title matches.
- Futures are not in the statement. They sit on a separate `Derivative…` sheet with quantity and
  market value but no % of NAV, and the Grand Total excludes them. We add them as derivative lines
  and compute % of NAV from the statement's NAV.
  - Multi Asset has two derivative sheets.
  - The "Scheme Name | Total number of contracts…" tables are monthly trading summaries, not
    positions. We skip them.
- Options and swaps on those sheets have no market value, so they are listed as warnings and not
  as holdings.
- Headings and totals sit in the ISIN column, not the name column.
- There is one Sub Total per major section, not per sub-section.
- In September, HEG Graphite (from a demerger) appears with an ISIN of `DUMMY…`. We blank it, so
  it shows up in `security_lines_without_identifier` (7 lines).

**What will likely break**

- The CMS API path (`v2`).
- A move back to a single combined workbook.
- Akamai tightening its bot rules. If that happens, the fix is not to fight it.

**Time:** about 1 h.

---

## Mahindra Manulife

**How the files were found**

- The site is a React single-page app. Its Downloads page gets the whole download tree from
  `GET https://investorapi.mahindramanulife.com/api/v1/web/preLogin/downloads`.
- The reply is `{"payload": "<base64>"}`, encrypted with AES-256-CBC (PKCS7).
- The key and IV are string constants in the public JS bundle `/assets/index-<hash>.js`, and every
  visitor's browser uses them to decrypt the reply. On each run we read the bundle name from the
  homepage, then the API base, key and IV from the bundle.
- We walk the tree by name: MANDATORY DISCLOSURES › Portfolio Disclosure › Monthly Portfolio
  Disclosure › 2026. Then we match the title "Monthly Portfolio Disclosure - September, 2026" (the
  comma is optional).

**What broke**

- Near the end of most sheets, after "Margin placed with CCIL & ARCL" and "Net Receivables /
  (Payables)", a second "Net Receivables / (Payables)" line equals the sum of those two. It is a
  subtotal printed like a holding, and counting it makes % of NAV look wrong.
  - We drop it only when it repeats the line above *and* equals the sum of the run of cash lines
    with no ISIN above it.
  - July's Overnight Fund (MMF12) has a single Net Receivables line, and it is kept.
  - Every drop is reported.
- Dates are printed US-style ("September 30, 2026"), so the date parser now handles both orders.
- Futures lines carry codes such as `HINIOCT26` in the ISIN column. They are resolved through the
  name.
- The tables after GRAND TOTAL (derivative disclosures) are ignored.

**What will likely break**

- A rotated key, or a bundle that declares it in a different shape. In both cases the regex stops
  matching and the run fails loudly.
- A renamed folder in the tree.
- There is **no independent scheme count**. There is no index sheet, the codes skip MMF06, and
  27 sheets are present. A missing scheme would not be caught. The next thing to add is a check
  against the scheme list on the site.

**Time:** about 40 min.

---

## Jio BlackRock

**How the files were found**

- The page `/statutory-disclosure/disclosures/monthly-portfolio-disclosure` is a Next.js App
  Router page. Its document list comes from a React *server action*: a `POST` to the page URL with
  a `Next-Action: <id>` header and the body
  `["monthly-portfolio-disclosure", {"year": "FI2026-2027", "month": "September"}, "MF"]`.
- The reply is React Flight text, and its line `1:` is JSON with titles and file URLs.
- The action id is a hash generated at build time. It is the "changing identifier" from the
  brief. On each run we read it from the page's own JS chunk, where the client stub is declared as
  `createServerReference("<id>", …, "getDisclosureL3Data")`.
- Plain HTTP works. A headless browser actually renders this page as "Oops! Something went wrong".

**What broke**

- Each month has one workbook per scheme, plus an all-schemes workbook with an `Index` sheet.
- In **July, the all-schemes workbook was published under the Money Market Fund's title**, so the
  Money Market Fund exists only inside it.
  - We parse every listed file and keep one copy of each scheme. If two copies of a scheme differ,
    the run fails.
  - The scheme count comes from the Index sheet.
- Futures are listed in a second table *after* the Grand Total, which excludes them. We add them
  as derivatives.
- The "Money Market Instruments" heading in Arbitrage carries the previous section's total in its
  value columns. Read naively, it becomes a ₹413 crore phantom holding. We blank values on known
  headings and report it.
- Some futures names are abbreviated past recognition, for example "PGC LTD FUT" (Power Grid),
  "CROMPT GREA CON ELEC" and "TCS LTD". Some positions are also split across two expiries. We
  group futures by name without the expiry, add up their quantities, and match an equity line with
  the same share count and the same industry or a shared word. The match must be unique. After
  that, every line has an identifier.
- Scheme names carry a "$" footnote marker, which we strip. The funds were also renamed between
  July and August, for example Low Duration became Ultra Short to Short Term.

**What will likely break**

- The server-action name `getDisclosureL3Data`, or the argument order.
- The Flight reply format if Next.js changes it.
- A new heading that carries values.

**Time:** about 45 min.

---

## Bandhan

**How the files were found**

The menu path is Statutory Disclosures › Disclosure of Detailed Scheme Portfolios › "Monthly and
Half Yearly" (`/statutory-disclosures/scheme-portfolios/monthly-half-yearly`). The page asks for a
year, a month and a *scheme*, and lists one file at a time.

**Plain HTTP was tried first and does not work:**

1. Every URL returns the same 3 KB React shell, with no data or links.
2. The list comes from
   `POST https://pnservices.bandhanmutual.com/internal/investorservices/encdec/investor/v1/dashboard/cms-call`.
   - Both the request and the reply bodies are encrypted.
   - The code that does it is an 11 MB obfuscated bundle.
   - It does not use WebCrypto (we hooked it and saw no calls), and there is no readable key.
   - Reproducing it would mean reverse-engineering obfuscated code. That would be brittle and
     arguably against the spirit of "no bypassing".
3. The WordPress CMS behind it (`cmsnew.bandhanmutual.com`) is locked down:
   - its REST API answers 401 "Only authenticated users can access the REST API";
   - `/feed/` answers 403;
   - there is no sitemap;
   - its post pages have no file links.

**So, a headless browser, for the list only:**

- We open the page in headless Chromium and let the site's own code make that call.
- An init script changes the request object *before the site encrypts it*. It keeps the same type
  and sub-category, sets our year and month, removes the scheme filter, and sets the page size to
  500. The plaintext looks like
  `{"type":"SCHEME_PORTFOLIOS","data":{"subcategory":"monthly-and-half-yearly","financial_year":"2026","month":"September","posts_per_page":500}}`.
- The script reads the reply after the site decrypts it.
- The browser makes one page visit per month. It blocks third-party trackers, images and fonts,
  and allows only `*.bandhanmutual.com`.
- The decrypted list is cached, so `--offline` and the tests need no browser.
- The workbooks themselves are plain HTTP downloads from Google Cloud Storage, using the links the
  site returns.
- We fail if `total_posts` exceeds the posts returned, or if the reply is for a different month.

**Judgement call: the request is widened.** We change the filter the site's own code sends: we
send no scheme filter where the UI sends one. The alternative is driving the scheme dropdown 77
times a month. That is slower, makes more requests to the site, and is more fragile. Neither
involves a login, a CAPTCHA, or touching the encryption. If this reads as bypassing, the fallback
is to iterate the dropdown.

**What broke**

- % to NAV is printed as a fraction, and lines under 0.01% print "$" instead of a number. We
  compute those from market value ÷ NAV and report it.
- "Net Current Assets" is printed after "Cash Margin – Derivatives", "Cash / Bank Balance" and
  "Net Receivables/Payables", and it equals their sum. We drop it when it does, and report it.
  When it stands alone (fund of funds), it is kept.
- Two interest-rate swaps can have identical figures and differ only in their code column, for
  example `IRS1257FL` and `IRS1258FL`. The first version of the duplicate-line check dropped one
  of them. The check now compares whole rows, apart from the serial number. This trap was caught
  here, and the fix also protects the other AMCs.
- Physical gold and silver bars carry made-up codes in the ISIN column (`DBXXXGOLD100`). We blank
  them and report them as physical commodities.
- Overseas ETFs and funds (iShares MSCI ACWI, the JPMorgan funds) have only internal codes
  (`14137311USD`), not ISINs. They are reported under `security_lines_without_identifier`, together
  with the swap legs.
- Written call options (Equity Savings) are named like "Titan Company Limited 5000 Call October
  2026 Option". We resolve them to the underlying.
- The "Derivative Disclosure" tables after the Grand Total have names shifted against their
  contract codes in some files. For example, `INDIGO_29/09/2026` is labelled "IndusInd Bank". We
  read derivatives only from the statement itself.

**What will likely break**

- Anything in the site's request object (`type`, `subcategory`, field names), or a server-side cap
  on `posts_per_page`.
- The JSON-hook approach, if the app stops using `JSON.stringify` / `JSON.parse` around its
  encryption.
- The `financial_year` field is the calendar year today, but could become the Indian financial
  year.

**Time:** about 1 h.

---

## Things that apply to every AMC

- **Tickers.** We use the NSE equity, SME and ETF lists, then the latest BSE bhavcopy (we walk
  back up to 10 days, because BSE serves its homepage with status 200 when a day has no file).
  These are current lists, so a security that was delisted or renamed since the month in question
  may get no ticker.
- **Hedged-futures matching** is the riskiest heuristic in the tool. It requires the same total
  share count, plus either a shared distinctive word or the same industry, and the match must be
  unique. A wrong match would still be internally consistent, so the line is not flagged.
- **Sub Total mismatches** are warnings, not errors, because some AMC files are themselves
  inconsistent (Quant). The Grand Total check is the hard gate.
