"""Persistence for the multi-car watchlist (saved searches).

Saved searches live in a single JSON file under the configured data dir so the
service can track several cars at once and survive restarts.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .models import SavedSearch, SearchFilters

log = logging.getLogger(__name__)

DEFAULT_SEARCHES: list[SavedSearch] = []

# How long a listing that vanished from a run is kept (flagged "stale") before
# it's actually dropped. Covers a couple of flaky auto-refresh cycles so a
# transient site failure doesn't churn the watchlist (and re-notify as "new").
LISTING_GRACE_SECONDS = 48 * 3600.0


def _usable_data_dir(data_dir: str) -> Path:
    """Return a writable base dir, falling back if the configured one isn't.

    The default is ``/data`` — the container mount point — which is usually not
    writable outside Docker, so a bare ``uvicorn`` run would otherwise crash at
    startup. Fall back to ``./data`` and finally a temp dir, keeping the service
    up (watchlist just won't persist across restarts in the temp case).
    """
    for candidate in (Path(data_dir), Path.cwd() / "data"):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write-test"
            probe.write_text("")
            probe.unlink()
            if candidate != Path(data_dir):
                log.warning("data_dir %r not writable; using %s", data_dir, candidate)
            return candidate
        except OSError:
            continue
    tmp = Path(tempfile.mkdtemp(prefix="carsearch-"))
    log.warning("data_dir %r not writable; using ephemeral %s", data_dir, tmp)
    return tmp


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or uuid.uuid4().hex[:8]


class SearchStore:
    """Thread-safe JSON-backed list of saved searches."""

    def __init__(self, data_dir: str) -> None:
        base = _usable_data_dir(data_dir)
        self._path = base / "searches.json"
        self._loc_path = base / "location.json"
        self._lock = threading.Lock()
        if not self._path.exists():
            self._write(DEFAULT_SEARCHES)

    # --- "my location": zip + radius applied to searches that don't set one ---
    def get_location(self) -> dict:
        try:
            return json.loads(self._loc_path.read_text())
        except (OSError, ValueError):
            return {"zip": "", "radius": 100}

    def set_location(self, zip: str, radius: int) -> dict:
        with self._lock:
            loc = {"zip": (zip or "").strip(), "radius": int(radius) if radius else 0}
            self._loc_path.write_text(json.dumps(loc, indent=2))
            return loc

    def _read(self) -> list[SavedSearch]:
        try:
            raw = json.loads(self._path.read_text())
            return [SavedSearch.model_validate(item) for item in raw]
        except (OSError, ValueError):
            return list(DEFAULT_SEARCHES)

    def _write(self, items: list[SavedSearch]) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps([i.model_dump() for i in items], indent=2))
        tmp.replace(self._path)

    def list(self) -> list[SavedSearch]:
        with self._lock:
            return self._read()

    def get(self, search_id: str) -> SavedSearch | None:
        return next((s for s in self.list() if s.id == search_id), None)

    def add(self, name: str, filters: SearchFilters) -> SavedSearch:
        with self._lock:
            items = self._read()
            existing = {s.id for s in items}
            sid = _slug(name)
            while sid in existing:
                sid = f"{sid}-{uuid.uuid4().hex[:4]}"
            item = SavedSearch(id=sid, name=name, filters=filters)
            items.append(item)
            self._write(items)
            return item

    def update(self, search_id: str, name: str, filters: SearchFilters) -> SavedSearch | None:
        with self._lock:
            items = self._read()
            for i, s in enumerate(items):
                if s.id == search_id:
                    items[i] = SavedSearch(id=search_id, name=name, filters=filters)
                    self._write(items)
                    return items[i]
            return None

    def delete(self, search_id: str) -> bool:
        with self._lock:
            items = self._read()
            kept = [s for s in items if s.id != search_id]
            if len(kept) == len(items):
                return False
            self._write(kept)
        self.clear_listings(search_id)
        return True

    # --- persisted listings per search (add new / drop gone on each run) ---
    def _listings_all(self) -> dict:
        try:
            return json.loads((Path(self._path.parent) / "listings.json").read_text())
        except (OSError, ValueError):
            return {}

    def get_listings(self, search_id: str) -> list[dict]:
        return self._listings_all().get(search_id, [])

    def save_listings(
        self, search_id: str, fresh: list[dict], grace_seconds: float = LISTING_GRACE_SECONDS,
        complete: bool = True,
    ) -> dict:
        """Reconcile a fresh run against the stored set.

        Newly-seen listings get ``first_seen`` stamped (the UI badges them NEW
        while recent). Listings missing from this run aren't dropped immediately:
        one flaky site run (FlareSolverr timeout, Cloudflare block) would
        otherwise "remove" them and re-notify them as new next run. Instead they
        stay for ``grace_seconds`` flagged ``stale`` ("possibly sold"), and are
        purged — and reported as removed — only after staying gone that long.

        ``complete`` is False when the run hit its result ``limit`` — a partial
        view where we can't distinguish "gone" from "fell out of the capped
        window". In that case a missing listing is kept as-is (not flagged stale,
        not re-notified when it churns back), only time-purged past the grace
        window. This stops limit-hitting searches from churning false NEW/Gone.

        Returns {"added": ids, "removed": ids, "listings": merged}.
        """
        p = Path(self._path.parent) / "listings.json"
        now = time.time()
        with self._lock:
            allls = self._listings_all()
            prev = {l["id"]: l for l in allls.get(search_id, [])}
            fresh_ids = {l["id"] for l in fresh}
            merged = []
            added = []
            for l in fresh:
                old = prev.get(l["id"])
                if old is None:
                    added.append(l["id"])
                merged.append({
                    **l,
                    "first_seen": (old or {}).get("first_seen") or now,
                    "last_seen": now,
                    "stale": False,
                })
            removed = []
            for lid, old in prev.items():
                if lid in fresh_ids:
                    continue
                last_seen = old.get("last_seen") or now
                if now - last_seen > grace_seconds:
                    removed.append(lid)                       # gone long enough → purge
                elif complete:
                    # Full result set → it genuinely disappeared: flag "possibly sold".
                    merged.append({**old, "last_seen": last_seen, "stale": True})
                else:
                    # Capped/partial run → can't tell gone from out-of-window; keep
                    # unchanged (no false stale, no re-notify when it churns back).
                    merged.append({**old, "last_seen": last_seen})
            allls[search_id] = merged
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(allls, indent=2))
            tmp.replace(p)
        return {"added": added, "removed": removed, "listings": merged}

    def clear_listings(self, search_id: str) -> None:
        p = Path(self._path.parent) / "listings.json"
        with self._lock:
            allls = self._listings_all()
            if allls.pop(search_id, None) is not None:
                p.write_text(json.dumps(allls, indent=2))

    # --- favorites (full listing objects so they persist even if delisted) ---
    def _fav_path(self) -> Path:
        return Path(self._path.parent) / "favorites.json"

    def list_favorites(self) -> list[dict]:
        try:
            return json.loads(self._fav_path().read_text())
        except (OSError, ValueError):
            return []

    def add_favorite(self, listing: dict) -> list[dict]:
        with self._lock:
            favs = self.list_favorites()
            if not any(f.get("id") == listing.get("id") for f in favs):
                favs.append(listing)
                self._fav_path().write_text(json.dumps(favs, indent=2))
            return favs

    def remove_favorite(self, listing_id: str) -> list[dict]:
        with self._lock:
            favs = [f for f in self.list_favorites() if f.get("id") != listing_id]
            self._fav_path().write_text(json.dumps(favs, indent=2))
            return favs
