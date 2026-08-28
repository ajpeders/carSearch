from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag

from ..config import Settings
from ..models import Listing

PRICE_RE = re.compile(r"\$(\d[\d,]{2,})")
# "$283/mo", "$1,499 down", "$1399 shipping" are fees/financing, not sale prices.
PAYMENT_TAIL_RE = re.compile(
    r"^\s*(?:/\s*(?:mo|month|wk|week)\b|per\s+(?:month|week)\b|down\b|shipping\b)", re.I
)
MILES_RE = re.compile(r"([\d,]{3,})\s*(?:mi|miles)\b(?!\s*away)", re.I)
# CarMax-style compact odometer: "52K mi".
MILES_K_RE = re.compile(r"\b(\d{1,3}(?:\.\d)?)\s*K\s*(?:mi|miles)\b(?!\s*away)", re.I)
YEAR_RE = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")
STATE_RE = re.compile(
    r"\b([A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*){0,2}),\s*([A-Z]{2})\b"
)
STATES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO "
    "MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY "
    "DC PR VI GU".split()
)

BLOCKED_RE = re.compile(
    r"just a moment|attention required|challenge-platform|cf-chl|captcha|access denied",
    re.I,
)
MANUAL_RE = re.compile(r"\b(6[- ]?speed|5[- ]?speed)?\s*manual\b|\bmt\b", re.I)
# No bare "\bat\b" — card text like "Only at South Denver" is not a transmission.
AUTO_RE = re.compile(r"\bautomatic\b|\bauto\b", re.I)
CONDITION_PREFIX_RE = re.compile(r"^\s*(?:used|new|certified)\s+", re.I)
TITLE_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.'&/-]*")
MULTIWORD_MAKES: tuple[tuple[str, str], ...] = (
    ("alfa", "romeo"),
    ("aston", "martin"),
    ("land", "rover"),
    ("rolls", "royce"),
)

DETAIL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AWD", re.compile(r"\bAWD\b|all[- ]wheel drive", re.I)),
    ("4WD", re.compile(r"\b4WD\b|four[- ]wheel drive", re.I)),
    ("FWD", re.compile(r"\bFWD\b|front[- ]wheel drive", re.I)),
    ("RWD", re.compile(r"\bRWD\b|rear[- ]wheel drive", re.I)),
    ("Hybrid", re.compile(r"\bhybrid\b", re.I)),
    ("Electric", re.compile(r"\belectric\b|\bEV\b", re.I)),
    ("Diesel", re.compile(r"\bdiesel\b", re.I)),
    ("Turbo", re.compile(r"\bturbo\b", re.I)),
    ("Leather", re.compile(r"\bleather\b", re.I)),
    ("Sunroof", re.compile(r"\bsunroof\b|\bmoonroof\b", re.I)),
    ("Navigation", re.compile(r"\bnavigation\b|\bnav\b", re.I)),
    ("Backup camera", re.compile(r"backup camera|rear[- ]view camera", re.I)),
    ("Heated seats", re.compile(r"heated seats?", re.I)),
)


def to_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value.replace(",", ""))
    except (TypeError, ValueError):
        return None


