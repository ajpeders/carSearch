import asyncio

import httpx
import pytest
import respx

from app.adapters.carmax import CarMaxAdapter
from app.adapters.html_common import (
    fetch_with_flaresolverr,
    flaresolverr_gate,
    flaresolverr_timeout,
)
from app.config import Settings
from app.models import SearchFilters


CARMAX_HTML = """
<html><body>
  <section class="car-tile">
    <a href="/car/26001111">2021 Subaru Outback Premium</a>
    <img data-src="https://images.example/carmax-1.jpg">
    <span>$27,998</span>
    <span>31,456 miles</span>
    <span>Manual AWD sunroof</span>
    <span>Colorado Springs, CO</span>
  </section>
  <section class="car-tile">
    <a href="/car/26002222">2019 Mazda CX-5 Touring</a>
    <span>$22,998</span>
    <span>48,900 mi</span>
    <span>Golden, CO</span>
  </section>
</body></html>
"""


def test_carmax_availability_requires_flag_and_flaresolverr():
    adapter = CarMaxAdapter()
    assert adapter.available(Settings(enable_carmax=True, flaresolverr_url="http://flare/v1"))
    assert not adapter.available(Settings(enable_carmax=False, flaresolverr_url="http://flare/v1"))
    assert not adapter.available(Settings(enable_carmax=True, flaresolverr_url=""))


def test_carmax_url_maps_common_filters():
    url = CarMaxAdapter()._url(
        SearchFilters(
            make="Subaru", model="Outback", max_price=30000, min_year=2020,
            max_mileage=50000, zip="80022", radius=100, sort="mileage_asc",
        ),
        Settings(default_zip="90210"),
    )
    assert "search=Subaru+Outback" in url
    assert "zip=80022" in url and "radius=100" in url
    assert "price=0-30000" in url
    assert "year=2020-2040" in url
    assert "mileage=0-50000" in url
    assert "sort=mileage-low-to-high" in url


def test_carmax_parser_extracts_listing_fields():
    results = CarMaxAdapter()._parse(CARMAX_HTML, limit=10)
    assert [r.id for r in results] == ["carmax:26001111", "carmax:26002222"]
    first = results[0]
    assert first.site == "carmax"
    assert first.title == "2021 Subaru Outback Premium"
    assert first.price == 27998
    assert first.year == 2021
    assert first.mileage == 31456
    assert first.location == "Colorado Springs, CO"
    assert first.url == "https://www.carmax.com/car/26001111"
    assert first.image_url == "https://images.example/carmax-1.jpg"
    assert first.transmission == "Manual"
    assert {"Manual", "AWD", "Sunroof"} <= set(first.highlights)


def test_carmax_widens_timeout_budget_with_llm():
    # Enabling the llm-router adds the make/model refine budget on top of the solve.
    base = Settings(flaresolverr_max_ms=60000)
    with_llm = Settings(flaresolverr_max_ms=60000, llm_base_url="http://llm/v1")
    assert CarMaxAdapter().timeout(with_llm) > CarMaxAdapter().timeout(base)
    assert CarMaxAdapter().timeout(base) == flaresolverr_timeout(base)


@respx.mock
@pytest.mark.asyncio
async def test_carmax_fills_make_model_via_llm_router():
    # CarMax parses no make/model from the DOM; the local llm-router fills them in.
    adapter = CarMaxAdapter()

    async def fake_fetch(url, client, settings):
        return CARMAX_HTML

    adapter._fetch = fake_fetch
    respx.post("http://llm/v1/chat/completions").mock(return_value=httpx.Response(200, json={
        "choices": [{"message": {"content":
            '[{"index":0,"make":"Subaru","model":"Outback"},'
            '{"index":1,"make":"Mazda","model":"CX-5"}]'}}]
    }))
    settings = Settings(
        enable_carmax=True, flaresolverr_url="http://flare/v1",
        llm_base_url="http://llm/v1", llm_model="qwen",
    )
    async with httpx.AsyncClient() as client:
        results = await adapter.search(SearchFilters(), client, settings)
    by_id = {r.id: r for r in results}
    assert by_id["carmax:26001111"].make == "Subaru"
    assert by_id["carmax:26001111"].model == "Outback"
    assert by_id["carmax:26002222"].make == "Mazda"


@pytest.mark.asyncio
async def test_carmax_search_surfaces_blocked_page():
    adapter = CarMaxAdapter()

    async def fake_fetch(url, client, settings):
        return "<html><body>Access Denied captcha</body></html>"

    adapter._fetch = fake_fetch
    with pytest.raises(RuntimeError, match="carmax blocked"):
        await adapter.search(SearchFilters(), client=None, settings=Settings())


@pytest.mark.asyncio
async def test_flaresolverr_gate_bounds_concurrency_across_calls():
    # Every FlareSolverr solve shares one gate (cached on the client), so no more
    # than flaresolverr_concurrency run at once even when many are launched together.
    inflight = 0
    peak = 0

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "ok", "solution": {"response": "<html>ok</html>"}}

    class _Client:
        async def post(self, *args, **kwargs):
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            await asyncio.sleep(0.02)
            inflight -= 1
            return _Resp()

    client = _Client()
    settings = Settings(flaresolverr_url="http://flare/v1", flaresolverr_concurrency=2)
    assert flaresolverr_gate(client, settings) is flaresolverr_gate(client, settings)
    await asyncio.gather(
        *(fetch_with_flaresolverr("http://example/x", client, settings) for _ in range(6))
    )
    assert peak == 2
