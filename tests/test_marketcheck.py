"""Tests for the MarketCheck adapter (offline; API mocked with respx)."""

import httpx
import pytest
import respx

from app.adapters.marketcheck import MarketCheckAdapter
from app.config import Settings
from app.models import SearchFilters

SEARCH = "https://api.marketcheck.com/v2/search/car/active"

PAYLOAD = {
    "num_found": 2,
    "listings": [
        {
            "vin": "1FTEW1E5", "price": 24995, "miles": 41000, "vdp_url": "https://dealer.example/a",
            "build": {"year": 2019, "make": "Volkswagen", "model": "Golf Alltrack",
                      "trim": "SE", "transmission": "Manual", "drivetrain": "AWD"},
            "dealer": {"name": "Denver VW", "city": "Denver", "state": "CO"},
            "media": {"photo_links": ["https://img.mc/a1.jpg", "https://img.mc/a2.jpg"]},
        },
        {
            "vin": "2FTEW1E5", "price": 26995, "miles": 33000, "vdp_url": "https://dealer.example/b",
            "build": {"year": 2018, "make": "Volkswagen", "model": "Golf Alltrack", "trim": "S"},
            "dealer": {"name": "Boulder VW", "city": "Boulder", "state": "CO"},
        },
    ],
}


def _settings() -> Settings:
    return Settings(enable_marketcheck=True, marketcheck_api_key="k",
                    marketcheck_max_results=50, default_zip="80022")


def test_availability_requires_flag_and_key():
    assert MarketCheckAdapter().available(Settings(enable_marketcheck=True, marketcheck_api_key="k"))
    assert not MarketCheckAdapter().available(Settings(enable_marketcheck=True))
    assert not MarketCheckAdapter().available(Settings(enable_marketcheck=False, marketcheck_api_key="k"))


def test_params_map_filters_and_ranges():
    p = MarketCheckAdapter()._params(
        SearchFilters(make="Volkswagen", model="Golf Alltrack", min_year=2018, max_year=2019,
                      max_price=30000, max_mileage=50000, zip="80301", radius=200, sort="price_asc"),
        _settings(),
    )
    assert p["api_key"] == "k" and p["car_type"] == "used"
    assert p["make"] == "Volkswagen" and p["model"] == "Golf Alltrack"
    assert p["year_range"] == "2018-2019"
    assert p["price_range"] == "0-30000"
    assert p["miles_range"] == "0-50000"
    assert p["zip"] == "80301"
    assert p["radius"] == "100"   # clamped from 200 to the plan's 100-mi cap
    assert p["sort_by"] == "price" and p["sort_order"] == "asc"
    assert int(p["rows"]) <= 50


@respx.mock
@pytest.mark.asyncio
async def test_search_parses_listings():
    respx.get(SEARCH).mock(return_value=httpx.Response(200, json=PAYLOAD))
    async with httpx.AsyncClient() as client:
        results = await MarketCheckAdapter().search(
            SearchFilters(make="Volkswagen", model="Golf Alltrack"), client, _settings())
    assert [r.id for r in results] == ["marketcheck:1FTEW1E5", "marketcheck:2FTEW1E5"]
    a = results[0]
    assert a.title == "2019 Volkswagen Golf Alltrack SE"
    assert a.price == 24995 and a.mileage == 41000 and a.year == 2019
    assert a.make == "Volkswagen" and a.model == "Golf Alltrack"
    assert a.location == "Denver, CO" and a.dealer == "Denver VW"
    assert a.image_url == "https://img.mc/a1.jpg"
    assert a.url == "https://dealer.example/a"        # real dealer VDP link
    assert a.transmission == "Manual" and "AWD" in a.highlights


@respx.mock
@pytest.mark.asyncio
async def test_make_only_fallback_when_exact_model_empty():
    def handler(request):
        if "model=" in str(request.url):
            return httpx.Response(200, json={"listings": []})
        return httpx.Response(200, json={"listings": [
            {"vin": "X", "price": 33000, "miles": 40000, "vdp_url": "https://d/x",
             "build": {"year": 2020, "make": "Audi", "model": "A6 allroad"},
             "dealer": {"name": "D", "city": "Denver", "state": "CO"}},
        ]})
    respx.get(SEARCH).mock(side_effect=handler)
    async with httpx.AsyncClient() as client:
        results = await MarketCheckAdapter().search(
            SearchFilters(make="Audi", model="Allroad"), client, _settings())
    assert [r.model for r in results] == ["A6 allroad"]


@respx.mock
@pytest.mark.asyncio
async def test_search_sends_api_key_and_used_type():
    route = respx.get(SEARCH).mock(return_value=httpx.Response(200, json={"listings": []}))
    async with httpx.AsyncClient() as client:
        await MarketCheckAdapter().search(SearchFilters(make="Toyota"), client, _settings())
    req = route.calls.last.request
    assert "api_key=k" in str(req.url) and "car_type=used" in str(req.url)
