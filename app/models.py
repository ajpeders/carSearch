from typing import Literal, Optional

from pydantic import BaseModel, Field

CarSort = Literal[
    "relevance",
    "price_asc",
    "price_desc",
    "year_desc",
    "year_asc",
    "mileage_asc",
]


class SearchFilters(BaseModel):
    """Search criteria fanned out to every selected site."""

    make: Optional[str] = None
    model: Optional[str] = None
    keywords: Optional[str] = None
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    min_year: Optional[int] = None
    max_year: Optional[int] = None
    max_mileage: Optional[int] = None
    transmission: Optional[str] = None   # "manual" | "automatic" (hard filter when known)
    zip: Optional[str] = None
    radius: Optional[int] = None
    sort: CarSort = "price_asc"
    # Which site ids to query. Empty/None means "every available site".
    sites: Optional[list[str]] = None
    limit: int = Field(default=50, ge=1, le=200)

    def query_text(self) -> str:
        """A free-text query built from make/model/keywords."""
        parts = [self.make, self.model, self.keywords]
        return " ".join(p.strip() for p in parts if p and p.strip()).strip()


class Listing(BaseModel):
    id: str
    site: str
    site_label: str
    title: str
    price: Optional[int] = None
    year: Optional[int] = None
    make: Optional[str] = None
    model: Optional[str] = None
    mileage: Optional[int] = None
    location: Optional[str] = None
    url: str
    image_url: Optional[str] = None
    dealer: Optional[str] = None
    transmission: Optional[str] = None
    highlights: list[str] = Field(default_factory=list)
    # CIS market-price enrichment (optional; populated when valuation is enabled).
    market_price: Optional[int] = None          # median asking price of comparables
    price_delta: Optional[int] = None           # this listing's price minus market_price
    deal_rating: Optional[str] = None           # great | good | fair | high
    # Watchlist lifecycle (stamped by the store when a saved search runs).
    first_seen: Optional[float] = None          # epoch seconds of first sighting
    last_seen: Optional[float] = None           # epoch seconds of latest sighting
    stale: bool = False                         # missing from the latest run ("possibly sold")


class SiteResult(BaseModel):
    site: str
    label: str
    count: int = 0
    error: Optional[str] = None


class SearchResponse(BaseModel):
    listings: list[Listing]
    sites: list[SiteResult]
    took_ms: int


class SiteInfo(BaseModel):
    id: str
    label: str
    region: str
    available: bool
    note: Optional[str] = None


class SavedSearch(BaseModel):
    """A named car search in the watchlist — lets the service track more than
    one car at a time (e.g. a VW Alltrack AND an Audi Allroad)."""

    id: str
    name: str
    filters: SearchFilters


class SavedSearchInput(BaseModel):
    """Body for creating/updating a saved search."""

    name: str
    filters: SearchFilters


class SavedSearchResult(BaseModel):
    """A saved search paired with its latest run's results."""

    id: str
    name: str
    filters: SearchFilters
    result: SearchResponse
    added: list[str] = []      # listing ids newly seen this run
    removed: list[str] = []    # listing ids gone since last run


class Location(BaseModel):
    """'My location' — applied to searches that don't set their own zip so
    everything filters by distance from here."""

    zip: str = ""
    radius: int = 100
