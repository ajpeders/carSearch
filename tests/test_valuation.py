"""Tests for CIS market-price enrichment (offline; CIS mocked with respx)."""

import httpx
import pytest
import respx

from app.config import Settings
from app.models import Listing, SearchFilters
from app.valuation import CISClient, MarketReference, _rating

BASE = "https://cis-automotive.p.rapidapi.com"


def _settings(**kw) -> Settings:
    base = dict(enable_cis_valuation=True, cis_api_key="k", cis_min_sample=3, default_zip="80022")
    base.update(kw)
    return Settings(**base)


def _listing(id, price, year) -> Listing:
    return Listing(id=id, site="demo", site_label="Demo", title=f"{year} car",
                   price=price, year=year, url=f"https://x/{id}")


def test_rating_thresholds():
    assert _rating(0.80) == "great"
    assert _rating(0.95) == "good"
    assert _rating(1.05) == "fair"
    assert _rating(1.20) == "high"


def test_market_reference_per_year_and_overall():
    ref = MarketReference({2019: [24000, 26000, 28000], 2020: [30000]})
    med, sample = ref.median_for(2019, min_sample=3)
    assert med == 26000 and sample == 3
    # 2020 has too few samples → falls back to overall median.
    med2, _ = ref.median_for(2020, min_sample=3)
    assert med2 == ref.overall_median


@pytest.mark.asyncio
async def test_enrich_noop_without_make_model():
    # No make/model → not a comparable set → listings untouched, no CIS calls.
    client = CISClient(_settings())
    out = await client.enrich([_listing("a", 20000, 2019)], SearchFilters(make="Toyota"), client=None)
    assert out[0].deal_rating is None


@respx.mock
@pytest.mark.asyncio
async def test_enrich_stamps_deal_rating():
    respx.get(f"{BASE}/getToken").mock(return_value=httpx.Response(200, json={"token": "jwt", "expires": 999}))
    respx.get(f"{BASE}/getBrands").mock(return_value=httpx.Response(200, json={"data": ["Volkswagen", "Toyota"]}))
    respx.get(f"{BASE}/getModels").mock(
        return_value=httpx.Response(200, json={"data": [{"modelName": "Golf Alltrack"}]})
    )
    respx.get(f"{BASE}/listings2").mock(return_value=httpx.Response(200, json={"data": {"page": 1, "maxPages": 1,
        "listings": [
            {"year": 2019, "askPrice": 25000}, {"year": 2019, "askPrice": 27000},
            {"year": 2019, "askPrice": 29000}, {"year": 2019, "askPrice": 31000},
        ]}}))

    client = CISClient(_settings())
    listings = [_listing("cheap", 22000, 2019), _listing("pricey", 34000, 2019)]
    async with httpx.AsyncClient() as http:
        out = await client.enrich(listings, SearchFilters(make="Volkswagen", model="Golf Alltrack", zip="80301"), http)

    by_id = {l.id: l for l in out}
    assert by_id["cheap"].market_price == 28000            # median of 25/27/29/31k
    assert by_id["cheap"].deal_rating == "great"           # 22000/28000 = 0.79
    assert by_id["cheap"].price_delta == 22000 - 28000
    assert by_id["pricey"].deal_rating == "high"           # 34000/28000 = 1.21


@respx.mock
@pytest.mark.asyncio
async def test_enrich_fails_open_on_cis_error():
    # CIS 500s → enrichment swallows it and returns listings unchanged.
    respx.get(f"{BASE}/getToken").mock(return_value=httpx.Response(500))
    client = CISClient(_settings())
    async with httpx.AsyncClient() as http:
        out = await client.enrich([_listing("a", 20000, 2019)],
                                  SearchFilters(make="Volkswagen", model="Golf Alltrack"), http)
    assert out[0].deal_rating is None
