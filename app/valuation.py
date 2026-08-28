"""CIS Automotive market-price enrichment.

We do NOT use the premium, VIN-only ``/valuation`` endpoint. Instead we call the
standard ``/listings2`` inventory search once per (make, model, area), build a
price distribution from the comparable dealer listings it returns, and rate each
of our listings against it locally. That keeps every call in the free "standard
request" tier, needs no VIN, and works for listings from any adapter.

Enrichment is best-effort: any failure leaves listings untouched (fail-open) so a
CIS outage never breaks a search.
"""

from __future__ import annotations

import logging
import statistics
import time

import httpx

from .config import Settings
from .models import Listing, SearchFilters

log = logging.getLogger(__name__)

_TOKEN_TTL = 23 * 3600  # tokens are valid 24h; refresh a little early
_CACHE_TTL = 3600       # market references are stable enough to cache an hour


def _rating(ratio: float) -> str:
    """Map price/market-median to a coarse deal grade."""
    if ratio <= 0.92:
        return "great"
    if ratio <= 0.99:
        return "good"
    if ratio <= 1.08:
        return "fair"
    return "high"


class MarketReference:
    """Per-year and overall median asking prices for a comparable set."""

    def __init__(self, prices_by_year: dict[int, list[int]]):
        self._by_year = {y: sorted(p) for y, p in prices_by_year.items() if p}
        allp = [p for ps in self._by_year.values() for p in ps]
        self.overall_median = int(statistics.median(allp)) if allp else None
        self.sample = len(allp)

    def median_for(self, year: int | None, min_sample: int) -> tuple[int | None, int]:
        """Median asking price for a year (fall back to overall), with its sample size."""
        if year is not None:
            ps = self._by_year.get(year)
            if ps and len(ps) >= min_sample:
                return int(statistics.median(ps)), len(ps)
        return self.overall_median, self.sample


class CISClient:
    """Thin CIS Automotive client with in-process caching.

    One instance lives on ``app.state`` for the process lifetime, so its token,
    brand/model vocabulary, and market references are shared across all searches.
    """

    def __init__(self, settings: Settings):
        self._s = settings
        self._token: str | None = None
        self._token_exp = 0.0
        self._brands: list[str] | None = None
        self._models: dict[str, list[str]] = {}
        self._refs: dict[tuple, tuple[float, MarketReference]] = {}

    def _headers(self) -> dict[str, str]:
        # RapidAPI routes authenticate by header; the host tells the gateway
        # which upstream API to proxy to.
        return {
            "x-rapidapi-key": self._s.cis_api_key,
            "x-rapidapi-host": self._s.cis_rapidapi_host,
        }

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict) -> dict:
        resp = await client.get(
            f"{self._s.cis_base_url.rstrip('/')}/{path}",
            params=params, headers=self._headers(), timeout=20,
        )
        resp.raise_for_status()
        return resp.json()

    async def _jwt(self, client: httpx.AsyncClient) -> str:
        now = time.monotonic()
        if self._token and now < self._token_exp:
            return self._token
        # On RapidAPI the api id is blank and the RapidAPI key doubles as apiKey.
        data = await self._get(client, "getToken", {
            "apiID": self._s.cis_api_id, "apiKey": self._s.cis_api_key,
        })
        token = data.get("token")
        if not token:
            raise RuntimeError("CIS getToken returned no token")
        self._token, self._token_exp = token, now + _TOKEN_TTL
        return token

    async def _brand_for(self, client: httpx.AsyncClient, make: str) -> str | None:
        if self._brands is None:
            jwt = await self._jwt(client)
            self._brands = (await self._get(client, "getBrands", {"jwt": jwt})).get("data") or []
        target = make.strip().lower()
        return next((b for b in self._brands if b.lower() == target), None) \
            or next((b for b in self._brands if target in b.lower()), None)

    async def _model_for(self, client: httpx.AsyncClient, brand: str, model: str) -> str | None:
        if brand not in self._models:
            jwt = await self._jwt(client)
            data = (await self._get(client, "getModels", {"jwt": jwt, "brandName": brand})).get("data") or []
            self._models[brand] = [m.get("modelName") for m in data if isinstance(m, dict) and m.get("modelName")]
        target = model.strip().lower()
        names = self._models[brand]
        return next((m for m in names if m.lower() == target), None) \
            or next((m for m in names if target in m.lower() or m.lower() in target), None)

    async def _reference(
        self, client: httpx.AsyncClient, brand: str, model: str, zip_code: int, radius: int
    ) -> MarketReference:
        key = (brand, model, zip_code, radius)
        cached = self._refs.get(key)
        if cached and time.monotonic() - cached[0] < _CACHE_TTL:
            return cached[1]
        jwt = await self._jwt(client)
        data = (await self._get(client, "listings2", {
            "jwt": jwt, "brandName": brand, "modelName": model,
            "zipCode": zip_code, "radius": radius, "newCars": "false",
        })).get("data") or {}
        prices_by_year: dict[int, list[int]] = {}
        for row in data.get("listings") or []:
            price = row.get("askPrice")
            year = row.get("year")
            if isinstance(price, (int, float)) and price > 0 and isinstance(year, (int, float)):
                prices_by_year.setdefault(int(year), []).append(int(price))
        ref = MarketReference(prices_by_year)
        self._refs[key] = (time.monotonic(), ref)
        return ref

    async def enrich(
        self, listings: list[Listing], filters: SearchFilters, client: httpx.AsyncClient
    ) -> list[Listing]:
        """Stamp market_price/price_delta/deal_rating onto listings, in place.

        No-ops (returns listings unchanged) unless enabled, keyed, and the search
        targets a specific make+model — a market reference is only meaningful for a
        comparable set."""
        s = self._s
        if not (s.enable_cis_valuation and s.cis_api_key and filters.make and filters.model):
            return listings
        try:
            brand = await self._brand_for(client, filters.make)
            model = brand and await self._model_for(client, brand, filters.model)
            if not brand or not model:
                return listings
            zip_code = int(filters.zip) if (filters.zip or "").isdigit() else int(s.default_zip)
            ref = await self._reference(client, brand, model, zip_code, filters.radius or 100)
            for l in listings:
                if l.price is None:
                    continue
                median, sample = ref.median_for(l.year, s.cis_min_sample)
                if not median or sample < s.cis_min_sample:
                    continue
                l.market_price = median
                l.price_delta = l.price - median
                l.deal_rating = _rating(l.price / median)
        except Exception as exc:  # noqa: BLE001 — enrichment must never break search
            log.warning("CIS enrichment skipped: %s", exc)
        return listings
