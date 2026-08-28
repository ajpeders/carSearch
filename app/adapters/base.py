from abc import ABC, abstractmethod

import httpx

from ..config import Settings
from ..models import Listing, SearchFilters


class Adapter(ABC):
    """A single car-listing source.

    Subclass this and register it in ``build_registry`` to add a new site.
    Implementations should be resilient: raise on hard failures (the aggregator
    turns exceptions into a per-site error) and never block forever (the
    aggregator enforces a per-site timeout, but be a good citizen anyway).
    """

    id: str
    label: str
    region: str
    #: Optional human note surfaced on GET /sites (e.g. "needs API key").
    note: str | None = None

    def available(self, settings: Settings) -> bool:
        """Whether this adapter is configured enough to run."""
        return True

    def timeout(self, settings: Settings) -> float:
        """Seconds the aggregator waits for ``search()`` before cancelling.

        Defaults to the global per-site timeout, which suits fast HTTP APIs.
        Adapters with a slower backend (e.g. a FlareSolverr browser solve) should
        override this so they aren't cancelled mid-solve — see the FlareSolverr
        adapters, which derive their budget from ``flaresolverr_max_ms``.
        """
        return settings.per_site_timeout

    @abstractmethod
    async def search(
        self,
        filters: SearchFilters,
        client: httpx.AsyncClient,
        settings: Settings,
    ) -> list[Listing]:
        """Return listings matching ``filters``. May be empty."""
        raise NotImplementedError


def build_registry(settings: Settings) -> dict[str, Adapter]:
    """Instantiate every known adapter, keyed by id.

    Import adapters lazily so this module stays import-cycle free.
    """
    from .autodev import AutoDevAdapter
    from .carmax import CarMaxAdapter
    from .cars_com import CarsComAdapter
    from .demo import DemoAdapter
    from .marketcheck import MarketCheckAdapter

    adapters: list[Adapter] = [
        DemoAdapter(),
        CarsComAdapter(),
        CarMaxAdapter(),
        AutoDevAdapter(),
        MarketCheckAdapter(),
    ]
    return {adapter.id: adapter for adapter in adapters}
