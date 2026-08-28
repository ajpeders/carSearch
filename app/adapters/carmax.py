from __future__ import annotations

import re
from urllib.parse import urlencode, urljoin

import httpx

from ..config import Settings
from ..models import Listing, SearchFilters
from .base import Adapter
from .html_common import (
    BLOCKED_RE,
    YEAR_RE,
    apply_title_make_model,
    apply_llm_make_model,
    city_state,
    compact,
    fetch_with_flaresolverr,
    flaresolverr_timeout,
    first_image,
    first_match,
    nearby_card,
    parse_miles,
    parse_price,
    soup,
    to_int,
    vehicle_details,
)

BASE = "https://www.carmax.com"
CAR_RE = re.compile(r"/car/([^/?#]+)")
SORT = {
    "price_asc": "price-low-to-high",
    "price_desc": "price-high-to-low",
    "year_desc": "year-newest-to-oldest",
    "year_asc": "year-oldest-to-newest",
    "mileage_asc": "mileage-low-to-high",
}


class CarMaxAdapter(Adapter):
    id = "carmax"
    label = "CarMax"
    region = "US"
    note = "Real listings via FlareSolverr (set CARSEARCH_FLARESOLVERR_URL)."

    def available(self, settings: Settings) -> bool:
        return settings.enable_carmax and bool(settings.flaresolverr_url)

    def timeout(self, settings: Settings) -> float:
        return flaresolverr_timeout(settings, llm=bool(settings.llm_base_url))

    def _url(self, filters: SearchFilters, settings: Settings) -> str:
        params: dict[str, str] = {
            "search": filters.query_text() or "cars",
            "zip": filters.zip or settings.default_zip,
            "radius": str(filters.radius or 100),
        }
        if filters.min_price is not None or filters.max_price is not None:
            lo = filters.min_price if filters.min_price is not None else 0
            hi = filters.max_price if filters.max_price is not None else 999999
            params["price"] = f"{lo}-{hi}"
        if filters.min_year is not None or filters.max_year is not None:
            lo = filters.min_year if filters.min_year is not None else 1990
            hi = filters.max_year if filters.max_year is not None else 2040
            params["year"] = f"{lo}-{hi}"
        if filters.max_mileage is not None:
            params["mileage"] = f"0-{filters.max_mileage}"
        if filters.sort in SORT:
            params["sort"] = SORT[filters.sort]
        return f"{BASE}/cars?{urlencode(params)}"

    async def _fetch(
        self, url: str, client: httpx.AsyncClient, settings: Settings
    ) -> str:
        return await fetch_with_flaresolverr(url, client, settings)

    @staticmethod
    def _title(text: str, anchor_text: str) -> str:
        candidates = [anchor_text]
        candidates.extend(m.group(0) for m in re.finditer(r"\b(19[89]\d|20[0-4]\d)\b[^$|]{8,90}", text))
        for candidate in candidates:
            title = compact(candidate)
            if YEAR_RE.search(title):
                return title.strip(" -|")
        return compact(anchor_text) or "CarMax listing"

    def _parse(self, html: str, limit: int, filters: SearchFilters | None = None) -> list[Listing]:
        doc = soup(html)
        seen: set[str] = set()
        listings: list[Listing] = []
        for anchor in doc.select('a[href*="/car/"]'):
            href = anchor.get("href")
            if not isinstance(href, str):
                continue
            match = CAR_RE.search(href)
            if not match or match.group(1) in seen:
                continue
            seen.add(match.group(1))
            card = nearby_card(anchor)
            text = compact(card.get_text(" ", strip=True))
            title = self._title(text, compact(anchor.get_text(" ", strip=True)))
            year = to_int(first_match(YEAR_RE, title))
            price = parse_price(text)
            mileage = parse_miles(text)
            transmission, highlights = vehicle_details(text, filters.keywords if filters else None)
            listings.append(
                Listing(
                    id=f"{self.id}:{match.group(1)}",
                    site=self.id,
                    site_label=self.label,
                    title=title,
                    price=price,
                    year=year,
                    make=None,
                    model=None,
                    mileage=mileage,
                    location=city_state(text),
                    url=urljoin(BASE, href),
                    image_url=first_image(card, BASE),
                    dealer=None,
                    transmission=transmission,
                    highlights=highlights,
                )
            )
            if len(listings) >= limit:
                break
        return listings

    async def search(
        self,
        filters: SearchFilters,
        client: httpx.AsyncClient,
        settings: Settings,
    ) -> list[Listing]:
        html = await self._fetch(self._url(filters, settings), client, settings)
        listings = self._parse(html, min(filters.limit, settings.carmax_max_results), filters)
        if not listings and BLOCKED_RE.search(html[:5000]):
            raise RuntimeError("carmax blocked or challenged")
        apply_title_make_model(
            listings, make_hint=filters.make, model_hint=filters.model
        )
        # Refine make/model from titles via the local llm-router.
        await apply_llm_make_model(listings, client, settings)
        return listings
