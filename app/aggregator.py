import asyncio
import re
import time

import httpx

from .adapters.base import Adapter
from .config import Settings
from .models import Listing, SearchFilters, SearchResponse, SiteResult


def _dedup(listings: list[Listing]) -> list[Listing]:
    """Drop obvious duplicates while preserving order.

    Two listings collide if they share a URL, or enough content to be the same
    car cross-posted to multiple sites. The content key includes mileage so two
    genuinely different cars that merely share title+price+year don't collapse,
    and it's only trusted when a price is present — a title alone (price/year/
    mileage all null) is too weak a signal to dedup on.
    """
    seen_urls: set[str] = set()
    seen_keys: set[tuple[str, int | None, int | None, int | None]] = set()
    out: list[Listing] = []
    for listing in listings:
        url_key = listing.url.strip().lower()
        if url_key and url_key in seen_urls:
            continue
        content_key = (
            listing.title.strip().lower(), listing.price, listing.year, listing.mileage,
        )
        if listing.price is not None and content_key in seen_keys:
            continue
        if url_key:
            seen_urls.add(url_key)
        if listing.price is not None:
            seen_keys.add(content_key)
        out.append(listing)
    return out


def _norm_words(text: str) -> set[str]:
    return set(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def _tokens_match(want: str, hay: str) -> bool:
    """All significant tokens of ``want`` must appear in ``hay``.

    So model "A4 Allroad" matches "Audi A4 allroad Premium" but NOT a plain "A4"
    sedan (missing "allroad") or an "A6 allroad" (missing "a4"). Using `all`
    rather than `any` is what keeps sedans out of an "<X> Allroad" search.
    """
    hay_words = _norm_words(hay)
    tokens = [t for t in _norm_words(want) if len(t) >= 2]
    return not tokens or all(t in hay_words for t in tokens)


def _name_ok(listing: Listing, f: SearchFilters) -> bool:
    """Does the listing plausibly match the requested make/model?

    Structured fields (adapter/LLM-filled) are checked individually. When a
    field is missing, fall back to the title — but jointly, since titles often
    carry only the model ("Golf Alltrack wagon"). A listing with no usable
    text at all stays: can't verify != mismatch.
    """
    unverified: list[str] = []
    for want, have in ((f.make, listing.make), (f.model, listing.model)):
        if not want:
            continue
        if have and have.strip():
            if not _tokens_match(want, have):
                return False
        else:
            unverified.append(want)
    if unverified and listing.title.strip():
        return any(_tokens_match(w, listing.title) for w in unverified)
    return True


def _matches_filters(listing: Listing, f: SearchFilters) -> bool:
    """Enforce the search criteria on what a site actually returned.

    Scrape-based adapters can hand back off-query inventory (e.g. CarMax pads a
    zero-result search with a "cars near you" carousel), so never trust a site
    to have applied the filters. Bounds only apply when the listing has the
    field — an unparsed price/year/mileage is not evidence of a mismatch.
    """
    if f.min_year is not None and listing.year is not None and listing.year < f.min_year:
        return False
    if f.max_year is not None and listing.year is not None and listing.year > f.max_year:
        return False
    if f.max_mileage is not None and listing.mileage is not None and listing.mileage > f.max_mileage:
        return False
    if f.min_price is not None and listing.price is not None and listing.price < f.min_price:
        return False
    if f.max_price is not None and listing.price is not None and listing.price > f.max_price:
        return False
    # Transmission is a STRICT filter: when the user asks for a transmission we
    # require the listing to confirm it. Unlike price/year/mileage, an unknown
    # transmission is dropped — a "manual" search should not surface cars we can't
    # verify as manual. (API sources report it; scrapers that don't are excluded
    # from transmission-filtered searches.)
    if f.transmission and (
        not listing.transmission
        or listing.transmission.strip().lower() != f.transmission.strip().lower()
    ):
        return False
    return _name_ok(listing, f)


def _sort(listings: list[Listing], sort: str) -> list[Listing]:
    # None values always sort last regardless of direction.
    def key_price(l: Listing) -> tuple[int, int]:
        return (0, l.price) if l.price is not None else (1, 0)

    def key_year(l: Listing) -> tuple[int, int]:
        return (0, l.year) if l.year is not None else (1, 0)

    def key_mileage(l: Listing) -> tuple[int, int]:
        return (0, l.mileage) if l.mileage is not None else (1, 0)

    if sort == "price_asc":
        return sorted(listings, key=key_price)
    if sort == "price_desc":
        return sorted(listings, key=lambda l: (key_price(l)[0], -key_price(l)[1]))
    if sort == "year_desc":
        return sorted(listings, key=lambda l: (key_year(l)[0], -key_year(l)[1]))
    if sort == "year_asc":
        return sorted(listings, key=key_year)
    if sort == "mileage_asc":
        return sorted(listings, key=key_mileage)
    # "relevance": keep source order (grouped by adapter).
    return listings


def _merge_relevance(site_listing_sets: list[list[Listing]]) -> list[Listing]:
    """Interleave sources so one adapter can't monopolize the first page.

    The old behavior preserved adapter order wholesale, which meant an early
    adapter returning many matches could fill the page before later sources were
    even visible once ``limit`` was applied. Round-robin across adapters keeps
    the default "relevance" view source-diverse while preserving per-site order.
    """
    merged: list[Listing] = []
    index = 0
    while True:
        added = False
        for listings in site_listing_sets:
            if index < len(listings):
                merged.append(listings[index])
                added = True
        if not added:
            break
        index += 1
    return merged


def _select_adapters(
    filters: SearchFilters, registry: dict[str, Adapter]
) -> list[Adapter]:
    if filters.sites:
        return [registry[sid] for sid in filters.sites if sid in registry]
    return list(registry.values())


async def _run_one(
    adapter: Adapter,
    filters: SearchFilters,
    client: httpx.AsyncClient,
    settings: Settings,
    sem: asyncio.Semaphore,
) -> tuple[SiteResult, list[Listing]]:
    async with sem:
        if not adapter.available(settings):
            return SiteResult(site=adapter.id, label=adapter.label, error="not configured"), []
        try:
            listings = await asyncio.wait_for(
                adapter.search(filters, client, settings),
                timeout=adapter.timeout(settings),
            )
        except asyncio.TimeoutError:
            return SiteResult(site=adapter.id, label=adapter.label, error="timeout"), []
        except Exception as exc:  # noqa: BLE001 — surface as a per-site error
            return SiteResult(site=adapter.id, label=adapter.label, error=str(exc)[:200]), []
        listings = [l for l in listings if _matches_filters(l, filters)]
        return (
            SiteResult(site=adapter.id, label=adapter.label, count=len(listings)),
            listings,
        )


async def search(
    filters: SearchFilters,
    client: httpx.AsyncClient,
    settings: Settings,
    registry: dict[str, Adapter],
) -> SearchResponse:
    """Fan the query out to every selected site concurrently and merge results."""
    started = time.monotonic()
    adapters = _select_adapters(filters, registry)
    sem = asyncio.Semaphore(max(1, settings.max_concurrency))

    outcomes = await asyncio.gather(
        *(_run_one(a, filters, client, settings, sem) for a in adapters)
    )

    site_results = [outcome[0] for outcome in outcomes]
    site_listing_sets = [listings for _, listings in outcomes]
    if filters.sort == "relevance":
        merged = _merge_relevance(site_listing_sets)
    else:
        merged = []
        for listings in site_listing_sets:
            merged.extend(listings)

    merged = _dedup(merged)
    merged = _sort(merged, filters.sort)
    merged = merged[: filters.limit]

    took_ms = int((time.monotonic() - started) * 1000)
    return SearchResponse(listings=merged, sites=site_results, took_ms=took_ms)