def compact(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def first_match(rx: re.Pattern[str], text: str) -> str | None:
    m = rx.search(text)
    return m.group(1) if m else None


def parse_price(text: str) -> int | None:
    """First dollar amount that reads as a sale price, skipping financing
    figures ("$283/mo", "$1,499 down") that marketplaces show alongside."""
    for m in PRICE_RE.finditer(text):
        if PAYMENT_TAIL_RE.match(text[m.end():m.end() + 16]):
            continue
        return to_int(m.group(1))
    return None


def parse_miles(text: str) -> int | None:
    """Odometer from card text; handles both "31,456 miles" and "52K mi"."""
    full = MILES_RE.search(text)
    compact_k = MILES_K_RE.search(text)
    if full and (not compact_k or full.start() <= compact_k.start()):
        return to_int(full.group(1))
    if compact_k:
        return int(float(compact_k.group(1)) * 1000)
    return None


def city_state(text: str) -> str | None:
    found = None
    for match in STATE_RE.finditer(text):
        if match.group(2) in STATES:
            found = match
    if not found:
        return None
    return f"{compact(found.group(1))}, {found.group(2)}"


def first_image(node: Tag | None, base_url: str) -> str | None:
    if node is None:
        return None
    img = node.find("img")
    if not isinstance(img, Tag):
        return None
    for attr in ("src", "data-src", "data-original", "data-lazy"):
        value = img.get(attr)
        if isinstance(value, str) and value and not value.startswith("data:"):
            return urljoin(base_url, value)
    return None


def transmission_from_text(text: str) -> str | None:
    if MANUAL_RE.search(text):
        return "Manual"
    if AUTO_RE.search(text):
        return "Automatic"
    return None


def vehicle_details(text: str, keywords: str | None = None) -> tuple[str | None, list[str]]:
    transmission = transmission_from_text(text)
    highlights: list[str] = []
    seen: set[str] = set()

    def add(label: str) -> None:
        key = label.lower()
        if key not in seen:
            highlights.append(label)
            seen.add(key)

    if transmission:
        add(transmission)
    for label, pattern in DETAIL_PATTERNS:
        if pattern.search(text):
            add(label)

    for raw in re.split(r"[,;|]+", keywords or ""):
        keyword = compact(raw)
        if len(keyword) >= 3 and keyword.lower() in text.lower():
            add(keyword)

    return transmission, highlights[:8]


def _norm_title_tokens(text: str) -> list[str]:
    return [m.group(0).strip(".,;:()[]{}").lower() for m in TITLE_TOKEN_RE.finditer(text or "")]


def _title_remainder(title: str) -> list[str]:
    clean = CONDITION_PREFIX_RE.sub("", compact(title))
    year = YEAR_RE.search(clean)
    if year:
        clean = clean[year.end():]
    return _norm_title_tokens(clean)


def infer_make_model_from_title(
    title: str,
    *,
    make_hint: str | None = None,
    model_hint: str | None = None,
) -> tuple[str | None, str | None]:
    """Best-effort make/model from a listing title.

    Prefer exact filter hints when the title contains them in order; otherwise
    parse a simple ``YEAR MAKE MODEL...`` title shape as a deterministic fallback.
    """
    tokens = _title_remainder(title)
    if not tokens:
        return None, None

    hint_make_tokens = _norm_title_tokens(make_hint or "")
    hint_model_tokens = _norm_title_tokens(model_hint or "")

    make: str | None = None
    model: str | None = None
    offset = 0

    if hint_make_tokens and tokens[: len(hint_make_tokens)] == hint_make_tokens:
        make = compact(make_hint or "")
        offset = len(hint_make_tokens)
    elif len(tokens) >= 2 and tuple(tokens[:2]) in MULTIWORD_MAKES:
        make = " ".join(word.capitalize() for word in tokens[:2])
        offset = 2
    else:
        make = tokens[0].capitalize()
        offset = 1

    if hint_model_tokens and tokens[offset: offset + len(hint_model_tokens)] == hint_model_tokens:
        model = compact(model_hint or "")
    elif len(tokens) > offset:
        model = tokens[offset]
        if model:
            model = model.upper() if len(model) <= 3 and model.isalpha() else model.capitalize()

    return make or None, model or None


def apply_title_make_model(
    listings: list[Listing],
    *,
    make_hint: str | None = None,
    model_hint: str | None = None,
) -> None:
    """Fill blank listing make/model fields from the title, in place."""
    for listing in listings:
        if listing.make and listing.model:
            continue
        make, model = infer_make_model_from_title(
            listing.title, make_hint=make_hint, model_hint=model_hint
        )
        listing.make = listing.make or make
        listing.model = listing.model or model


def nearby_card(anchor: Tag, required: re.Pattern[str] = YEAR_RE) -> Tag:
    node = anchor
    for parent in anchor.parents:
        if not isinstance(parent, Tag) or parent.name in {"body", "html"}:
            break
        text = compact(parent.get_text(" ", strip=True))
        if len(text) > 30 and required.search(text):
            node = parent
            if PRICE_RE.search(text) or MILES_RE.search(text):
                break
    return node


def flaresolverr_timeout(settings: Settings, *, llm: bool = False) -> float:
    """Whole-adapter budget for a FlareSolverr-backed search.

    A challenge solve can take up to ``flaresolverr_max_ms``; add headroom for the
    POST round-trip and DOM parsing. Adapters pass this to the aggregator via
    their ``timeout()`` override so the global (fast-API) per-site timeout never
    cancels a solve mid-flight. Pass ``llm=True`` when a make/model llm-router
    refine runs after the fetch — it has its own 60s httpx timeout, so the budget
    must cover both.
    """
    budget = settings.flaresolverr_max_ms / 1000 + 30
    if llm:
        budget += 65
    return budget


async def llm_make_model(
    titles: list[str], client: httpx.AsyncClient, settings: Settings
) -> dict[int, dict]:
    """Extract make/model for a batch of listing titles via the local llm-router.

    One request for the whole batch. Returns ``{index: {"make":.., "model":..}}``;
    an empty dict when the router is unconfigured or unavailable, so callers fall
    back to their regex/DOM guesses. Runs on the user's local GPU (qwen) — never
    an external LLM.
    """
    if not settings.llm_base_url or not titles:
        return {}
    listing = "\n".join(f"[{i}] {t}" for i, t in enumerate(titles))
    user = (
        "For each numbered vehicle title, output the make and model. Return ONLY a JSON "
        'array of {"index":int,"make":str,"model":str}. Model is the family name, e.g. '
        '"A6 allroad", "Golf Alltrack", "Q5".\n\n' + listing
    )
    try:
        resp = await client.post(
            settings.llm_base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {settings.llm_api_key or 'none'}"},
            json={"model": settings.llm_model, "temperature": 0, "messages": [
                {"role": "system", "content": "You extract car make/model from titles. Output JSON only."},
                {"role": "user", "content": user},
            ]},
            timeout=60,
        )
        resp.raise_for_status()
        txt = resp.json()["choices"][0]["message"]["content"]
        arr = json.loads(txt[txt.index("["): txt.rindex("]") + 1])
        return {int(o["index"]): o for o in arr if isinstance(o, dict) and "index" in o}
    except Exception:
        return {}


async def apply_llm_make_model(
    listings: list[Listing], client: httpx.AsyncClient, settings: Settings
) -> None:
    """Fill in make/model on listings via the llm-router, in place.

    No-op when the router isn't configured or returns nothing — the listings keep
    whatever make/model the adapter already parsed.
    """
    if not settings.llm_base_url or not listings:
        return
    mm = await llm_make_model([l.title for l in listings], client, settings)
    for i, listing in enumerate(listings):
        guess = mm.get(i)
        if not guess:
            continue
        listing.make = guess.get("make") or listing.make
        listing.model = guess.get("model") or listing.model


_FLARESOLVERR_SEM_ATTR = "_carsearch_flaresolverr_sem"


def flaresolverr_gate(client: httpx.AsyncClient, settings: Settings) -> asyncio.Semaphore:
    """Process-wide cap on concurrent FlareSolverr solves.

    FlareSolverr drives a single headless browser, so overlapping solves queue and
    can exhaust memory or time out — a real risk when ``run_all_searches`` fans
    every saved search out to the scrape adapters at once. The semaphore is cached
    on the shared httpx client (one per app lifespan, hence one per event loop) so
    every adapter and every concurrent saved search contends on the *same* limit,
    and it never leaks across event loops in tests.
    """
    sem = getattr(client, _FLARESOLVERR_SEM_ATTR, None)
    if sem is None:
        sem = asyncio.Semaphore(max(1, settings.flaresolverr_concurrency))
        setattr(client, _FLARESOLVERR_SEM_ATTR, sem)
    return sem


async def fetch_with_flaresolverr(
    url: str,
    client: httpx.AsyncClient,
    settings: Settings,
) -> str:
    if not settings.flaresolverr_url:
        raise RuntimeError("flaresolverr_url is not configured")
    # Hold the gate only for the expensive solve; parsing the reply is cheap.
    async with flaresolverr_gate(client, settings):
        resp = await client.post(
            settings.flaresolverr_url,
            json={"cmd": "request.get", "url": url, "maxTimeout": settings.flaresolverr_max_ms},
            timeout=settings.flaresolverr_max_ms / 1000 + 20,
        )
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"flaresolverr: non-JSON response ({exc})") from exc
    if data.get("status") != "ok":
        raise RuntimeError(f"flaresolverr: {data.get('message', 'error')}")
    html = data.get("solution", {}).get("response", "") or ""
    if not html.strip():
        raise RuntimeError("flaresolverr: empty response")
    return html


def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")
