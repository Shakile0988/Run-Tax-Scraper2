"""
Georgia County Tax Scraper (Catalis / Avalon / Sturgis Wildfire platform)
==========================================================================

Works with any county tax commissioner site running on the
catalisgov.com / sturgiswebservices.com "Wildfire" search platform.

How it works:
  1. Fetch the county's taxes.html page and extract the TENANT_ID (a GUID)
     from the HTML source via regex. Every county site embeds this GUID in:
        <script src=".../js/{GUID}/1.js">
  2. Use that GUID to POST to the CloudFront data API:
        https://d1ebsyxxbc7tep.cloudfront.net/data/{GUID}/Wildfire/Records
     Body: {"value": "<parcel>", "skip": 0, "direct": false,
            "facets": {"Status": {}, "Type": {}, "Years": {"<year>": true}}}
  3. Parse the JSON response into a clean, structured record.

Usage:
    from tax_scraper import TaxScraper
    scraper = TaxScraper()
    result = scraper.search(
        county_url="https://www.brantleytax.com",
        parcel="B065 172",
        year=2019,
    )
"""

import re
import json
import requests
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any


CLOUDFRONT_BASE = "https://d1ebsyxxbc7tep.cloudfront.net"

GUID_RE = re.compile(
    r"/(?:js|css)/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/"
)


# ---------------------------------------------------------------------------
# All known county sites. Nothing is ever removed from this list -- new
# counties can only be ADDED. If a GUID is known, it's cached here so the
# scraper skips the auto-detect HTTP call; if not, it stays as None and
# will be auto-detected from the site's HTML on first use.
# ---------------------------------------------------------------------------
KNOWN_COUNTIES: Dict[str, Dict[str, Optional[str]]] = {
    "brantley": {
        "url": "https://www.brantleytax.com",
        "guid": "b0e49e2c-bd50-450c-9a24-6b7efe41aed4",
    },
    "chattooga": {
        "url": "https://www.chattoogatax.com",
        "guid": "571a5057-e965-49ea-85d5-8b5bb4270cac",
    },
    "dawson": {
        "url": "https://www.dawsoncountytax.com",
        "guid": None,
    },
    "hall": {
        "url": "https://hallcountytax.org",
        "guid": None,
    },
    "pierce": {
        "url": "https://piercegatax.com",
        "guid": None,
    },
    "union": {
        "url": "https://www.uniongatax.com",
        "guid": None,
    },
}


@dataclass
class TaxRecord:
    """A single, human-friendly tax bill record."""

    year: int
    bill_number: Any
    parcel_number: str
    owner_name: str
    owner_address: str
    situs_address: str
    description: str
    acres: float
    fair_market_value: float
    assessed_value: float
    millage: float
    gross_tax: float
    credits: float
    base_tax: float
    penalty: float
    interest: float
    amount_due: float
    payment_status: str
    payment_date: Optional[str]
    payment_amount: float
    due_date: Optional[str]
    is_delinquent: bool
    tax_entities: List[Dict[str, Any]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, rec: Dict[str, Any]) -> "TaxRecord":
        values = rec.get("Values", {}) or {}
        payment = rec.get("Payment", {}) or {}
        owner_addr = rec.get("OwnerAddress", {}) or {}
        situs = rec.get("SitusAddress", {}) or {}

        owner_addr_str = ", ".join(
            p for p in [
                owner_addr.get("Line1"),
                owner_addr.get("Line2"),
                owner_addr.get("Line3"),
                owner_addr.get("City"),
                owner_addr.get("State"),
                owner_addr.get("Zip"),
            ] if p
        )
        situs_addr_str = ", ".join(
            p for p in [
                situs.get("Line1"),
                situs.get("Line2"),
                situs.get("Line3"),
                situs.get("City"),
                situs.get("State"),
                situs.get("Zip"),
            ] if p
        )

        return cls(
            year=rec.get("Year"),
            bill_number=rec.get("BillNumber"),
            parcel_number=(rec.get("ParcelNumber") or "").strip(),
            owner_name=rec.get("OwnerName1") or rec.get("Name") or "",
            owner_address=owner_addr_str,
            situs_address=situs_addr_str,
            description=rec.get("Description") or "",
            acres=rec.get("Acres") or 0,
            fair_market_value=values.get("FairMarketValue", 0),
            assessed_value=values.get("Assessed", 0),
            millage=values.get("Millage", 0),
            gross_tax=values.get("GrossTax", 0),
            credits=values.get("Credits", 0),
            base_tax=values.get("BaseTax", 0),
            penalty=values.get("Penalty", 0),
            interest=values.get("Interest", 0),
            amount_due=values.get("AmountDue", 0),
            payment_status=payment.get("Status") or payment.get("PaymentStatus") or "",
            payment_date=payment.get("Date"),
            payment_amount=payment.get("Amount", 0),
            due_date=rec.get("DueDate"),
            is_delinquent=bool(rec.get("isDelinquent")),
            tax_entities=[
                {
                    "name": (te.get("Name") or "").strip(),
                    "millage_rate": te.get("MillageRate", 0),
                    "gross_tax": te.get("GrossTax", 0),
                    "credit": te.get("Credit", 0),
                    "net_tax": te.get("NetTax", 0),
                }
                for te in (rec.get("TaxEntities") or [])
            ],
            raw=rec,
        )

    def to_dict(self, include_raw: bool = False) -> Dict[str, Any]:
        d = self.__dict__.copy()
        if not include_raw:
            d.pop("raw", None)
        return d


