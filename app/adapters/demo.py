import hashlib

import httpx

from ..config import Settings
from ..models import Listing, SearchFilters
from .base import Adapter
from .html_common import vehicle_details

# A tiny fixture pool the demo adapter draws from. Deterministic so tests and
# manual pokes return stable, filterable data with no network or credentials.
_FIXTURES = [
    ("Toyota", "Camry", 2018, 18995, 42000, "San Francisco, CA"),
    ("Toyota", "Camry", 2020, 22995, 28000, "Oakland, CA"),
    ("Honda", "Civic", 2019, 17995, 36000, "San Jose, CA"),
    ("Honda", "Accord", 2017, 15995, 61000, "Berkeley, CA"),
    ("Ford", "F-150", 2016, 24995, 78000, "Fremont, CA"),
    ("Subaru", "Outback", 2021, 27995, 19000, "Palo Alto, CA"),
    ("Tesla", "Model 3", 2022, 33995, 21000, "San Mateo, CA"),
    ("Mazda", "CX-5", 2019, 20995, 40000, "Daly City, CA"),
    # VW Golf Alltrack (2019, manual) — cars we're actually watching.
    ("Volkswagen", "Golf Alltrack", 2019, 24995, 41000, "Denver, CO"),
    ("Volkswagen", "Golf Alltrack", 2019, 26995, 33000, "Boulder, CO"),
    ("Volkswagen", "Golf Alltrack", 2018, 22995, 58000, "Fort Collins, CO"),
    # Audi Allroad — newer, under 80k.
    ("Audi", "A6 Allroad", 2021, 51995, 28000, "Denver, CO"),
    ("Audi", "A4 Allroad", 2020, 34995, 39000, "Colorado Springs, CO"),
    ("Audi", "A4 Allroad", 2018, 30995, 62000, "Aurora, CO"),
    ("Audi", "A6 Allroad", 2014, 18995, 96000, "Pueblo, CO"),
]


class DemoAdapter(Adapter):
    id = "demo"
    label = "Demo Listings"
    region = "US"
    note = "Synthetic data for testing; disable with CARSEARCH_ENABLE_DEMO=false."

    def available(self, settings: Settings) -> bool:
        return settings.enable_demo

    async def search(
        self,
        filters: SearchFilters,
        client: httpx.AsyncClient,
        settings: Settings,
    ) -> list[Listing]:
        make = (filters.make or "").strip().lower()
        model = (filters.model or "").strip().lower()
        results: list[Listing] = []

        for mk, md, year, price, mileage, location in _FIXTURES:
            if make and make not in mk.lower():
                continue
            if model and model not in md.lower():
                continue
            if filters.min_price is not None and price < filters.min_price:
                continue
            if filters.max_price is not None and price > filters.max_price:
                continue
            if filters.min_year is not None and year < filters.min_year:
                continue
            if filters.max_year is not None and year > filters.max_year:
                continue
            if filters.max_mileage is not None and mileage > filters.max_mileage:
                continue

            title = f"{year} {mk} {md}"
            if mk == "Volkswagen" and md == "Golf Alltrack" and year == 2019:
                title = f"{title} Manual AWD"
            uid = hashlib.sha1(title.encode()).hexdigest()[:10]
            transmission, highlights = vehicle_details(title, filters.keywords)
            results.append(
                Listing(
                    id=uid,
                    site=self.id,
                    site_label=self.label,
                    title=title,
                    price=price,
                    year=year,
                    make=mk,
                    model=md,
                    mileage=mileage,
                    location=location,
                    url=f"https://example.invalid/demo/{uid}",
                    image_url=None,
                    dealer="Demo Motors",
                    transmission=transmission,
                    highlights=highlights,
                )
            )
        return results
