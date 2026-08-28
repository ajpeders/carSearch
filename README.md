# carSearch

A small, standalone service that searches car listings across many
marketplaces at once and returns a single merged result set. It fans a query
out to per-site **adapters** concurrently, normalizes and de-duplicates the
results, and reports what each site returned (including failures).

This service is fully self-contained — it has **no dependency on any other
project** and is meant to run on its own in a homelab (Docker or bare uvicorn).

## Quick start (Docker)

```bash
cp .env.example .env      # optional; the demo adapter works with no config
docker compose up --build
```

Then:

```bash
curl localhost:8080/health
curl localhost:8080/sites
curl -X POST localhost:8080/search \
  -H 'content-type: application/json' \
  -d '{"make":"Toyota","max_price":25000,"sort":"price_asc"}'
```

Interactive API docs are at `http://localhost:8080/docs`.

## Quick start (local Python)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8080
pytest
```

## API

| Method | Path      | Description                                             |
| ------ | --------- | ------------------------------------------------------- |
| GET    | `/health` | Liveness probe.                                         |
| GET    | `/sites`  | List adapters and whether each is configured/available. |
| POST   | `/search` | Aggregate listings across sites.                        |

`POST /search` body (all fields optional):

```jsonc
{
  "make": "Toyota",
  "model": "Camry",
  "keywords": "AWD leather",
  "min_price": 0, "max_price": 25000,
  "min_year": 2015, "max_year": 2024,
  "max_mileage": 80000,
  "zip": "94103", "radius": 100,
  "sort": "price_asc",          // relevance|price_asc|price_desc|year_desc|year_asc|mileage_asc
  "sites": ["demo", "ebay_motors"], // omit to query every available site
  "limit": 50
}
```

Response: `{ listings: Listing[], sites: SiteResult[], took_ms: number }`.
Each `SiteResult` carries a per-site `count` and an optional `error`
(`"not configured"`, `"timeout"`, or the failure message) so partial results
are always explainable.

## Adapters

| id            | site          | status                                            |
| ------------- | ------------- | ------------------------------------------------- |
| `demo`        | Demo Listings | Synthetic data; on by default so it works with no config. |
| `autodev`     | Auto.dev      | Real dealer inventory via a JSON API (`CARSEARCH_AUTODEV_API_KEY`). No scraping. |
| `marketcheck` | MarketCheck   | Dealer + private-party via a JSON API (`CARSEARCH_MARKETCHECK_API_KEY`); real `vdp_url`. |
| `cars_com`    | Cars.com      | Best-effort scrape via FlareSolverr (`CARSEARCH_ENABLE_CARS_COM=true`). |
| `carmax`      | CarMax        | Best-effort scrape via FlareSolverr (`CARSEARCH_ENABLE_CARMAX=true`). |

An adapter that isn't configured is simply reported as `"not configured"` and
skipped — the service still returns whatever the other sites found.

> Prefer the JSON-API sources (Auto.dev, MarketCheck) — they're reliable and need
> no headless browser. The FlareSolverr scrapers (cars.com, CarMax) add coverage
> but are fragile. AutoTrader/TrueCar/CarGurus/eBay/Craigslist adapters were
> removed 2026-07-13 (Cloudflare-blocked / dead endpoints / redundant).

### Adding a site

1. Create `app/adapters/<site>.py` with a class subclassing `Adapter`
   (see `app/adapters/base.py`). Implement `search()` and, if it needs
   credentials, `available()`.
2. Register it in `build_registry()` in `app/adapters/base.py`.
3. Add a test in `tests/test_adapters.py` (use `respx` to mock HTTP).

> **Note on scraping:** several large marketplaces have no public API and/or
> forbid scraping in their terms of service. Add adapters for those only where
> you have permission or an API key. The framework isolates each adapter and
> caps it with a per-site timeout, so a slow or blocked site never breaks a
> search.

## Watchlist, listing lifecycle & notifications

Saved searches (`/api/searches`) are re-run automatically every
`CARSEARCH_AUTO_REFRESH_HOURS` (default 6h; 0 disables) and on demand from the
UI ("Re-sync"). Each run is reconciled against the stored results:

- **New** listings get a persisted `first_seen` stamp; the UI shows a NEW badge
  while it's <24h old, and each background refresh that finds new listings
  pushes an ntfy notification (`CARSEARCH_NTFY_URL` + optional
  `CARSEARCH_NTFY_TOKEN`). Manual re-syncs don't notify — you're watching.
- **Disappeared** listings aren't dropped immediately: one flaky site run would
  otherwise churn them out and re-notify them as "new" when they come back.
  They stay for a 48h grace window flagged `stale` (rendered dimmed with a
  "Gone?" badge — usually means sold), then are purged and reported `removed`.
- A listing that returns within the grace window is silently un-staled — no
  duplicate notification.

## Configuration

All settings are environment variables prefixed `CARSEARCH_` (see
`.env.example`). Key ones: `PER_SITE_TIMEOUT`, `MAX_CONCURRENCY`,
`AUTO_REFRESH_HOURS`, `CORS_ORIGINS`, `AUTODEV_API_KEY`, `MARKETCHECK_API_KEY`,
`FLARESOLVERR_URL` (for cars.com/CarMax), `DEFAULT_ZIP`.

## Extracting this into its own repository

This directory is self-contained. To make it a standalone repo:

```bash
# from a copy of this carSearch/ directory
git init
git add .
git commit -m "Initial commit: carSearch service"
git remote add origin <your-new-repo-url>
git push -u origin main
```

Nothing here imports from the parent project, so no code changes are needed
after extraction.
