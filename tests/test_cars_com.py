"""Offline tests for the cars.com adapter's DOM parser.

No network: ``_fetch`` is stubbed to return a small saved HTML fixture and the
LLM refiner is disabled (empty ``llm_base_url``), so parsing is exercised
end-to-end through ``search()`` with only the deterministic code path.
"""

from pathlib import Path

import pytest

from app.adapters.cars_com import CarsComAdapter, _city_state
from app.config import Settings
from app.models import SearchFilters

FIXTURE = (Path(__file__).parent / "fixtures" / "cars_com_results.html").read_text()


def _settings() -> Settings:
    # llm_base_url="" keeps llm_make_model offline (returns {} immediately).
    return Settings(
        enable_cars_com=True, flaresolverr_url="http://flare/v1",
        llm_base_url="", cars_com_max_results=25, default_zip="80022",
    )


def _adapter(html: str = FIXTURE) -> CarsComAdapter:
    ad = CarsComAdapter()

    async def fake_fetch(url, client, settings):   # bound as instance attr → no self
        return html

    ad._fetch = fake_fetch
    return ad


async def _run(html=FIXTURE, **filter_kw):
    ad = _adapter(html)
    return await ad.search(SearchFilters(**filter_kw), client=None, settings=_settings())


@pytest.mark.asyncio
async def test_extracts_core_fields_and_dedupes_and_skips_junk():
    results = await _run(make="Volkswagen")
    by_id = {r.id: r for r in results}
    # 5 real cards; the duplicate anchor + the two non-listing links are dropped.
    assert set(by_id) == {"card-1", "card-2", "card-3", "card-4", "card-5"}

    c1 = by_id["card-1"]
    assert c1.title == "2017 Volkswagen Golf Alltrack TSI SE"   # "Used " prefix stripped
    assert c1.price == 18424
    assert c1.mileage == 70285
    assert c1.year == 2017
    assert c1.make == "Volkswagen"
    assert c1.image_url == "https://img.example/1.jpg"
    assert "card-1" in c1.url and c1.url.startswith("https://www.cars.com/")


@pytest.mark.asyncio
async def test_location_never_swallows_dealer_or_filler():
    loc = {r.id: r.location for r in await _run(make="Volkswagen")}
    assert loc["card-1"] == "Billings, MT"          # dealer "Underriner Hyundai" excluded
    assert loc["card-2"] == "Colorado Springs, CO"  # genuine two-word city preserved
    assert loc["card-3"] == "Santa Fe, NM"
    assert loc["card-4"] == "Northlake, IL"          # "Delivery from " filler stripped
    assert loc["card-5"] is None                     # no location node → null, no crash


@pytest.mark.asyncio
async def test_missing_price_and_mileage_are_null():
    c3 = next(r for r in await _run(make="Volkswagen") if r.id == "card-3")
    assert c3.price is None
    assert c3.mileage is None
    assert c3.year == 2018            # still parsed from the title
    assert c3.image_url is None


@pytest.mark.asyncio
async def test_empty_page_returns_no_listings():
    assert await _run(html="<html><body>nothing here</body></html>", make="Volkswagen") == []


@pytest.mark.asyncio
async def test_cloudflare_challenge_surfaces_as_error():
    blocked = "<html><head><title>Just a moment...</title></head><body>cf-chl</body></html>"
    with pytest.raises(RuntimeError):
        await _run(html=blocked, make="Volkswagen")


@pytest.mark.asyncio
async def test_model_keyword_filter():
    # "Jetta" is in no fixture title → everything filtered out.
    assert await _run(make="Volkswagen", model="Jetta") == []
    # "Alltrack" is in every title → all five kept.
    assert len(await _run(make="Volkswagen", model="Alltrack")) == 5


@pytest.mark.asyncio
async def test_numeric_mileage_filter_applied_client_side():
    ids = {r.id for r in await _run(make="Volkswagen", max_mileage=50000)}
    assert "card-2" in ids        # 47,070 mi kept
    assert "card-1" not in ids    # 70,285 mi dropped
    assert "card-3" in ids        # no mileage → not dropped by a mileage cap


@pytest.mark.asyncio
async def test_radius_filters_out_of_range_cards():
    # cars.com returns nationwide inventory for rare cars; the "(N mi)" badge is
    # used to drop anything beyond the requested radius. Fixture distances:
    # card-1 449mi, card-2 72mi, card-3 210mi, card-4 620mi, card-5 none.
    ids = {r.id for r in await _run(make="Volkswagen", radius=100)}
    assert ids == {"card-2", "card-5"}   # 72mi + unknown-distance kept; 449/210/620 dropped
    assert len(await _run(make="Volkswagen")) == 5   # no radius → nothing dropped


def test_city_state_helper():
    assert _city_state("Billings, MT (449 mi)") == "Billings, MT"
    assert _city_state("Colorado Springs, CO (72 mi)") == "Colorado Springs, CO"
    assert _city_state("Salt Lake City, UT (5 mi)") == "Salt Lake City, UT"  # 3-word city
    assert _city_state("Somewhere, ZZ (5 mi)") is None   # ZZ is not a real state
    assert _city_state("Golf Alltrack TSI SE") is None   # no "City, ST" at all
