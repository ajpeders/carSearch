# carSearch roadmap

Candidate future work, roughly ordered within each group. Nothing here is
committed to a timeline — it's a backlog surfaced from a repo audit. Done items
are kept briefly for context.

## Adapters

- [x] **TrueCar** — FlareSolverr HTML adapter (`app/adapters/truecar.py`).
- [x] **CarGurus** — public JSON, no FlareSolverr (`app/adapters/cargurus.py`).
- [x] **TrueCar — verified live** (2026-07-07): returns real in-radius listings
      with price/mileage/location; make/model filled via the llm-router. Enabled
      in prod.
- [ ] **CarGurus — endpoint dead.** The guessed
      `/Cars/inventorylisting/ajaxFetchSubsetInventoryListing.action` JSON endpoint
      returns **404** against production; the parser is fixture-only and can't run
      live. Disabled in prod (`CARSEARCH_ENABLE_CARGURUS=false`). Needs the real
      current endpoint (inspect cargurus.com XHR traffic) before re-enabling.
- [ ] **More sources as needed** (Facebook Marketplace, local dealer feeds).
      Each new site is an `Adapter` subclass registered in
      `app/adapters/base.py:build_registry` — see the "Adding a site" section in
      the README.

## Reliability

- [ ] **FlareSolverr block resilience.** Under bursty load (e.g. `run_all` fanning
      several searches × 4 scrape adapters through a single-browser FlareSolverr),
      Cloudflare-gated sites intermittently return an unsolved challenge →
      `"<site> blocked or challenged"`. Same query succeeds when FlareSolverr is
      fresh. Mitigations to consider: run saved searches sequentially in
      `run_all_searches` (bound peak load), a short retry/backoff on a detected
      block, and/or a cooldown between solves.

## Data sources

- [x] **Adapter roster trimmed to 5** (2026-07-13): kept demo, cars_com,
      carmax, autodev, marketcheck. Removed autotrader + truecar (Cloudflare),
      cargurus (dead endpoint), ebay_motors (marginal), craigslist (IP-blocked).

- [x] **Auto.dev Vehicle Listings — added & verified live** (2026-07-07). Real
      dealer-inventory JSON API (`api.auto.dev/listings`, Bearer auth, ~1k
      calls/mo free), no Cloudflare/FlareSolverr. Returns price/mileage/dealer/
      city-state/photo + rich vehicle specs; in-radius via `distance`. Click-through
      is a VIN web-search (Auto.dev exposes no dealer VDP URL — `retailListing.vdp`
      is an internal fragment).
- [ ] **Consider retiring the FlareSolverr dealer-scrapers** (cars.com, AutoTrader,
      CarMax, TrueCar) in favor of Auto.dev now that it's proven — it covers the
      same dealer inventory reliably without a headless browser. Keep eBay +
      Craigslist for private-party. Decide after comparing coverage over a few days.
- [x] **MarketCheck — added & verified live** (2026-07-13). Dealer + private-party
      inventory via `api.marketcheck.com/v2/search/car/active` (api_key), real
      `vdp_url` links. Radius clamped to 100 mi (plan cap; 422 above). Surfaced
      listings the others missed (e.g. a $14.9k Golf Alltrack). Free tier ~500
      calls/mo — auto-refresh stays under it.
- [ ] **Carvana / Cars & Bids** — probed 2026-07-13, both blocked from the
      homelab: Carvana's public endpoint is 410-dead + PerimeterX; Cars & Bids is
      Cloudflare-gated. Would need a live request-capture from a browser to build.

## Performance

- [ ] **Result caching / request coalescing.** Every `/search` re-drives every
      adapter, and FlareSolverr solves are expensive (tens of seconds). Add a
      short-TTL cache keyed on the normalized filters, and coalesce identical
      concurrent searches so a burst hits each site once.
- [x] **Bound FlareSolverr concurrency process-wide** (`flaresolverr_concurrency`
      gate) so `run_all_searches` can't flood a single-browser FlareSolverr.
- [x] **Keep blocking JSON-store I/O off the event loop** (sync endpoints +
      `asyncio.to_thread`).

## Watchlist / product

- [x] **Scheduled background refresh** (2026-07-11): an in-app asyncio loop
      re-runs every saved search every `CARSEARCH_AUTO_REFRESH_HOURS` (default 6),
      sequentially to keep FlareSolverr load gentle. Fail-isolated per search.
