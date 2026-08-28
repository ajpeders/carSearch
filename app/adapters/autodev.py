"""Auto.dev Vehicle Listings adapter — dealer inventory via a proper JSON API.

Auto.dev (``api.auto.dev``) aggregates US dealer inventory with make/model/year/
price/zip/distance filters and returns structured JSON — no Cloudflare, no
FlareSolverr. Bearer-auth with an API key; free tier is ~1,000 calls/mo.

Only vin/year/make/model/price are pinned by the docs; the exact nested paths for
mileage, dealer, city/state, photos, and the click-through URL are read
defensively (several candidate paths, graceful fallback) and confirmed against a
live response before we trust them.
"""

from __future__ import annotations

import re

import httpx

from ..config import Settings
from ..models import Listing, SearchFilters
from .base import Adapter
from .html_common import vehicle_details

BASE = "https://api.auto.dev/listings"

# Auto.dev sort keys are "<field>.<dir>".
_SORT = {
    "price_asc": "retailListing.price.asc",
    "price_desc": "retailListing.price.desc",
    "year_desc": "vehicle.year.desc",
    "year_asc": "vehicle.year.asc",
    "mileage_asc": "retailListing.miles.asc",
}


def _dig(obj: dict, *paths: str):
    """First present value among dot-paths; supports numeric list indices."""
    for path in paths:
        cur = obj
        ok = True
        for key in path.split("."):
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            elif isinstance(cur, list) and key.isdigit() and int(key) < len(cur):
                cur = cur[int(key)]
            else:
                ok = False
                break
        if ok and cur not in (None, "", []):
            return cur
    return None


def _int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        m = re.search(r"\d[\d,]*", value)
        if m:
            return int(m.group().replace(",", ""))
    return None


class AutoDevAdapter(Adapter):
    id = "autodev"
    label = "Auto.dev"
    region = "US"
    note = "Real dealer inventory via api.auto.dev (set CARSEARCH_AUTODEV_API_KEY)."

    def available(self, settings: Settings) -> bool:
        return settings.enable_autodev and bool(settings.autodev_api_key)

    def _params(self, f: SearchFilters, settings: Settings) -> dict[str, str]:
        params: dict[str, str] = {
            "limit": str(min(f.limit, settings.autodev_max_results)),
            "zip": f.zip or settings.default_zip,
            "distance": str(f.radius or 100),
        }
        if f.make:
            params["vehicle.make"] = f.make
        if f.model:
            params["vehicle.model"] = f.model
        if f.min_year is not None or f.max_year is not None:
            params["vehicle.year"] = f"{f.min_year or 1990}-{f.max_year or 2100}"
        if f.min_price is not None or f.max_price is not None:
            params["retailListing.price"] = f"{f.min_price or 0}-{f.max_price or 100_000_000}"
        if f.max_mileage is not None:
            params["retailListing.miles"] = f"0-{f.max_mileage}"
        if f.sort in _SORT:
            params["sort"] = _SORT[f.sort]
        return params

    def _to_listing(self, row: dict, filters: SearchFilters) -> Listing | None:
        if not isinstance(row, dict):
            return None
        vin = _dig(row, "vin", "vehicle.vin")
        year = _int(_dig(row, "vehicle.year", "year"))
        make = _dig(row, "vehicle.make", "make")
        model = _dig(row, "vehicle.model", "model")
        trim = _dig(row, "vehicle.trim", "trim")
        price = _int(_dig(row, "retailListing.price", "price"))
        mileage = _int(_dig(row, "retailListing.miles", "retailListing.mileage", "miles"))
        city = _dig(row, "retailListing.city", "city")
        state = _dig(row, "retailListing.state", "state")
        location = ", ".join(str(x) for x in (city, state) if x) or None
        # retailListing.dealer is a plain string ("Holman Honda").
        dealer = _dig(row, "retailListing.dealer", "retailListing.dealerName", "dealer.name")
        image = _dig(row, "retailListing.primaryImage", "retailListing.primaryPhotoUrl",
                     "retailListing.photoUrls.0", "primaryImage")
        title = " ".join(str(x) for x in (year, make, model, trim) if x) or vin or "Auto.dev listing"

        ident = vin or title
        if not ident:
            return None
        # Auto.dev has no dealer VDP link (retailListing.vdp is an internal fragment),
        # so click through via a VIN web-search that surfaces the live listing.
        url = _dig(row, "retailListing.vdpUrl", "url")
        if not (isinstance(url, str) and url.startswith("http")):
            url = f"https://www.google.com/search?q={vin}" if vin else "https://www.auto.dev/"

        specs = row.get("vehicle") or {}
        blob = " ".join(str(x) for x in (
            title, specs.get("drivetrain"), specs.get("transmission"),
            specs.get("engine"), specs.get("exteriorColor"), specs.get("fuel"),
        ) if x)
        transmission, highlights = vehicle_details(blob, filters.keywords)
        return Listing(
            id=f"{self.id}:{ident}", site=self.id, site_label=self.label,
            title=title, price=price, year=year, make=make, model=model,
            mileage=mileage, location=location, url=url, image_url=image,
            dealer=dealer, transmission=transmission, highlights=highlights,
        )

    async def _fetch(self, client: httpx.AsyncClient, params: dict, settings: Settings) -> list:
        resp = await client.get(
            BASE, params=params,
            headers={"Authorization": f"Bearer {settings.autodev_api_key}", "Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return (data.get("data") if isinstance(data, dict) else data) or []

    async def search(
        self, filters: SearchFilters, client: httpx.AsyncClient, settings: Settings
    ) -> list[Listing]:
        params = self._params(filters, settings)
        rows = await self._fetch(client, params, settings)
        # Auto.dev matches model exactly ("A4 allroad"), so a generic model
        # ("Allroad") returns nothing — retry make-only and let the aggregator's
        # token post-filter keep the right cars.
        if not rows and filters.model and "vehicle.make" in params:
            params.pop("vehicle.model", None)
            rows = await self._fetch(client, params, settings)
        out: list[Listing] = []
        seen: set[str] = set()
        for row in rows or []:
            listing = self._to_listing(row, filters)
            if not listing or listing.id in seen:
                continue
            # Client-side backstop in case the server miles filter is loose.
            if filters.max_mileage is not None and listing.mileage and listing.mileage > filters.max_mileage:
                continue
            seen.add(listing.id)
            out.append(listing)
            if len(out) >= settings.autodev_max_results:
                break
        return out
