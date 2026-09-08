"""
iasWorld (Tyler Technologies) platform scraper (Phase 3 counties).

This is a completely separate module from tax_scraper.py (Phase 1,
Wildfire/Catalis) and assurancegov_scraper.py (Phase 2, AssuranceWeb
Property). Nothing here touches or imports either of those, so all
already-working counties are unaffected.

Platform covered here ("iasWorld Public Access" by Tyler Technologies):
    www.chathamtax.org               (Chatham County)
    publicaccess.claytoncountyga.gov (Clayton County)
    publicaccess.dekalbtaxga.gov     (DeKalb County)

All three are confirmed to run the exact same vendor product (page meta
tags read: Description="iasWorld", Copyright="Tyler Technologies Inc,
Akanda Solutions, LLC"), so the same field names / flow are expected to
work across all three -- only the base URL differs. This has been
live-verified against Chatham; Clayton/DeKalb should be spot-checked the
first few times they're used, since multi-jurisdiction counties can
occasionally require an extra "hdJur" (jurisdiction) value.

How it works (reverse-engineered from browser DevTools):

    1. GET  {base}/search/CommonSearch.aspx?mode=REALPROP
       -> Classic ASP.NET WebForms page. Sets ASP.NET_SessionId +
          DISCLAIMER cookies. The HTML contains hidden fields required
          for the next POST -- these are per-request and MUST be
          re-scraped fresh every time, never hardcoded:
            __VIEWSTATE, __VIEWSTATEGENERATOR, __EVENTVALIDATION,
            ScriptManager1_TSM

    2. POST {base}/search/CommonSearch.aspx?mode=REALPROP
       (same session cookies)
       Body includes the hidden fields from step 1, plus:
            inpParid=<parcel id, exact spacing as printed on the bill>
            inpTaxyr=<year, or "0" for Any>
            hdAction=Search
       -> Response HTML contains a <table id="searchResults"> with one
          <tr class="SearchResults"> per matching parcel. Each row's
          onclick attribute holds a relative link, e.g.:
            onclick="javascript:selectSearchRow(
                '../Datalets/Datalet.aspx?sIndex=2&idx=1')"
          That sIndex/idx pair addresses a server-side (session-bound)
          stored result set -- it is NOT a stable/bookmarkable URL on
          its own.

    3. To actually open a result row, the site's JS does ANOTHER POST
       back to the same CommonSearch.aspx URL, this time with:
            hdAction=Link
            hdLink=<the relative Datalet.aspx URL from step 2>
       using the same session. The server then serves/redirects to the
       Datalet detail page for that specific parcel record.

    4. The Datalet detail page (Tax Commissioner Summary / Parcel
       Status / Most Current Owner / Tax due, etc.) is parsed for the
       fields we care about. A generic label->value extractor is used
       since exact markup can vary slightly county to county.

No Cloudflare or other JS challenge was observed on any of these sites
during testing, so plain `requests` (no headless browser) is used here.
If a county later adds bot protection, this module would need to switch
to a Playwright-based approach like the separate Athens-Clarke scraper.
"""

import re
import json
from typing import Optional, Dict, Any, List
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup


IASWORLD_COUNTIES: Dict[str, str] = {
    "chatham": "https://www.chathamtax.org/PT",
    "clayton": "https://publicaccess.claytoncountyga.gov",
    "dekalb": "https://publicaccess.dekalbtaxga.gov",
}

SEARCH_PATH = "/search/CommonSearch.aspx?mode=REALPROP"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# The row-click handler passes its relative URL as a single-quoted JS
# string argument to selectSearchRow(...).
ROW_LINK_RE = re.compile(r"selectSearchRow\('([^']+)'\)")


class IasWorldError(Exception):
    pass