- [ ] **Notifications** on new matches (email / ntfy / webhook) built on the
      auto-refresh loop + the added/removed diff from `store.save_listings`.

## Security / hardening (review 2026-07-11)

- [ ] **Container runs as root** (the one real finding). Dockerfile has no
      `USER`; the running container is `User=""`, `ReadonlyRootfs=false`,
      `CapDrop=[]`. Not urgent — deployed behind Traefik `local-only@file` with
      `no-new-privileges` — but it only ever writes `/data` (a volume), so it's
      a clean fit for: non-root user in the Dockerfile + `read_only: true` +
      `tmpfs: /tmp` + `cap_drop: ALL` in `services/carsearch/docker-compose.yml`.
- [ ] **CORS defaults to `*`** — fine behind local-only, but tighten to
      `cars.example.com` to close a small browser-scripting gap.
- [ ] **Standalone `apps/carSearch` compose publishes `8080:8080` on 0.0.0.0** —
      dev-only (deploy uses `services/`), but bind to `127.0.0.1` so it can't be
      accidentally LAN-exposed with no auth.
- [ ] **Non-atomic writes** for `favorites`/`location`/`clear_listings` — the
      main stores use tmp+replace; these don't (tiny corruption-on-crash window).
- [ ] **`run_all_searches`** uses `asyncio.gather` without
      `return_exceptions=True`, so one failing saved search fails the whole
      batch (per-adapter errors are already isolated; a store write failure is not).

## Fixes / cleanup

- [x] **Aggregator post-filter** (2026-07-11): never trust a site to have
      applied the search criteria — enforce make/model/year/mileage/price on
      every listing an adapter returns. Kills the CarMax failure mode where a
      zero-result search page padded with a "cars near you" carousel came back
      as off-query junk (Sentras on an Alltrack search).
- [x] **Card-text parsing fixes** (2026-07-11): skip financing figures
      ("$283/mo", "$1,499 down") when extracting prices, parse CarMax-style
      "52K mi" odometers, and stop bare "at" reading as an Automatic
      transmission ("Only at South Denver…").
- [ ] **Demo id collision.** The two 2019 Golf Alltrack fixtures in
      `app/adapters/demo.py` hash to the same id/url (id is `sha1(title)`), so
      one gets deduped away. Give demo listings distinct ids.
- [x] **Per-adapter timeouts** so FlareSolverr adapters aren't cancelled
      mid-solve by the global per-site timeout.
- [x] **Graceful `/data` fallback** for bare `uvicorn` runs, plus a compose
      volume so the watchlist persists.

## Search quality & notifications (2026-07-13)

- [x] **Transmission filter** — `SearchFilters.transmission` ("manual"/"automatic"),
      enforced in the aggregator only when the listing's transmission is known.
      Precise on the API sources (Auto.dev/MarketCheck report it); scrapers
      (cars.com/CarMax) rarely parse it, so those pass through as "unknown".
- [x] **Strict transmission filter** (2026-07-13): when a transmission is
      requested, listings whose transmission is unknown are dropped (not just
      mismatches). So a "manual" search shows only confirmed manuals — sourced
      from the APIs that report transmission; scrapers that don't parse it are
      excluded from transmission-filtered searches. (LLM-enrich was rejected:
      the scraped card text doesn't contain transmission, so there's nothing to
      extract.)
- [x] **Generic-model API fallback** — Auto.dev/MarketCheck match model exactly,
      so "Allroad" returned 0; now retry make-only and let the aggregator's token
      post-filter keep the matches.
- [x] **ntfy notifications** — background auto-refresh pushes a notification on
      newly-seen listings (`CARSEARCH_NTFY_URL`). Built on the existing add-diff.
- [ ] **MarketCheck title dedup** — cosmetic: model+trim can repeat ("Golf
      Alltrack Alltrack S"); collapse repeated tokens in the built title.
- [ ] **Cars & Bids adapter** — probed 2026-07-13: FlareSolverr *can* get past its
      Cloudflare, but the homepage embeds only reviews; the auction list loads
      from an XHR API that needs a browser request-capture to discover. Also a
      poor structural fit (national auctions, live-bid prices, no radius, sparse).
      Revisit only with a captured API endpoint.
## Make this usable by others (added 2026-08-27)

- [ ] Universalize the README / docs / code for outside users: document setup
  from scratch on generic infrastructure, replace homelab-specific assumptions
  (private hostnames, LAN addresses, personal paths and defaults) with
  env-driven configuration plus examples, and keep the public GitHub mirror
  directly runnable.
