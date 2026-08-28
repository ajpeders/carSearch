# carSearch — HOWTO

Step-by-step guides for common tasks. Overview in README.md, design in
ARCHITECTURE.md.

## Run it

```bash
# Docker (persists /data in a named volume)
cp .env.example .env        # optional; demo adapter works with no config
docker compose up --build -d
open http://localhost:8080

# Bare Python (dev)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8080
```

Run the tests: `pytest` (all offline; HTTP is mocked with respx).

## Track a new car

1. Open the UI → **Add car**.
2. Fill make + model (model tokens must all appear in a listing title, so
   "Golf Alltrack" won't match a plain Golf). Keywords are highlight chips +
   free-text for the scrapers, not strict filters. Transmission **is** strict —
   "Manual" drops listings that can't be confirmed manual.
3. Leave ZIP/radius blank to inherit "my location" (set in the toolbar).
4. **Search** on the card to run it once; **Re-sync all** runs everything.

Or via API:

```bash
curl -X POST localhost:8080/api/searches -H 'content-type: application/json' \
  -d '{"name":"A4 Allroad","filters":{"make":"Audi","model":"A4 Allroad","max_price":30000}}'
curl -X POST localhost:8080/api/searches/a4-allroad/run
```

## Enable a real listing source

Set the relevant vars in `.env`, restart, then check `GET /sites` shows
`available: true`.

| Source      | Vars                                                             |
| ----------- | ---------------------------------------------------------------- |
| Auto.dev    | `CARSEARCH_ENABLE_AUTODEV=true`, `CARSEARCH_AUTODEV_API_KEY=…`   |
| MarketCheck | `CARSEARCH_ENABLE_MARKETCHECK=true`, `CARSEARCH_MARKETCHECK_API_KEY=…` |
| cars.com    | `CARSEARCH_ENABLE_CARS_COM=true`, `CARSEARCH_FLARESOLVERR_URL=http://flaresolverr:8191/v1` |
| CarMax      | `CARSEARCH_ENABLE_CARMAX=true` + the same FlareSolverr URL       |
| CIS pricing | `CARSEARCH_ENABLE_CIS_VALUATION=true`, `CARSEARCH_CIS_API_KEY=…` (RapidAPI key) |

Free-tier budgets: Auto.dev ~1k calls/mo, MarketCheck ~500/mo — the 6h
auto-refresh stays under both with a handful of saved searches.

## Set up notifications (ntfy)

1. Point the service at a topic:
   `CARSEARCH_NTFY_URL=https://ntfy.example.com/carsearch-alerts` and, for an
   auth-protected server, `CARSEARCH_NTFY_TOKEN=<write-only token>`.
2. Subscribe to the topic in the ntfy app.
3. Notifications fire **only from the background auto-refresh**
   (`CARSEARCH_AUTO_REFRESH_HOURS`, default 6, `0` disables), and only for
   listings never seen before (or gone >48h and relisted) — the 48h stale
   grace window suppresses flaky-scrape re-notifications. Manual re-syncs never
   notify.

Note: the notification click-through URL is currently hardcoded to
`https://cars.example.com` in `app/main.py:_notify`.

## Understand the listing badges

- **NEW** — `first_seen` within the last 24h (persists across reloads).
- **Gone?** (dimmed card) — missing from the latest run; kept 48h before being
  dropped. Usually means sold.
- **Great/Good/Fair price / Above market** — CIS market-median comparison
  (only when valuation is enabled and ≥5 comparables).

## Add a new site adapter

1. Create `app/adapters/<site>.py` subclassing `Adapter`; implement `search()`
   and `available()` (and `timeout()` if the backend is slow — see the
   FlareSolverr adapters).
2. Reuse `html_common.py` for scraping (price/miles/location regexes,
   `fetch_with_flaresolverr`, `llm_make_model`) — don't re-implement.
3. Register it in `build_registry()` (`app/adapters/base.py`).
4. Add a respx-mocked test (`tests/test_adapters.py` has the pattern).
5. Don't pre-trust the site's filtering — the aggregator re-checks everything,
   but return honest fields (`None` when unparsed, never guesses).

## Debug a misbehaving source

```bash
curl localhost:8080/sites                     # configured/available?
docker compose logs -f carsearch              # per-site errors are logged
curl -X POST localhost:8080/search -H 'content-type: application/json' \
  -d '{"sites":["cars_com"],"make":"Audi"}' | jq .sites
```

- `"not configured"` — enable flag / API key / FlareSolverr URL missing.
- `"timeout"` — for scrapers usually a slow challenge solve; raise
  `CARSEARCH_FLARESOLVERR_MAX_MS`. Never lower `per_site_timeout` below what a
  solve needs (the adapters budget for this themselves).
- `"… blocked (Cloudflare challenge not solved)"` — FlareSolverr is up but
  couldn't solve; often transient under bursty load (the stale grace window
  keeps the watchlist steady meanwhile). Restarting FlareSolverr helps.

## Wipe or inspect persisted state

```bash
docker exec carsearch ls /data                     # searches/listings/favorites/location
docker exec carsearch cat /data/listings.json | jq 'keys'
docker compose down && docker volume rm carsearch_carsearch-data   # full reset
```
