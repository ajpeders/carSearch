"""cars.com adapter — real listings via self-hosted FlareSolverr.

cars.com sits behind Cloudflare, so we fetch through FlareSolverr (which solves
the challenge and returns rendered HTML), then extract listings from the DOM:
each result is an <a href="/vehicledetail/…"> whose text is the title, wrapped
in a <fuse-card> that holds price/mileage/location. Numbers come from exact
regex on that single-card scope; the local LLM (llm-router) refines make/model
from the clean titles when configured, with a deterministic fallback. No API key.
"""

from __future__ import annotations

import re
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup

from ..config import Settings
from ..models import Listing, SearchFilters
from .base import Adapter
from .html_common import (
    fetch_with_flaresolverr,
    flaresolverr_timeout,
    llm_make_model,
    vehicle_details,
)

BASE = "https://www.cars.com"
RESULTS = f"{BASE}/shopping/results/"

_SORT = {
    "relevance": "best_match_desc", "price_asc": "list_price_asc",
    "price_desc": "list_price_desc", "year_desc": "year_desc",
    "year_asc": "year_asc", "mileage_asc": "mileage_asc",
}
_PRICE = re.compile(r"\$(\d[\d,]{2,})")
_MILES = re.compile(r"([\d,]{3,})\s*mi\b", re.I)
_YEAR = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")
_COND = re.compile(r"^(Used|New|Certified)\s+", re.I)

# US state / territory codes — used to validate the 2-letter tail of a location
# so trim levels ("SE", "SEL") and other capitalised tokens can't masquerade as
# a state.
_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO "
    "MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY "
    "DC PR VI GU AK".split()
)

# A US location is a short "City, ST": a Title-Case city of 1–3 words followed by
# a 2-letter state. cars.com renders it in its own node with a trailing distance
# ("Billings, MT (449 mi)"), which is the reliable anchor for the location block.
# Case-sensitive on purpose: cities are Title-Case, so requiring a leading
# capital keeps lowercase filler ("delivery from", "to") out of the city.
_CITY = r"[A-Z][A-Za-z.'\-]*(?:\s+[A-Z][A-Za-z.'\-]*){0,2}"
_LOC_DIST = re.compile(rf"({_CITY}),\s*([A-Z]{{2}})\s*\(\s*[\d,]+\s*mi")
_LOC = re.compile(rf"({_CITY}),\s*([A-Z]{{2}})\b")

# The "(N mi)" distance badge sits next to the location; strip it before parsing
# numbers so it can never be misread as mileage.
_DIST = re.compile(r"\(\s*[\d,]+\s*mi\.?\s*\)", re.I)

# Markers of an unsolved Cloudflare challenge / interstitial (FlareSolverr should
# clear these, but surface a clear error if one slips through).
_BLOCKED = re.compile(
    r"just a moment|attention required|challenge-platform|cf-chl|/cdn-cgi/challenge",
    re.I,
)


def _int(s: str | None) -> int | None:
    """Parse a comma-grouped integer, tolerating junk (returns None)."""
    if not s:
        return None
    try:
        return int(s.replace(",", ""))
    except (TypeError, ValueError):
        return None


def _city_state(snippet: str) -> str | None:
    """Extract a clean "City, ST" from a text snippet, if present.

    Prefers the distance-anchored form, else the last bare "City, ST"; only
    accepts a real US state code and keeps the city to at most three words.
    """
    best = None
    for rx in (_LOC_DIST, _LOC):
        for m in rx.finditer(snippet):
            if m.group(2).upper() in _STATES:
                best = m  # keep the LAST valid match (closest to the badge)
        if best is not None:
            break
    if best is None:
        return None
    city = re.sub(r"\s+", " ", best.group(1)).strip(" .,-")
    if not city:
        return None
    return f"{city}, {best.group(2).upper()}"


# The distance badge ("City, ST (449 mi)") — cars.com shows nationwide inventory
# for rare cars regardless of the maximum_distance param, so we read this and
# filter by it client-side.
_DIST_MI = re.compile(r"\(\s*([\d,]+)\s*mi", re.I)


def _extract_location(card, text: str) -> str | None:
    """Pull the location from the card.

    cars.com puts the location in its own node ("City, ST (N mi)"), so parse that
    isolated node first — that way a dealer name sitting beside it in the
    flattened text can't bleed into the city. Fall back to the flattened text.
    """
    if card is not None:
        nodes = [str(s) for s in card.find_all(string=_LOC_DIST)]
        for snippet in reversed(nodes):  # last node = the location block
            loc = _city_state(snippet)
            if loc:
                return loc
    return _city_state(text)


def _extract_distance(card, text: str) -> int | None:
    """Miles from the search ZIP, read from the "(N mi)" badge on the location node."""
    hay = None
    if card is not None:
        nodes = [str(s) for s in card.find_all(string=_LOC_DIST)]
        if nodes:
            hay = nodes[-1]
    m = _DIST_MI.search(hay or text)
    return int(m.group(1).replace(",", "")) if m else None


