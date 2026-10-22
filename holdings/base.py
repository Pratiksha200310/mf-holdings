"""The interface every AMC module implements. A seventh AMC is one new file in holdings/amcs/."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .fetch import Fetcher
from .model import Scheme


@dataclass
class Document:
    """One file the AMC publishes for the month, as found by `discover`."""
    url: str
    label: str = ""


class AMC(ABC):
    key: str = ""          # short id used on the command line and in file names, e.g. "quant"
    name: str = ""         # display name for the dashboard
    homepage: str = ""

    def __init__(self, fetcher: Fetcher):
        self.fetcher = fetcher

    @abstractmethod
    def discover(self, month: date) -> list[Document]:
        """Find this month's portfolio files on the AMC site.

        `month` is the first day of the month. Must raise NotPublishedError when nothing is
        published for that month, rather than returning an empty list.
        """

    @abstractmethod
    def parse(self, path: Path, month: date) -> list[Scheme]:
        """Parse one downloaded file into schemes. Raise LayoutError on anything unexpected."""

    def expected_scheme_count(self, month: date, docs: list[Document]) -> int | None:
        """Optional independent count of schemes (from the site), used as a completeness check."""
        return None

    def fetch(self, month: date) -> tuple[list[Scheme], list[Document], int | None]:
        docs = self.discover(month)
        schemes: list[Scheme] = []
        for doc in docs:
            schemes.extend(self.parse(self.fetcher.download(doc.url), month))
        return schemes, docs, self.expected_scheme_count(month, docs)
