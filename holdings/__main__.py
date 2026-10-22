"""Command line: python -m holdings 2026-09 --amc quant"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from . import dashboard
from .amcs import REGISTRY
from .fetch import Fetcher
from .identifiers import TickerMap
from .model import HoldingsError
from .pipeline import run


def parse_month(s: str) -> date:
    try:
        y, m = s.split("-")
        return date(int(y), int(m), 1)
    except ValueError:
        raise argparse.ArgumentTypeError(f"month must look like 2026-09, got {s!r}") from None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m holdings", description=__doc__)
    p.add_argument("months", nargs="+", type=parse_month, help="one or more months, e.g. 2026-09")
    p.add_argument("--amc", action="append", choices=sorted(REGISTRY) + ["all"], default=None,
                   help="AMC key (repeatable); default all")
    p.add_argument("--out", type=Path, default=Path("out"))
    p.add_argument("--cache", type=Path, default=Path(".cache"))
    p.add_argument("--offline", action="store_true", help="use only cached downloads")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    keys = list(REGISTRY) if not args.amc or "all" in args.amc else args.amc
    fetcher = Fetcher(args.cache, offline=args.offline)
    tickers = TickerMap.load(fetcher)

    failures = 0
    for month in args.months:
        for key in keys:
            amc = REGISTRY[key](fetcher)
            try:
                rep = run(amc, month, args.out, tickers)
                print(f"OK   {key:<10} {month:%Y-%m}  {rep['scheme_count']} schemes, {rep['holding_count']} lines, "
                      f"{len(rep['schemes_outside_97_103'])} outside 97-103, "
                      f"{len(rep['security_lines_without_identifier'])} without identifier")
            except HoldingsError as e:
                failures += 1
                print(f"FAIL {key:<10} {month:%Y-%m}  {e}", file=sys.stderr)
            except Exception as e:  # network errors etc.: still report per AMC and carry on
                failures += 1
                print(f"FAIL {key:<10} {month:%Y-%m}  {type(e).__name__}: {e}", file=sys.stderr)
    args.out.mkdir(parents=True, exist_ok=True)
    print(f"dashboard: {dashboard.build(args.out)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
