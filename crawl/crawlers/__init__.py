"""sn-mdm crawler package.

Each module under :mod:`crawl.crawlers` is one concrete
implementation of :class:`crawl.crawlers.base.BaseCrawler`, mirroring
the ``Connector`` trait surface from
``kennguy3n/knowledge``'s ``crates/connector_framework/src/connector.rs``.

The :func:`get_crawler` helper resolves a crawler by ``publisher_id``
so :mod:`crawl.pipeline` can drive the entire registry without
hardcoding imports.
"""

from __future__ import annotations

from .a16z import A16zCrawler
from .acquired import AcquiredCrawler
from .base import BaseCrawler
from .bcg import BcgCrawler
from .deutsche_bank import DeutscheBankCrawler
from .exit_five import ExitFiveCrawler
from .frog import FrogCrawler
from .imd import ImdCrawler
from .masters_of_scale import MastersOfScaleCrawler
from .mckinsey import McKinseyCrawler
from .microsoft_cyber import MicrosoftCyberCrawler
from .ncsc import NcscCrawler
from .people_matters import PeopleMattersCrawler
from .rbc_disruptors import RbcDisruptorsCrawler
from .rics import RicsCrawler
from .ted_worklife import TedWorklifeCrawler
from .thomson_reuters import ThomsonReutersCrawler
from .wef_radio_davos import WefRadioDavosCrawler

_REGISTRY: dict[str, type[BaseCrawler]] = {
    "a16z": A16zCrawler,
    "acquired": AcquiredCrawler,
    "bcg": BcgCrawler,
    "deutsche_bank": DeutscheBankCrawler,
    "exit_five": ExitFiveCrawler,
    "frog": FrogCrawler,
    "imd": ImdCrawler,
    "masters_of_scale": MastersOfScaleCrawler,
    "mckinsey": McKinseyCrawler,
    "microsoft_cyber": MicrosoftCyberCrawler,
    "ncsc": NcscCrawler,
    "people_matters": PeopleMattersCrawler,
    "rbc_disruptors": RbcDisruptorsCrawler,
    "rics": RicsCrawler,
    "ted_worklife": TedWorklifeCrawler,
    "thomson_reuters": ThomsonReutersCrawler,
    "wef_radio_davos": WefRadioDavosCrawler,
}


def get_crawler(publisher_id: str) -> type[BaseCrawler]:
    """Resolve the crawler class for a publisher id.

    Raises ``KeyError`` with a list of known ids if the lookup
    fails — this is more useful than a bare ``KeyError`` when a
    new publisher is being added to the registry.
    """
    try:
        return _REGISTRY[publisher_id]
    except KeyError as exc:
        known = ", ".join(sorted(_REGISTRY))
        raise KeyError(
            f"no crawler registered for publisher_id={publisher_id!r}; known: {known}"
        ) from exc


def known_publishers() -> list[str]:
    """Return the sorted list of publisher ids the registry knows."""
    return sorted(_REGISTRY)


__all__ = [
    "BaseCrawler",
    "get_crawler",
    "known_publishers",
]
