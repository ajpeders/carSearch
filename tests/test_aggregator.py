import asyncio

import pytest

from app.aggregator import _dedup, _sort, search
from app.config import Settings
from app.models import Listing, SearchFilters
from app.adapters.base import Adapter


def _listing(**kw) -> Listing:
    base = dict(
        id="x",
        site="s",
        site_label="S",
        title="car",
        url="https://example.invalid/x",
    )
    base.update(kw)
    return Listing(**base)


def test_dedup_by_url():
    # Distinct titles so only the shared-URL collision (a, b) is exercised.
    a = _listing(id="1", url="https://a/1", title="Car A")
    b = _listing(id="2", url="https://a/1", title="Car B")  # same url as a
    c = _listing(id="3", url="https://a/3", title="Car C")
    assert [l.id for l in _dedup([a, b, c])] == ["1", "3"]


def test_dedup_by_content():
    a = _listing(id="1", url="https://a/1", title="2018 Toyota", price=100, year=2018)
    b = _listing(id="2", url="https://b/2", title="2018 Toyota", price=100, year=2018)
    assert [l.id for l in _dedup([a, b])] == ["1"]


def test_sort_price_asc_nulls_last():
    items = [_listing(id="a", price=None), _listing(id="b", price=200), _listing(id="c", price=100)]
    assert [l.id for l in _sort(items, "price_asc")] == ["c", "b", "a"]


def test_sort_year_desc():
    items = [_listing(id="a", year=2010), _listing(id="b", year=2022), _listing(id="c", year=None)]
    assert [l.id for l in _sort(items, "year_desc")] == ["b", "a", "c"]


class _StubAdapter(Adapter):
    def __init__(self, id_, listings=None, fail=False, hang=False, ok=True):
        self.id = id_
        self.label = id_.title()
        self.region = "US"
        self._listings = listings or []
        self._fail = fail
        self._hang = hang
        self._ok = ok

    def available(self, settings):
        return self._ok

    async def search(self, filters, client, settings):
        if self._hang:
            await asyncio.sleep(5)
        if self._fail:
            raise RuntimeError("boom")
        return self._listings


@pytest.mark.asyncio
async def test_search_merges_and_reports_per_site():
    settings = Settings(per_site_timeout=0.2, max_concurrency=8)
    registry = {
        "good": _StubAdapter("good", [_listing(id="g", site="good", url="https://g/1")]),
        "bad": _StubAdapter("bad", fail=True),
        "slow": _StubAdapter("slow", hang=True),
        "off": _StubAdapter("off", ok=False),
    }
    resp = await search(SearchFilters(), client=None, settings=settings, registry=registry)

    assert len(resp.listings) == 1
    by_site = {s.site: s for s in resp.sites}
    assert by_site["good"].count == 1 and by_site["good"].error is None
    assert by_site["bad"].error == "boom"
    assert by_site["slow"].error == "timeout"
    assert by_site["off"].error == "not configured"


def test_flaresolverr_adapters_get_a_longer_timeout_than_per_site():
    # Regression: with the default 12s per-site timeout the aggregator used to
    # cancel FlareSolverr solves (up to flaresolverr_max_ms) mid-flight. The
    # scrape adapters now derive their own budget from flaresolverr_max_ms.
    from app.adapters.carmax import CarMaxAdapter
    from app.adapters.cars_com import CarsComAdapter

    settings = Settings(per_site_timeout=12, flaresolverr_max_ms=60000)
    for adapter in (CarMaxAdapter(), CarsComAdapter()):
        assert adapter.timeout(settings) >= 60, adapter.id

    # cars.com adds an LLM refine budget on top when an LLM endpoint is set.
    base = CarsComAdapter().timeout(settings)
    with_llm = CarsComAdapter().timeout(
        Settings(per_site_timeout=12, flaresolverr_max_ms=60000, llm_base_url="http://llm/v1")
    )
    assert with_llm > base

    # A plain HTTP-API adapter still uses the global per-site timeout.
    assert _StubAdapter("x").timeout(settings) == 12


@pytest.mark.asyncio
async def test_search_respects_site_selection():
    settings = Settings()
    registry = {
        "a": _StubAdapter("a", [_listing(id="a", site="a", url="https://a/1")]),
        "b": _StubAdapter("b", [_listing(id="b", site="b", url="https://b/1")]),
    }
    resp = await search(
        SearchFilters(sites=["a"]), client=None, settings=settings, registry=registry
    )
    assert {s.site for s in resp.sites} == {"a"}
    assert [l.id for l in resp.listings] == ["a"]


