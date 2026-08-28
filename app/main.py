import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .adapters.base import build_registry
from .aggregator import search as run_search
from .config import get_settings
from .models import (
    Listing,
    Location,
    SavedSearch,
    SavedSearchInput,
    SavedSearchResult,
    SearchFilters,
    SearchResponse,
    SiteInfo,
)
from .store import SearchStore
from .valuation import CISClient

STATIC_DIR = Path(__file__).parent / "static"
log = logging.getLogger(__name__)


async def _notify(app: FastAPI, title: str, body: str) -> None:
    """Push a notification to the configured ntfy topic (no-op if unset).

    Uses ntfy's JSON publishing (POST the server root with a topic field) so
    UTF-8 titles/emoji work — sending the title as an HTTP header fails httpx's
    latin-1 header encoding.
    """
    settings = app.state.settings
    if not settings.ntfy_url:
        return
    parts = urlsplit(settings.ntfy_url)
    topic = parts.path.strip("/")
    if not topic:
        return
    payload = {
        "topic": topic, "title": title, "message": body,
        "tags": ["car"], "click": "https://cars.example.com",
    }
    headers = {"Authorization": f"Bearer {settings.ntfy_token}"} if settings.ntfy_token else {}
    try:
        await app.state.client.post(
            f"{parts.scheme}://{parts.netloc}", json=payload, headers=headers, timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 — notifications are best-effort
        log.warning("ntfy notify failed: %s", exc)


async def _notify_new(app: FastAPI, result: SavedSearchResult) -> None:
    """Notify about newly-seen listings for one saved search."""
    if not result.added:
        return
    added = set(result.added)
    fresh = [l for l in result.result.listings if l.id in added]
    lines = [
        (f"{l.title} — ${l.price:,}" if l.price else l.title) + (f" ({l.location})" if l.location else "")
        for l in fresh[:8]
    ]
    if len(result.added) > len(lines):
        lines.append(f"…and {len(result.added) - len(lines)} more")
    await _notify(app, f"{len(result.added)} new · {result.name}", "\n".join(lines) or "new listings")


async def _auto_refresh_loop(app: FastAPI) -> None:
    """Periodically re-run every saved search so the watchlist stays current.

    Runs searches sequentially (not concurrently) to keep the load on the single
    FlareSolverr browser gentle. Every failure is contained per-search so one bad
    run never stops the loop.
    """
    hours = app.state.settings.auto_refresh_hours
    if not hours or hours <= 0:
        log.info("auto-refresh disabled (CARSEARCH_AUTO_REFRESH_HOURS=0)")
        return
    interval = hours * 3600
    log.warning("auto-refresh scheduled every %.1fh", hours)
    while True:
        try:
            await asyncio.sleep(interval)
            saved = await asyncio.to_thread(app.state.store.list)
            log.warning("auto-refresh: running %d saved searches", len(saved))
            for s in saved:
                try:
                    result = await _run_and_store(s)
                    await _notify_new(app, result)
                except Exception as exc:  # noqa: BLE001
                    log.warning("auto-refresh %s failed: %s", s.id, exc)
        except asyncio.CancelledError:
            break
        except Exception as exc:  # noqa: BLE001 — never let the loop die
            log.warning("auto-refresh loop error: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.registry = build_registry(settings)
    app.state.store = SearchStore(settings.data_dir)
    app.state.cis = CISClient(settings)
    app.state.client = httpx.AsyncClient(
        timeout=settings.per_site_timeout,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    )
    refresh_task = asyncio.create_task(_auto_refresh_loop(app))
    try:
        yield
    finally:
        refresh_task.cancel()
        await app.state.client.aclose()


app = FastAPI(
    title="carSearch",
    version=__version__,
    summary="Aggregate car listings across many marketplaces.",
    lifespan=lifespan,
)

_cors = get_settings().cors_origin_list()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "carSearch", "version": __version__}


@app.get("/sites", response_model=list[SiteInfo])
async def sites() -> list[SiteInfo]:
    settings = app.state.settings
    registry = app.state.registry
    return [
        SiteInfo(
            id=adapter.id,
            label=adapter.label,
            region=adapter.region,
            available=adapter.available(settings),
            note=adapter.note,
        )
        for adapter in registry.values()
    ]


def _with_location(filters: SearchFilters) -> SearchFilters:
    """Apply 'my location' (zip + radius) to a search that didn't set its own."""
    loc = app.state.store.get_location()
    if loc.get("zip") and not filters.zip:
        filters = filters.model_copy(update={
            "zip": loc["zip"],
            "radius": filters.radius or loc.get("radius") or None,
        })
    return filters


async def _run(filters: SearchFilters) -> SearchResponse:
    # _with_location reads a JSON file; keep it off the event loop.
    filters = await asyncio.to_thread(_with_location, filters)
    resp = await run_search(
        filters, app.state.client, app.state.settings, app.state.registry
    )
    # Best-effort CIS market-price badges (no-op unless enabled + keyed).
    resp.listings = await app.state.cis.enrich(resp.listings, filters, app.state.client)
    return resp


@app.post("/search", response_model=SearchResponse)
async def search(filters: SearchFilters) -> SearchResponse:
    return await _run(filters)


# --- Watchlist: track more than one car at a time -------------------------

# These endpoints only touch the (synchronous, thread-safe) JSON store, so they
# are plain `def` — FastAPI runs sync path operations in a worker thread, keeping
# the file I/O off the event loop.
@app.get("/api/location", response_model=Location)
def get_location() -> Location:
    return Location(**app.state.store.get_location())


@app.put("/api/location", response_model=Location)
def set_location(body: Location) -> Location:
    return Location(**app.state.store.set_location(body.zip, body.radius))


@app.get("/api/searches", response_model=list[SavedSearch])
def list_searches() -> list[SavedSearch]:
    return app.state.store.list()


@app.post("/api/searches", response_model=SavedSearch)
def add_search(body: SavedSearchInput) -> SavedSearch:
    return app.state.store.add(body.name, body.filters)


@app.put("/api/searches/{search_id}", response_model=SavedSearch)
def update_search(search_id: str, body: SavedSearchInput) -> SavedSearch:
    updated = app.state.store.update(search_id, body.name, body.filters)
    if not updated:
        raise HTTPException(status_code=404, detail="search not found")
    return updated


@app.delete("/api/searches/{search_id}")
def delete_search(search_id: str) -> dict[str, bool]:
    if not app.state.store.delete(search_id):
        raise HTTPException(status_code=404, detail="search not found")
    return {"deleted": True}


async def _run_and_store(s: SavedSearch) -> SavedSearchResult:
    """Run a search, persist its listings (add new / drop gone), report the diff."""
    res = await _run(s.filters)
    # A run that returned fewer than the limit saw the whole result set; one that
    # hit the limit is a partial view (don't stale/re-notify churned listings).
    complete = len(res.listings) < s.filters.limit
    diff = await asyncio.to_thread(
        app.state.store.save_listings, s.id, [l.model_dump() for l in res.listings],
        complete=complete,
    )
    # Serve the reconciled set: fresh listings stamped with first_seen/last_seen,
    # plus grace-kept "stale" ones so a flaky site run doesn't blank the card.
    res.listings = [Listing.model_validate(l) for l in diff["listings"]]
    return SavedSearchResult(
        id=s.id, name=s.name, filters=s.filters, result=res,
        added=diff["added"], removed=diff["removed"],
    )


@app.post("/api/searches/run", response_model=list[SavedSearchResult])
async def run_all_searches() -> list[SavedSearchResult]:
    """Run every saved search concurrently, persist and diff each."""
    saved = await asyncio.to_thread(app.state.store.list)
    return list(await asyncio.gather(*(_run_and_store(s) for s in saved)))


@app.post("/api/searches/{search_id}/run", response_model=SavedSearchResult)
async def run_one_search(search_id: str) -> SavedSearchResult:
    s = await asyncio.to_thread(app.state.store.get, search_id)
    if not s:
        raise HTTPException(status_code=404, detail="search not found")
    return await _run_and_store(s)


@app.get("/api/searches/results")
def saved_results() -> list[dict]:
    """Persisted last-known listings per search — instant page load, no re-run."""
    return [
        {"id": s.id, "name": s.name, "filters": s.filters.model_dump(),
         "listings": app.state.store.get_listings(s.id)}
        for s in app.state.store.list()
    ]


# --- Favorites --------------------------------------------------------------

@app.get("/api/favorites", response_model=list[Listing])
def get_favorites() -> list[Listing]:
    return app.state.store.list_favorites()


@app.post("/api/favorites", response_model=list[Listing])
def add_favorite(listing: Listing) -> list[Listing]:
    return app.state.store.add_favorite(listing.model_dump())


@app.delete("/api/favorites/{listing_id}")
def remove_favorite(listing_id: str) -> dict:
    return {"favorites": app.state.store.remove_favorite(listing_id)}


# --- Frontend (served last so API routes win) -----------------------------
# The self-contained page lives in app/static/index.html; mounting at "/" with
# html=True serves it at the root and any static assets alongside it.
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