class TaxScraperError(Exception):
    pass


class TaxScraper:
    def __init__(self, timeout: int = 20, user_agent: Optional[str] = None):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent or (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
        })
        self._guid_cache: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Step 1: Detect the county's TENANT_ID (GUID) from its HTML
    # ------------------------------------------------------------------
    def detect_guid(self, county_url: str) -> str:
        """Fetches the county's taxes.html and extracts the TENANT_ID (GUID)
        via regex. Returns from cache if already detected."""

        county_url = county_url.rstrip("/")
        if county_url in self._guid_cache:
            return self._guid_cache[county_url]

        candidates = [
            f"{county_url}/taxes.html",
            f"{county_url}/",
        ]

        last_err = None
        for url in candidates:
            try:
                resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
                resp.raise_for_status()
                match = GUID_RE.search(resp.text)
                if match:
                    guid = match.group(1)
                    self._guid_cache[county_url] = guid
                    return guid
            except requests.RequestException as e:
                last_err = e
                continue

        raise TaxScraperError(
            f"Could not detect TENANT_ID (GUID) from '{county_url}'. "
            f"The site structure may differ. Last error: {last_err}"
        )

    # ------------------------------------------------------------------
    # Step 2: Call the Wildfire/Records API
    # ------------------------------------------------------------------
    def fetch_records(
        self,
        guid: str,
        parcel: str,
        year: Optional[int] = None,
        referer: Optional[str] = None,
        skip: int = 0,
    ) -> Dict[str, Any]:
        """Calls the Records API for a given county GUID + parcel.
        year=None returns records for all years."""

        url = f"{CLOUDFRONT_BASE}/data/{guid}/Wildfire/Records"

        facets: Dict[str, Any] = {"Status": {}, "Type": {}, "Years": {}}
        if year is not None:
            facets["Years"] = {str(year): True}

        payload = {
            "value": parcel,
            "skip": skip,
            "direct": False,
            "facets": facets,
        }

        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": referer.rstrip("/") if referer else None,
            "Referer": referer if referer else None,
        }
        headers = {k: v for k, v in headers.items() if v}

        resp = self.session.post(
            url, data=json.dumps(payload), headers=headers, timeout=self.timeout
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Step 3 (optional): Autocomplete -- useful for verifying exact parcel match
    # ------------------------------------------------------------------
    def autocomplete(self, guid: str, query: str, referer: Optional[str] = None) -> List[Dict[str, str]]:
        url = f"{CLOUDFRONT_BASE}/data/{guid}/Wildfire/Autocomplete"
        headers = {}
        if referer:
            headers["Referer"] = referer
        resp = self.session.get(
            url, params={"q": query}, headers=headers, timeout=self.timeout
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # High-level: does everything in one call
    # ------------------------------------------------------------------
    def search(
        self,
        county_url: str,
        parcel: str,
        year: Optional[int] = None,
        guid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        county_url : e.g. "https://www.brantleytax.com"
        parcel     : e.g. "B065 172"
        year       : e.g. 2019 (None returns all years)
        guid       : pass directly if already known (skips auto-detect)

        Returns:
        {
            "county_url": ...,
            "guid": ...,
            "parcel_query": ...,
            "year_filter": ...,
            "total_records": N,
            "records": [ TaxRecord.to_dict(), ... ]
        }
        """
        county_url = county_url.rstrip("/")
        used_guid = guid or self.detect_guid(county_url)

        data = self.fetch_records(
            guid=used_guid, parcel=parcel, year=year, referer=county_url
        )

        records = [TaxRecord.from_api(r) for r in data.get("Records", [])]

        return {
            "county_url": county_url,
            "guid": used_guid,
            "parcel_query": parcel,
            "year_filter": year,
            "search_token": data.get("SearchToken"),
            "total_records": data.get("TotalRecords", len(records)),
            "records": [r.to_dict() for r in records],
        }

    def search_by_name(self, name: str) -> Optional[Dict[str, str]]:
        """Look up a county from KNOWN_COUNTIES by its shortcut name.
        e.g. name="brantley" -> {"url":..., "guid":...}"""
        return KNOWN_COUNTIES.get(name.lower())


# ==========================================================================
# Example usage (runs if this file is executed directly)
# ==========================================================================
if __name__ == "__main__":
    scraper = TaxScraper()

    result = scraper.search(
        county_url="https://www.brantleytax.com",
        parcel="B065 172",
        year=2019,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
