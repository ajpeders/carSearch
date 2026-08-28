from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from environment / .env (prefix CARSEARCH_)."""

    model_config = SettingsConfigDict(
        env_prefix="CARSEARCH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Server behaviour
    per_site_timeout: float = 12.0
    max_concurrency: int = 8
    # Auto-refresh the whole watchlist in the background every N hours (0 = off).
    # Keeps saved-search results current without manual "Re-sync".
    auto_refresh_hours: float = 6.0
    # ntfy notification target — full topic URL (e.g. https://ntfy.sh/mytopic or
    # http://ntfy/carsearch-alerts). Empty = no notifications. When set, each
    # background auto-refresh that finds NEW listings pushes a notification.
    ntfy_url: str = ""
    # Bearer token for a protected ntfy server (leave empty for public topics).
    ntfy_token: str = ""
    user_agent: str = "carSearch/0.1 (+homelab)"
    cors_origins: str = "*"

    # Demo adapter (synthetic data so the service is useful with no credentials)
    enable_demo: bool = True

    # cars.com + CarMax via FlareSolverr (self-hosted Cloudflare bypass). Real
    # listings, no API key. flaresolverr_url like http://flaresolverr:8191/v1.
    enable_cars_com: bool = False
    flaresolverr_url: str = ""
    flaresolverr_max_ms: int = 60000
    # Process-wide cap on concurrent FlareSolverr solves. FlareSolverr drives a
    # single headless browser, so overlapping solves queue and can time out —
    # this bounds the flood when many saved searches run at once.
    flaresolverr_concurrency: int = 2
    cars_com_max_results: int = 50
    default_zip: str = "80022"
    enable_carmax: bool = False
    carmax_max_results: int = 50

    # Auto.dev Vehicle Listings — real dealer inventory via a proper JSON API
    # (api.auto.dev), Bearer-auth, no Cloudflare/FlareSolverr. Free tier ~1k/mo.
    enable_autodev: bool = False
    autodev_api_key: str = ""
    autodev_max_results: int = 50

    # MarketCheck — dealer + FSBO + auction inventory via a documented JSON API
    # (api.marketcheck.com), api_key param. Adds private-party; real vdp_url.
    enable_marketcheck: bool = False
    marketcheck_api_key: str = ""
    marketcheck_max_results: int = 50

    # CIS Automotive (autodealerdata.com) — market-price enrichment. We use the
    # standard /listings2 endpoint (NOT the premium, VIN-only /valuation) to pull
    # comparable dealer inventory and rate each listing's price against it.
    enable_cis_valuation: bool = False
    cis_api_key: str = ""
    # Blank when going through RapidAPI (the RapidAPI key stands in for it).
    cis_api_id: str = ""
    cis_base_url: str = "https://cis-automotive.p.rapidapi.com"
    cis_rapidapi_host: str = "cis-automotive.p.rapidapi.com"
    # Minimum comparable sample size before we trust a market reference.
    cis_min_sample: int = 5

    # LLM-assisted parsing (OpenAI-compatible endpoint, e.g. the llm-router).
    # When set, messy listing-card HTML is structured by the model, with a
    # deterministic regex fallback if the LLM is unavailable.
    llm_base_url: str = ""
    llm_model: str = "qwen2.5:7b-instruct"
    llm_api_key: str = ""

    # Where saved searches (the multi-car watchlist) persist.
    data_dir: str = "/data"

    def cors_origin_list(self) -> list[str]:
        raw = self.cors_origins.strip()
        if raw == "*" or not raw:
            return ["*"]
        return [origin.strip() for origin in raw.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
