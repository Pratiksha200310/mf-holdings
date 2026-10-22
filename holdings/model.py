"""Shared data types: one holding row, one parsed scheme, and the errors every AMC raises."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# Output columns, in the exact order the brief asks for.
COLUMNS = [
    "amc",
    "scheme_name",
    "month_end",
    "instrument_name",
    "isin",
    "ticker",
    "industry_or_rating",
    "asset_type",
    "quantity",
    "market_value_lakhs",
    "pct_of_nav",
]

ASSET_TYPES = {"equity", "debt", "derivative", "cash", "other"}


@dataclass
class Holding:
    instrument_name: str
    isin: str = ""
    ticker: str = ""
    industry_or_rating: str = ""
    asset_type: str = "other"
    quantity: float | None = None
    market_value_lakhs: float | None = None
    pct_of_nav: float | None = None

    def __post_init__(self) -> None:
        if self.asset_type not in ASSET_TYPES:
            raise ValueError(f"bad asset_type {self.asset_type!r} for {self.instrument_name!r}")


@dataclass
class Scheme:
    name: str
    month_end: date
    holdings: list[Holding] = field(default_factory=list)
    # Where this scheme came from (file name / sheet name), for error messages and the report.
    source: str = ""
    # Whether the AMC's own Grand Total leaves derivative lines out. Some files net short futures
    # into NAV, others list them as exposure only; the % of NAV check follows the file's convention.
    nav_excludes_derivatives: bool = False
    # Problems in the source file that we did not "fix" silently, surfaced in the report.
    warnings: list[str] = field(default_factory=list)


class HoldingsError(Exception):
    """Base class for errors the CLI reports as a clean failure."""


class NotPublishedError(HoldingsError):
    """The AMC has not (yet) published files for the requested month."""


class LayoutError(HoldingsError):
    """A file or listing does not look the way the parser expects; refuse to guess."""
