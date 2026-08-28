"""MarketCheck adapter — dealer + private-party inventory via a documented JSON API.

MarketCheck (``api.marketcheck.com``) aggregates active US listings (dealer, FSBO,
auction) with make/model/year/price/mileage/zip/radius filters and returns a real
dealer ``vdp_url`` per car — no Cloudflare, no FlareSolverr. Auth is an ``api_key``
query param.
"""

from __future__ import annotations

import httpx

from ..config import Settings
from ..models import Listing, SearchFilters
from .base import Adapter
from .html_common import vehicle_details

SEARCH = "https://api.marketcheck.com/v2/search/car/active"

_SORT = {
    "price_asc": ("price", "asc"),
    "price_desc": ("price", "desc"),
    "year_desc": ("year", "desc"),
    "year_asc": ("year", "asc"),
    "mileage_asc": ("miles", "asc"),
}


def _int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.replace(",", "")))
        except ValueError:
            return None
    return None


class MarketCheckAdapter(Adapter):
    id = "marketcheck"
    label = "MarketCheck"
    region = "US"
    note = "Dealer + private-party via api.marketcheck.com (set CARSEARCH_MARKETCHECK_API_KEY)."

    def available(self, settings: Settings) -> bool:
        return settings.enable_marketcheck and bool(settings.marketcheck_api_key)

    def _params(self, f: SearchFilters, settings: Settings) -> dict[str, str]:
        params: dict[str, str] = {
            "api_key": settings.marketcheck_api_key,
            "car_type": "used",
            "zip": f.zip or settings.default_zip,
            # MarketCheck plans cap the radius (100 mi on the standard tier) and
            # 422 above it — clamp so a wider watchlist radius still works.
            "radius": str(min(f.radius or 100, 100)),
            "rows": str(min(f.limit, settings.marketcheck_max_results, 50)),
            "start": "0",
        }
        if f.make:
            params["make"] = f.make
        if f.model:
            params["model"] = f.model
        if f.min_year is not None or f.max_year is not None:
            params["year_range"] = f"{f.min_year or 1900}-{f.max_year or 2100}"
        if f.min_price is not None or f.max_price is not None:
            params["price_range"] = f"{f.min_price or 0}-{f.max_price or 100_000_000}"
        if f.max_mileage is not None:
            params["miles_range"] = f"0-{f.max_mileage}"
        if f.sort in _SORT:
            params["sort_by"], params["sort_order"] = _SORT[f.sort]
        return params

    def _to_listing(self, row: dict, filters: SearchFilters) -> Listing | None:
        if not isinstance(row, dict):
            return None
        build = row.get("build") or {}
        dealer = row.get("dealer") or {}
        media = row.get("media") or {}
        vin = row.get("vin") or row.get("id")
        year = _int(build.get("year"))
        make = build.get("make")
        model = build.get("model")
        trim = build.get("trim")
        title = " ".join(str(x) for x in (year, make, model, trim) if x) or vin or "MarketCheck listing"
        url = row.get("vdp_url") or (f"https://www.google.com/search?q={vin}" if vin else "")
        if not url:
            return None
        photos = media.get("photo_links") or []
        image = photos[0] if isinstance(photos, list) and photos else None
        city, state = dealer.get("city"), dealer.get("state")
        location = ", ".join(str(x) for x in (city, state) if x) or None
        transmission, highlights = vehicle_details(
            " ".join(str(x) for x in (title, build.get("drivetrain"), build.get("transmission")) if x),
            filters.keywords,
        )
        return Listing(
            id=f"{self.id}:{vin}", site=self.id, site_label=self.label,
            title=title, price=_int(row.get("price")), year=year, make=make, model=model,
            mileage=_int(row.get("miles")), location=location, url=url, image_url=image,
            dealer=dealer.get("name"), transmission=transmission, highlights=highlights,
        )

    async def _fetch(self, client: httpx.AsyncClient, params: dict) -> list:
        resp = await client.get(SEARCH, params=params, timeout=30)
        resp.raise_for_status()
        return (resp.json() or {}).get("listings") or []

    async def search(
        self, filters: SearchFilters, client: httpx.AsyncClient, settings: Settings
    ) -> list[Listing]:
        params = self._params(filters, settings)
        rows = await self._fetch(client, params)
        # MarketCheck matches model exactly, so a generic model ("Allroad")
        # returns nothing — retry make-only and let the token post-filter keep it.
        if not rows and filters.model and "make" in params:
            params.pop("model", None)
            rows = await self._fetch(client, params)
        out: list[Listing] = []
        seen: set[str] = set()
        for row in rows:
            listing = self._to_listing(row, filters)
            if not listing or listing.id in seen:
                continue
            seen.add(listing.id)
            out.append(listing)
            if len(out) >= settings.marketcheck_max_results:
                break
        return out
