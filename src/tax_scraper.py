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
import time
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
        "guid": "4979a210-6a6c-4ef6-8156-3c6b812d29e5",
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
            bill_number=rec.get("BillNumber") or rec.get("BillNo"),
            parcel_number=(rec.get("ParcelNumber") or "").strip(),
            owner_name=rec.get("Name") or rec.get("LegalOwner") or rec.get("OwnerName1") or "",
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
                for te in (rec.get("TaxEntities") or rec.get("Jurisdictions") or [])
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
    def __init__(self, timeout: Optional[int] = None, user_agent: Optional[str] = None,
                 proxy_url: Optional[str] = None):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent or (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "cross-site",
        })
        self._guid_cache: Dict[str, str] = {}

        # Optional proxy support: pass proxy_url explicitly, or set the
        # PROXY_URL env var (e.g. as a GitHub Actions secret). Format:
        # "http://user:pass@host:port" or "http://host:port".
        #
        # No-signup free option: set USE_FREE_PROXY=1 to pull a random
        # public proxy from ProxyScrape's free list (no account needed).
        # WARNING: public/open proxies have a very low success rate
        # (roughly ~2%) against bot-protected sites and are frequently
        # dead or slow. This is a best-effort fallback, not a real fix --
        # a paid/managed proxy is far more likely to actually work.
        import os
        resolved_proxy = proxy_url or os.environ.get("PROXY_URL")
        if not resolved_proxy and os.environ.get("USE_FREE_PROXY"):
            resolved_proxy = self._get_free_public_proxy()
        if resolved_proxy:
            self.session.proxies.update({
                "http": resolved_proxy,
                "https": resolved_proxy,
            })

    @staticmethod
    def _get_free_public_proxy() -> Optional[str]:
        """Grabs one random HTTP proxy from ProxyScrape's free, no-signup
        public list. No reliability guarantee -- see the warning above."""
        try:
            resp = requests.get(
                "https://api.proxyscrape.com/v4/free-proxy-list/get",
                params={
                    "request": "display_proxies",
                    "protocol": "http",
                    "proxy_format": "protocolipport",
                    "format": "text",
                },
                timeout=15,
            )
            resp.raise_for_status()
            lines = [ln.strip() for ln in resp.text.splitlines() if ln.strip()]
            if not lines:
                return None
            import random
            return random.choice(lines)
        except requests.RequestException:
            return None

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
            f"{county_url}/pay-bill/",
            f"{county_url}/pay-bill.html",
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
        search_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Calls the Records API for a given county GUID + parcel.
        year=None returns records for all years.

        search_token: the SearchToken from a previous unfiltered call to the
        same parcel. The website itself does a 2-step flow: (1) search the
        parcel with no year facet, (2) THEN check a year checkbox, which
        re-queries using the SearchToken from step 1 plus the Years facet.
        Some counties (e.g. Dawson) return the wrong record if you skip
        straight to a single filtered call without that token -- see
        TaxScraper.search() which does this 2-step flow automatically
        whenever `year` is given."""

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
            # Confirmed via DevTools: the site sends the previous search's
            # SearchToken back as a REQUEST HEADER (not a body field) when
            # applying a facet filter (e.g. checking a Year checkbox).
            "SearchToken": search_token,
        }
        headers = {k: v for k, v in headers.items() if v}

        resp = self._post_with_retry(url, payload, headers)
        return resp.json()

    def _post_with_retry(
        self, url: str, payload: Dict[str, Any], headers: Dict[str, str],
        max_attempts: int = 5, backoff_seconds: float = 5,
    ) -> requests.Response:
        """POSTs with retries on 502/503/504 -- these are transient
        gateway/upstream-timeout errors from a slow county backend
        (seen on Dawson), not something a client-side timeout value can
        fix. Retries with increasing backoff before giving up."""
        last_exc: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
                resp = self.session.post(
                    url, data=json.dumps(payload), headers=headers, timeout=self.timeout
                )
                if resp.status_code in (502, 503, 504) and attempt < max_attempts:
                    time.sleep(backoff_seconds * attempt)
                    continue
                resp.raise_for_status()
                return resp
            except requests.exceptions.RequestException as e:
                last_exc = e
                if attempt < max_attempts:
                    time.sleep(backoff_seconds * attempt)
                    continue
                raise
        raise last_exc  # pragma: no cover

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

        if year is not None:
            # Step A: a normal, already year-filtered search (fast -- same
            # as the original single-call approach). We only need its
            # SearchToken, not a full unfiltered fetch of every year
            # (which is what was timing out with 504s on Dawson).
            initial = self.fetch_records(
                guid=used_guid, parcel=parcel, year=year, referer=county_url
            )
            token = initial.get("SearchToken")

            # Step B: repeat the same filtered query, now passing that
            # SearchToken back as a header -- this is what makes the
            # backend return the FULL correct record (right BillNo,
            # Jurisdictions/tax breakdown, and Name) instead of a
            # stale/partial one.
            data = self.fetch_records(
                guid=used_guid, parcel=parcel, year=year,
                referer=county_url, search_token=token,
            )
        else:
            data = self.fetch_records(
                guid=used_guid, parcel=parcel, year=None, referer=county_url
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