class IasWorldScraper:
    def __init__(self, timeout: Optional[int] = 30, user_agent: Optional[str] = None):
        self.timeout = timeout
        self.session = requests.Session()
        headers = dict(DEFAULT_HEADERS)
        if user_agent:
            headers["User-Agent"] = user_agent
        self.session.headers.update(headers)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_base(county: str) -> str:
        key = (county or "").strip().lower()
        if key in IASWORLD_COUNTIES:
            return IASWORLD_COUNTIES[key]
        # Allow a direct base URL / host to be passed instead of a shortcut.
        if county.startswith("http"):
            return county.rstrip("/")
        raise IasWorldError(
            f"Unknown iasWorld county '{county}'. "
            f"Available: {list(IASWORLD_COUNTIES)}"
        )

    @staticmethod
    def _extract_hidden_fields(html: str) -> Dict[str, str]:
        """Pulls the ASP.NET WebForms hidden fields required for the next
        POST. These are per-request/session and must never be hardcoded."""
        soup = BeautifulSoup(html, "html.parser")
        fields: Dict[str, str] = {}
        for name in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION",
                     "ScriptManager1_TSM"):
            el = soup.find("input", {"name": name}) or soup.find("input", {"id": name})
            fields[name] = el.get("value", "") if el else ""

        # ScriptManager1_TSM's <input> always renders with value="" in the
        # raw HTML -- the real value is normally filled in client-side by
        # the Telerik ScriptManager JS from the combined-scripts URL
        # (_TSM_CombinedScripts_=... on the Telerik.Web.UI.WebResource.axd
        # <script> tag). Since we don't run JS, pull it from there instead.
        m = re.search(r"_TSM_HiddenField_=ScriptManager1_TSM[^\"']*", html)
        if m:
            m2 = re.search(r"_TSM_CombinedScripts_=([^\"'&]*)", m.group(0))
            if m2:
                fields["ScriptManager1_TSM"] = unquote(m2.group(1))

        return fields

    @staticmethod
    def _dump_form_fields(html: str) -> str:
        """Diagnostic-only: lists every input/select name + (for selects)
        their option values, so a 500 on POST can be compared against the
        real field names/allowed values instead of our hardcoded guesses."""
        soup = BeautifulSoup(html, "html.parser")
        parts = []
        for inp in soup.find_all("input"):
            n = inp.get("name")
            if n:
                parts.append(f"input:{n}={inp.get('value', '')!r}"[:80])
        for sel in soup.find_all("select"):
            n = sel.get("name") or sel.get("id")
            opts = [o.get("value", o.get_text(strip=True)) for o in sel.find_all("option")]
            parts.append(f"select:{n}=options{opts}"[:200])
        return " | ".join(parts)

    @staticmethod
    def _compact_html(html: str, max_len: int = 6000) -> str:
        """Strips huge VIEWSTATE/EVENTVALIDATION blobs out of the HTML so a
        diagnostic dump stays readable, then truncates."""
        html = re.sub(
            r'(name="__VIEWSTATE"[^>]*value=")[^"]*(")',
            r"\1...TRUNCATED...\2", html,
        )
        html = re.sub(
            r'(name="__EVENTVALIDATION"[^>]*value=")[^"]*(")',
            r"\1...TRUNCATED...\2", html,
        )
        return html[:max_len]

    def _get_search_page(self, base: str) -> str:
        url = f"{base}{SEARCH_PATH}"
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code >= 400:
            raise IasWorldError(
                f"GET {url} -> {resp.status_code}. Body[:500]: {resp.text[:500]!r}"
            )
        return resp.text

    # ------------------------------------------------------------------
    # Step 1+2: load search page, then POST the parcel/year search
    # ------------------------------------------------------------------
    def _post_search(self, base: str, parcel: str, year: Optional[int]) -> str:
        html = self._get_search_page(base)
        hidden = self._extract_hidden_fields(html)

        if not hidden.get("__VIEWSTATE"):
            title_m = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
            title = title_m.group(1).strip() if title_m else "?"
            raise IasWorldError(
                f"__VIEWSTATE not found on search page (title: {title!r}) -- "
                f"likely a disclaimer/interstitial page, not the search form. "
                f"Body[:500]: {html[:500]!r}"
            )

        if "name=\"inpParid\"" not in html and "id=\"inpParid\"" not in html:
            # Not the real search form -- almost certainly an interstitial
            # (disclaimer / redirect) page that must be clicked through
            # first. Dump the compacted HTML so the real flow can be seen.
            raise IasWorldError(
                "GET landed on an interstitial page (no inpParid field found) "
                "-- likely a disclaimer/redirect page that must be submitted "
                "first. Compact HTML:\n" + self._compact_html(html)
            )

        form: Dict[str, str] = {
            "ScriptManager1_TSM": hidden["ScriptManager1_TSM"],
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
            "__VIEWSTATE": hidden["__VIEWSTATE"],
            "__VIEWSTATEGENERATOR": hidden["__VIEWSTATEGENERATOR"],
            "__EVENTVALIDATION": hidden["__EVENTVALIDATION"],
            "PageNum": "",
            "SortBy": "PARID",
            "SortDir": " asc",
            "PageSize": "15",
            "hdAction": "Search",
            "hdIndex": "",
            "sIndex": "-1",
            "hdListType": "PA",
            "hdJur": "",
            "hdSelectAllChecked": "false",
            "inpParid": parcel,
            "inpUnit": "",
            "searchClt$hdSelSuf": "",
            "searchClt$hdSelDir": "",
            "hdTaxYear": "",
            "inpTaxyr": str(year) if year else "0",
            "selSortBy": "PARID",
            "selSortDir": " asc",
            "searchOptions$hdBeta": "",
            "btSearch": "",
            "RadWindow_NavigateUrl_ClientState": "",
            "mode": "REALPROP",
            "mask": "",
            "param1": "",
            "searchimmediate": "",
        }

        url = f"{base}{SEARCH_PATH}"
        resp = self.session.post(
            url, data=form, timeout=self.timeout,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": url,
                "Origin": base.split("/PT")[0] if "/PT" in base else base,
            },
        )
        if resp.status_code >= 400:
            raise IasWorldError(
                f"POST {url} -> {resp.status_code}. "
                f"Real search-page fields were: {self._dump_form_fields(html)}"
            )
        return resp.text

    # ------------------------------------------------------------------
    # Parse the search-results HTML for matching rows
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_results(html: str) -> List[Dict[str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("tr.SearchResults")
        results = []
        for row in rows:
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            onclick = row.get("onclick", "")
            m = ROW_LINK_RE.search(onclick)
            link = m.group(1) if m else None
            results.append({
                "property_id": cells[0] if len(cells) > 0 else None,
                "owner": cells[1] if len(cells) > 1 else None,
                "parcel_address": cells[2] if len(cells) > 2 else None,
                "city": cells[3] if len(cells) > 3 else None,
                "tax_year": cells[4] if len(cells) > 4 else None,
                "_row_link": link,
            })
        return results

    # ------------------------------------------------------------------
    # Step 3: follow a result row's link (a same-session postback) to
    # reach the Datalet detail page
    # ------------------------------------------------------------------
    def _open_detail(self, base: str, search_html: str, row_link: str) -> str:
        hidden = self._extract_hidden_fields(search_html)
        form: Dict[str, str] = {
            "ScriptManager1_TSM": hidden["ScriptManager1_TSM"],
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
            "__VIEWSTATE": hidden["__VIEWSTATE"],
            "__VIEWSTATEGENERATOR": hidden["__VIEWSTATEGENERATOR"],
            "__EVENTVALIDATION": hidden["__EVENTVALIDATION"],
            "hdAction": "Link",
            "hdLink": row_link,
            "mode": "REALPROP",
        }
        url = f"{base}{SEARCH_PATH}"
        resp = self.session.post(
            url, data=form, timeout=self.timeout,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": url,
            },
            allow_redirects=True,
        )
        resp.raise_for_status()
        return resp.text

    # ------------------------------------------------------------------
    # Generic label -> value extractor for the Datalet detail page.
    # iasWorld renders most detail data as simple two-column tables /
    # label-then-value pairs, so we look for a label's text and grab the
    # next sibling cell/text.
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_detail_fields(html: str) -> Dict[str, Optional[str]]:
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text("\n", strip=True)

        def grab(label: str) -> Optional[str]:
            # Try table-cell based extraction first.
            label_td = soup.find(string=re.compile(rf"^{re.escape(label)}\s*:?\s*$"))
            if label_td:
                parent = label_td.find_parent(["td", "th", "div", "span"])
                if parent:
                    sib = parent.find_next_sibling(["td", "div", "span"])
                    if sib:
                        val = sib.get_text(strip=True)
                        if val:
                            return val
            # Fallback: line-based text search "Label\nValue"
            m = re.search(rf"{re.escape(label)}\s*:?\s*\n?([^\n]+)", text)
            if m:
                val = m.group(1).strip()
                if val and val.lower() != label.lower():
                    return val
            return None

        return {
            "parid": grab("PARID"),
            "owner_name": grab("Most Current Owner") or grab("Current Owner"),
            "mailing_address": grab("Mailing Address"),
            "status": grab("Status"),
            "alternate_id": grab("Alternate ID"),
            "bill_number": grab("Bill #"),
            "tax_district": grab("Tax District/Description") or grab("Tax District"),
            "legal_description": grab("Legal Description"),
            "property_class": grab("Property Class"),
            "millage_rate": grab("Total Millage Rate") or grab("Millage Rate"),
            "parcel_status": grab("Parcel Status"),
        }

    # ------------------------------------------------------------------
    # High-level: does everything in one call
    # ------------------------------------------------------------------
    def search(self, county: str, parcel: str, year: Optional[int] = None) -> Dict[str, Any]:
        """
        county : shortcut name ("chatham", "clayton", "dekalb") or a
                 direct base URL
        parcel : Property ID / PIN / Map Code, exact spacing as printed
                 on the tax bill (e.g. "10046 03008")
        year   : tax year to filter by (None = Any)

        Returns:
        {
            "county_url": ...,
            "parcel_query": ...,
            "year_filter": ...,
            "total_records": N,
            "records": [ {property_id, owner, parcel_address, city,
                          tax_year, detail: {...}}, ... ]
        }
        """
        base = self._resolve_base(county)
        search_html = self._post_search(base, parcel, year)
        rows = self._parse_results(search_html)

        if not rows:
            return {
                "county_url": base,
                "parcel_query": parcel,
                "year_filter": year,
                "total_records": 0,
                "records": [],
            }

        records = []
        for row in rows:
            record = dict(row)
            link = record.pop("_row_link", None)
            if link:
                try:
                    detail_html = self._open_detail(base, search_html, link)
                    record["detail"] = self._extract_detail_fields(detail_html)
                except requests.RequestException as e:
                    record["detail"] = None
                    record["detail_error"] = str(e)
            else:
                record["detail"] = None
            records.append(record)

        return {
            "county_url": base,
            "parcel_query": parcel,
            "year_filter": year,
            "total_records": len(records),
            "records": records,
        }


# ==========================================================================
# Example usage (runs if this file is executed directly)
# ==========================================================================
if __name__ == "__main__":
    scraper = IasWorldScraper()
    result = scraper.search(county="chatham", parcel="10046 03008", year=2021)
    print(json.dumps(result, indent=2, ensure_ascii=False))
