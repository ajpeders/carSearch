import pytest

from app.adapters.demo import DemoAdapter
from app.config import Settings
from app.models import SearchFilters


@pytest.mark.asyncio
async def test_demo_filters_by_make_and_price():
    adapter = DemoAdapter()
    settings = Settings(enable_demo=True)
    filters = SearchFilters(make="Toyota", max_price=20000)
    results = await adapter.search(filters, client=None, settings=settings)
    assert results, "expected at least one demo listing"
    assert all(r.make == "Toyota" for r in results)
    assert all(r.price is None or r.price <= 20000 for r in results)


def test_demo_availability_toggle():
    adapter = DemoAdapter()
    assert adapter.available(Settings(enable_demo=True)) is True
    assert adapter.available(Settings(enable_demo=False)) is False


def test_parse_price_skips_financing_figures():
    from app.adapters.html_common import parse_price

    assert parse_price("Est. $283/mo · 2018 Audi A4 $23,998") == 23998
    assert parse_price("$1,499 down $289 /mo $19,598") == 19598
    assert parse_price("$283/mo only") is None
    assert parse_price("$1399 shipping · Jul 23 from GA $25,998*") == 25998
    assert parse_price("$24,995 · 41,000 miles") == 24995


def test_parse_miles_handles_compact_k():
    from app.adapters.html_common import parse_miles

    assert parse_miles("2018 Audi A4 Allroad · 70K mi") == 70000
    assert parse_miles("31,456 miles · Manual") == 31456
    assert parse_miles("store 15 miles away") is None
    assert parse_miles("no odometer here") is None


def test_transmission_ignores_bare_at():
    from app.adapters.html_common import transmission_from_text

    # "Only at South Denver" must not read as Automatic.
    assert transmission_from_text("Available today · Only at South Denver") is None
    assert transmission_from_text("8-speed Automatic") == "Automatic"
    assert transmission_from_text("6-speed Manual") == "Manual"