@pytest.mark.asyncio
async def test_relevance_interleaves_sites_before_limit():
    settings = Settings()
    registry = {
        "a": _StubAdapter("a", [
            _listing(id="a1", site="a", url="https://a/1"),
            _listing(id="a2", site="a", url="https://a/2"),
            _listing(id="a3", site="a", url="https://a/3"),
        ]),
        "b": _StubAdapter("b", [
            _listing(id="b1", site="b", url="https://b/1"),
            _listing(id="b2", site="b", url="https://b/2"),
        ]),
    }
    resp = await search(
        SearchFilters(sort="relevance", limit=3),
        client=None,
        settings=settings,
        registry=registry,
    )
    assert [l.id for l in resp.listings] == ["a1", "b1", "a2"]


@pytest.mark.asyncio
async def test_non_relevance_sort_still_merges_then_sorts_globally():
    settings = Settings()
    registry = {
        "a": _StubAdapter("a", [
            _listing(id="a1", site="a", url="https://a/1", price=300),
            _listing(id="a2", site="a", url="https://a/2", price=100),
        ]),
        "b": _StubAdapter("b", [
            _listing(id="b1", site="b", url="https://b/1", price=200),
        ]),
    }
    resp = await search(
        SearchFilters(sort="price_asc"),
        client=None,
        settings=settings,
        registry=registry,
    )
    assert [l.id for l in resp.listings] == ["a2", "b1", "a1"]


# --- post-filter: never trust a site to have applied the search criteria ---

@pytest.mark.asyncio
async def test_search_drops_off_query_listings():
    # A CarMax-style zero-result page padded with a "cars near you" carousel:
    # unrelated makes, over-mileage, over-price — all must be filtered out.
    junk = [
        _listing(id="j1", title="2015 Nissan Sentra S", make="Nissan", year=2015),
        _listing(id="j2", title="2018 Audi Q5 Premium", make="Audi", model="Q5", year=2018),
        _listing(id="j3", title="2019 VW Golf Alltrack SE", make="Volkswagen",
                 model="Golf Alltrack", year=2019, mileage=90000),
        _listing(id="j4", title="2019 VW Golf Alltrack S", make="Volkswagen",
                 model="Golf Alltrack", year=2019, price=45000),
    ]
    good = _listing(id="g1", title="2019 Volkswagen Golf Alltrack SE", make="Volkswagen",
                    model="Golf Alltrack", year=2019, price=21000, mileage=41000)
    registry = {"stub": _StubAdapter("stub", listings=junk + [good])}
    filters = SearchFilters(make="Volkswagen", model="Golf Alltrack",
                            min_year=2018, max_mileage=50000, max_price=30000)
    resp = await search(filters, client=None, settings=Settings(), registry=registry)
    assert [l.id for l in resp.listings] == ["g1"]
    assert resp.sites[0].count == 1


def test_model_requires_all_tokens_not_any():
    from app.aggregator import _matches_filters
    f = SearchFilters(make="Audi", model="A4 Allroad")
    keep = _listing(make="Audi", model="A4 allroad", title="2021 Audi A4 allroad Premium")
    sedan = _listing(make="Audi", model="A4", title="2021 Audi A4 Premium")   # no "allroad"
    a6 = _listing(make="Audi", model="A6 allroad", title="2022 Audi A6 allroad")  # no "a4"
    assert _matches_filters(keep, f)
    assert not _matches_filters(sedan, f)   # plain A4 sedan excluded
    assert not _matches_filters(a6, f)      # A6 allroad excluded from an A4 search


def test_transmission_is_a_strict_filter():
    from app.aggregator import _matches_filters
    f = SearchFilters(transmission="manual")
    assert _matches_filters(_listing(transmission="Manual"), f)          # match → keep
    assert not _matches_filters(_listing(transmission="Automatic"), f)   # mismatch → drop
    assert not _matches_filters(_listing(transmission=None), f)          # unknown → drop (strict)
    # No transmission filter → unknown transmission is fine.
    assert _matches_filters(_listing(transmission=None), SearchFilters())


@pytest.mark.asyncio
async def test_search_keeps_listings_with_unparsed_fields():
    # Missing price/year/mileage/make is not evidence of a mismatch — the model
    # token in the title is enough to keep it.
    sparse = _listing(id="s1", title="Golf Alltrack wagon, 6MT")
    registry = {"stub": _StubAdapter("stub", listings=[sparse])}
    filters = SearchFilters(make="Volkswagen", model="Golf Alltrack",
                            min_year=2018, max_price=30000)
    resp = await search(filters, client=None, settings=Settings(), registry=registry)
    assert [l.id for l in resp.listings] == ["s1"]
