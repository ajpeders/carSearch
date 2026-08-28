# carSearch — Architecture

A single FastAPI service (`app/`) that fans a car search out to per-site
adapters, merges the results, and maintains a persistent multi-car watchlist
with background refresh and ntfy notifications. Self-contained: no dependency
on anything else in the homelab repo.

## Components

```
Browser (app/static/index.html — single self-contained page)
   │  REST
   ▼
FastAPI (app/main.py)
   ├── Aggregator (app/aggregator.py) ──► Adapters (app/adapters/*)
   │                                        ├── demo         synthetic, no config
   │                                        ├── autodev      JSON API (Bearer key)
   │                                        ├── marketcheck  JSON API (api_key param)
   │                                        ├── cars_com     scrape via FlareSolverr
   │                                        └── carmax       scrape via FlareSolverr
   ├── CIS valuation (app/valuation.py)    optional market-price enrichment
   ├── SearchStore (app/store.py)          JSON files under /data
   └── ntfy notifications (_notify)        background-refresh finds only
```

- **`app/main.py`** — HTTP surface, app lifespan (shared `httpx.AsyncClient`,
  adapter registry, store, CIS client), the auto-refresh background task, and
  ntfy publishing.
- **`app/aggregator.py`** — concurrency fan-out (semaphore-capped), per-site
  timeout/error containment, post-filtering, dedup, merge, sort.
- **`app/adapters/base.py`** — the `Adapter` ABC + `build_registry()`.
  `app/adapters/html_common.py` holds all shared scraping machinery: regex
  parsers, FlareSolverr fetch + process-wide solve gate, LLM make/model
  extraction.
- **`app/store.py`** — thread-safe JSON persistence (searches, per-search
  listings, favorites, "my location") with atomic tmp-file writes.
- **`app/valuation.py`** — CIS Automotive client; builds a comparable price
  distribution per (make, model, area) and grades each listing against it.

## Search data flow

1. `POST /search` (or a saved-search run) → `_run()` applies "my location"
   (zip+radius) if the search didn't set one.
2. The aggregator selects adapters (`filters.sites` or all), runs each under a
   global semaphore (`CARSEARCH_MAX_CONCURRENCY`) and a per-adapter timeout.
   FlareSolverr adapters override `timeout()` with a budget derived from
   `flaresolverr_max_ms` (+65s when an LLM refine follows) so the default 12s
   `per_site_timeout` never cancels a browser solve mid-flight.
3. **Never trust the site**: every listing is re-checked against the filters
   (`_matches_filters`) because scrapers pad zero-result pages with "near you"
   carousels. Bounds only apply when the listing has the field — except
   **transmission, which is strict**: a "manual" search drops listings whose
   transmission can't be confirmed.
4. Make/model matching (`_tokens_match`) requires **all** tokens of the wanted
   model in the listing ("A4 Allroad" ≠ plain "A4" sedan). Scrape adapters fill
   make/model from titles, refined by the local llm-router (qwen on the local
   GPU — never an external LLM) with a deterministic regex fallback.
5. Results are round-robin interleaved across sites (relevance sort) so one
   adapter can't monopolize the first page, deduped (URL, then
   title+price+year+mileage when a price exists), sorted, and truncated to
   `limit`. Per-site errors are surfaced in `sites[]`, never fatal.
6. Optional CIS enrichment stamps `market_price` / `price_delta` /
   `deal_rating` (fail-open; cached ~1h per make/model/area).

## Watchlist lifecycle

Saved searches live in `searches.json`; each run's results are reconciled into
`listings.json` by `store.save_listings`:

- New listings get `first_seen`/`last_seen` epoch stamps; the UI badges NEW
  while `first_seen` < 24h (persisted, so it survives reloads and covers
  background-refresh finds).
- Listings missing from a run are kept for a **48h grace window** flagged
  `stale` (UI: dimmed + "Gone?" badge) before being purged and reported
  `removed`. This is deliberate: one flaky FlareSolverr run would otherwise
  drop a site's listings and re-notify them as "new" on the next run.
- `added` (which drives ntfy notifications) therefore fires only for
  never-before-seen listings, or ones that return after >48h gone (relisted —
  arguably worth a ping).

The auto-refresh loop (`_auto_refresh_loop`, every
`CARSEARCH_AUTO_REFRESH_HOURS`, default 6h) runs saved searches
**sequentially** to keep load on the single-browser FlareSolverr gentle, and
notifies via ntfy per search that found new listings. Manual re-syncs from the
UI don't notify — the user is watching. A process-wide semaphore
(`flaresolverr_gate`, cap `CARSEARCH_FLARESOLVERR_CONCURRENCY=2`) additionally
bounds concurrent solves when the UI fans out.

## Key decisions

- **JSON files, not a database** — single-user homelab scale; atomic
  tmp-file+rename writes under one `threading.Lock`, run via `asyncio.to_thread`
  (or sync path operations) to stay off the event loop.
- **Prefer JSON-API adapters** (Auto.dev, MarketCheck) over scrapers; the
  FlareSolverr pair adds coverage but is fragile. The roster was trimmed to the
  reliable five on 2026-07-13 (see ROADMAP).
- **API-exact model params fall back to make-only**: Auto.dev/MarketCheck match
  model exactly, so a generic model ("Allroad") retries make-only and relies on
  the aggregator's token post-filter.
- **LLM parsing is local-only** (llm-router / qwen), best-effort, one batched
  call per page of titles.
- **Frontend is one static HTML file** served by the same app — no build step,
  mounted last so API routes win.
- **ntfy publishing uses the JSON body** (topic in payload), not headers —
  UTF-8 titles/emoji break httpx's latin-1 header encoding.

## Persistence layout (`/data`, or `./data` outside Docker)

| File             | Contents                                             |
| ---------------- | ---------------------------------------------------- |
| `searches.json`  | Saved searches (id, name, filters)                   |
| `listings.json`  | Last-known listings per search, with lifecycle stamps|
| `favorites.json` | Full listing objects (survive delisting)             |
| `location.json`  | "My location" zip + radius default                   |