class CarsComAdapter(Adapter):
    id = "cars_com"
    label = "Cars.com"
    region = "US"
    note = "Real listings via self-hosted FlareSolverr (set CARSEARCH_FLARESOLVERR_URL)."

    def available(self, settings: Settings) -> bool:
        return settings.enable_cars_com and bool(settings.flaresolverr_url)

    def timeout(self, settings: Settings) -> float:
        # FlareSolverr solve, plus the LLM make/model refine when configured.
        return flaresolverr_timeout(settings, llm=bool(settings.llm_base_url))

    def _url(self, f: SearchFilters, settings: Settings) -> str:
        p: list[tuple[str, str]] = [("stock_type", "used")]
        if f.make:
            p.append(("makes[]", f.make.strip().lower().replace(" ", "_")))
        if f.min_price is not None:
            p.append(("list_price_min", str(f.min_price)))
        if f.max_price is not None:
            p.append(("list_price_max", str(f.max_price)))
        if f.min_year is not None:
            p.append(("year_min", str(f.min_year)))
        if f.max_year is not None:
            p.append(("year_max", str(f.max_year)))
        if f.max_mileage is not None:
            p.append(("mileage_max", str(f.max_mileage)))
        p.append(("maximum_distance", str(f.radius) if f.radius else "all"))
        p.append(("zip", f.zip or settings.default_zip))
        p.append(("sort", _SORT.get(f.sort, "best_match_desc")))
        p.append(("page_size", "50"))
        # cars.com has no clean model param, so fold model + keywords into its
        # free-text keyword search (e.g. "allroad", "Golf Alltrack manual").
        kw = " ".join(x for x in (f.model, f.keywords) if x)
        if kw:
            p.append(("keyword", kw))
        return f"{RESULTS}?{urlencode(p)}"

    async def _fetch(self, url: str, client: httpx.AsyncClient, settings: Settings) -> str:
        # Delegate to the shared helper so the concurrency gate applies here too.
        return await fetch_with_flaresolverr(url, client, settings)

    def _cards(self, html: str, limit: int) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        seen: set[str] = set()
        cards: list[dict] = []
        for a in soup.select('a[href*="/vehicledetail/"]'):
            href = a.get("href", "")
            m = re.search(r"/vehicledetail/([^/?#]+)", href)
            if not m or m.group(1) in seen:
                continue
            title = _COND.sub("", a.get_text(" ", strip=True)).strip()
            if not _YEAR.search(title):     # skip non-title links (photos, dealer, etc.)
                continue
            seen.add(m.group(1))
            card = a.find_parent("fuse-card") or a.parent
            text = re.sub(r"\s+", " ", card.get_text(" ", strip=True))[:300] if card else title
            img = (card or a).find("img")
            src = (img.get("src") or img.get("data-src")) if img else None
            cards.append({
                "id": m.group(1),
                "url": href if href.startswith("http") else BASE + href,
                "image_url": src or None,
                "title": title,
                "text": text,
                "location": _extract_location(card, text),
                "distance": _extract_distance(card, text),
            })
            if len(cards) >= limit:
                break
        return cards

    @staticmethod
    def _titleparts(title: str) -> tuple[int | None, str | None, str | None]:
        ym = _YEAR.search(title)
        year = int(ym.group(1)) if ym else None
        after = title[ym.end():].split() if ym else []
        make = after[0] if after else None
        model = after[1] if len(after) > 1 else None
        return year, make, model

    async def search(self, filters: SearchFilters, client: httpx.AsyncClient, settings: Settings) -> list[Listing]:
        html = await self._fetch(self._url(filters, settings), client, settings)
        cards = self._cards(html, settings.cars_com_max_results)
        if not cards:
            # Distinguish a genuine "no results" page from a Cloudflare block so
            # the aggregator can surface something actionable per-site.
            if _BLOCKED.search(html[:4000]):
                raise RuntimeError("cars.com blocked (Cloudflare challenge not solved)")
            return []

        # LLM refines make/model from the (clean) titles; cheap and accurate.
        mm = await llm_make_model([c["title"] for c in cards], client, settings)
        want_model = (filters.model or "").strip().lower()

        out: list[Listing] = []
        for i, c in enumerate(cards):
            title = c["title"]
            # cars.com ignores maximum_distance for rare cars and returns nationwide
            # inventory — enforce the radius ourselves from the "(N mi)" badge.
            if filters.radius and c.get("distance") and c["distance"] > filters.radius:
                continue
            # numbers: exact regex on the single-card scope, with the "(N mi)"
            # distance badge stripped so it can't be misread as mileage.
            num_text = _DIST.sub(" ", c["text"])
            pm = _PRICE.search(num_text)
            mi = _MILES.search(num_text)
            price = _int(pm.group(1)) if pm else None
            mileage = _int(mi.group(1)) if mi else None
            y, mk, md = self._titleparts(title)
            g = mm.get(i, {})
            make = g.get("make") or mk or filters.make
            model = g.get("model") or md or filters.model

            # client-side filters (cars.com make-only URL is broad)
            if want_model and want_model not in f"{title} {model or ''}".lower():
                continue
            if filters.max_mileage is not None and mileage and mileage > filters.max_mileage:
                continue
            if filters.min_year is not None and y and y < filters.min_year:
                continue
            if filters.max_year is not None and y and y > filters.max_year:
                continue
            if filters.max_price is not None and price and price > filters.max_price:
                continue
            transmission, highlights = vehicle_details(f"{title} {num_text}", filters.keywords)

            out.append(Listing(
                id=c["id"], site=self.id, site_label=self.label,
                title=title or "cars.com listing", price=price, year=y,
                make=make, model=model, mileage=mileage,
                location=c.get("location"),
                url=c["url"], image_url=c["image_url"], dealer=None,
                transmission=transmission, highlights=highlights,
            ))
        return out
