"""Tests for the Auto.dev listings adapter (offline; API mocked with respx).

The response fixture mirrors the documented nested shape (vehicle.*, retailListing.*).
Live field paths for mileage/dealer/photo/url are validated separately before trust.
"""

import httpx
import pytest
import respx

from app.adapters.autodev import AutoDevAdapter, _dig
from app.config import Settings
from app.models import SearchFilters

LISTINGS = "https://api.auto.dev/listings"

# Mirrors the real api.auto.dev shape (nested vehicle.* / retailListing.*; dealer
# is a string; primaryImage; retailListing.vdp is an internal fragment, not a URL).
PAYLOAD = {
    "data": [
        {
            "vin": "3TMCZ5AN0PM1",
            "location": [-104.86, 39.59],
            "vehicle": {"year": 2023, "make": "Toyota", "model": "Tacoma",
                        "trim": "TRD Off Road", "drivetrain": "4WD", "transmission": "Automatic"},
            "retailListing": {
                "price": 42998, "miles": 18500, "city": "Denver", "state": "CO",
                "dealer": "Denver Toyota", "primaryImage": "https://img.auto.dev/1.jpg",
                "vdp": "#222818089761669518",
            },
        },
        {
            "vin": "3TMCZ5AN0PM2",
            "vehicle": {"year": 2019, "make": "Toyota", "model": "Tacoma", "trim": "SR5"},
            "retailListing": {"price": 31000, "miles": 60000, "city": "Boulder", "state": "CO",
                              "dealer": "Boulder Toyota", "primaryImage": "https://img.auto.dev/2.jpg"},
        },
    ],
}


def _settings() -> Settings:
    return Settings(enable_autodev=True, autodev_api_key="k", autodev_max_results=50, default_zip="80022")


def test_availability_requires_flag_and_key():
    assert AutoDevAdapter().available(Settings(enable_autodev=True, autodev_api_key="k"))
    assert not AutoDevAdapter().available(Settings(enable_autodev=True))          # no key
    assert not AutoDevAdapter().available(Settings(enable_autodev=False, autodev_api_key="k"))


def test_dig_handles_nested_and_list_paths():
    obj = {"a": {"b": 1}, "list": [{"x": 9}]}
    assert _dig(obj, "a.b") == 1
    assert _dig(obj, "list.0.x") == 9
    assert _dig(obj, "missing", "a.b") == 1     # falls through to first present
    assert _dig(obj, "nope") is None


def test_params_map_filters_and_ranges():
    p = AutoDevAdapter()._params(
        SearchFilters(make="Toyota", model="Tacoma", min_year=2018, max_year=2024,
                      max_price=45000, zip="80301", radius=200, sort="price_asc"),
        _settings(),
    )
    assert p["vehicle.make"] == "Toyota"
    assert p["vehicle.model"] == "Tacoma"
    assert p["vehicle.year"] == "2018-2024"
    assert p["retailListing.price"] == "0-45000"
    assert p["zip"] == "80301" and p["distance"] == "200"
    assert p["sort"] == "retailListing.price.asc"


@respx.mock
@pytest.mark.asyncio
async def test_max_mileage_filters_server_and_client_side():
    p = AutoDevAdapter()._params(SearchFilters(make="Toyota", max_mileage=80000), _settings())
    assert p["retailListing.miles"] == "0-80000"      # server param
    # Client-side backstop: a high-mileage row that slips through is dropped.
    payload = {"data": [
        {"vin": "A", "vehicle": {"year": 2018, "make": "Toyota", "model": "Tacoma"},
         "retailListing": {"price": 20000, "miles": 40000}},
        {"vin": "B", "vehicle": {"year": 2015, "make": "Toyota", "model": "Tacoma"},
         "retailListing": {"price": 15000, "miles": 145000}},
    ]}
    respx.get(LISTINGS).mock(return_value=httpx.Response(200, json=payload))
    async with httpx.AsyncClient() as client:
        results = await AutoDevAdapter().search(
            SearchFilters(make="Toyota", model="Tacoma", max_mileage=80000), client, _settings())
    assert [r.id for r in results] == ["autodev:A"]   # 145k-mile B dropped


@respx.mock
@pytest.mark.asyncio
async def test_search_parses_listings():
    respx.get(LISTINGS).mock(return_value=httpx.Response(200, json=PAYLOAD))
    async with httpx.AsyncClient() as client:
        results = await AutoDevAdapter().search(
            SearchFilters(make="Toyota", model="Tacoma", zip="80022", radius=200), client, _settings()
        )
    assert [r.id for r in results] == ["autodev:3TMCZ5AN0PM1", "autodev:3TMCZ5AN0PM2"]
    first = results[0]
    assert first.title == "2023 Toyota Tacoma TRD Off Road"
    assert first.price == 42998
    assert first.mileage == 18500
    assert first.year == 2023 and first.make == "Toyota" and first.model == "Tacoma"
    assert first.location == "Denver, CO"
    assert first.dealer == "Denver Toyota"
    assert first.image_url == "https://img.auto.dev/1.jpg"
    assert "4WD" in first.highlights and first.transmission == "Automatic"
    # No dealer VDP URL (vdp is an internal fragment) → VIN web-search click-through.
    assert first.url == "https://www.google.com/search?q=3TMCZ5AN0PM1"


@respx.mock
@pytest.mark.asyncio
async def test_make_only_fallback_when_exact_model_empty():
    # Generic model ("Allroad") matches nothing exactly → retry make-only.
    def handler(request):
        if "vehicle.model" in str(request.url):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"data": [
            {"vin": "X", "vehicle": {"year": 2020, "make": "Audi", "model": "A4 allroad"},
             "retailListing": {"price": 33000, "miles": 40000, "dealer": "D",
                               "primaryImage": "https://i/x.jpg", "city": "Denver", "state": "CO"}},
        ]})
    respx.get(LISTINGS).mock(side_effect=handler)
    async with httpx.AsyncClient() as client:
        results = await AutoDevAdapter().search(
            SearchFilters(make="Audi", model="Allroad"), client, _settings())
    assert [r.model for r in results] == ["A4 allroad"]   # fallback returned the car


@respx.mock
@pytest.mark.asyncio
async def test_search_sends_bearer_auth():
    route = respx.get(LISTINGS).mock(return_value=httpx.Response(200, json={"data": []}))
    async with httpx.AsyncClient() as client:
        await AutoDevAdapter().search(SearchFilters(make="Toyota"), client, _settings())
    assert route.calls.last.request.headers["Authorization"] == "Bearer k"
