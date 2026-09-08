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
from urllib.parse import unquote, urljoin

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
    def _compact_html(html: str, max_len: int = 8000) -> str:
        """Strips huge VIEWSTATE/EVENTVALIDATION blobs and irrelevant
        header/footer boilerplate out of the HTML so a diagnostic dump
        stays readable -- keeps just the <form>...</form> body, which is
        where hidden fields / buttons / onclick JS actually live."""
        html = re.sub(
            r'(name="__VIEWSTATE"[^>]*value=")[^"]*(")',
            r"\1...TRUNCATED...\2", html,
        )
        html = re.sub(
            r'(name="__EVENTVALIDATION"[^>]*value=")[^"]*(")',
            r"\1...TRUNCATED...\2", html,
        )
        m = re.search(r"<form\b.*?</form>", html, re.I | re.S)
        if m:
            html = m.group(0)
        return html[:max_len]

    def _accept_disclaimer(self, base: str, disclaimer_html: str) -> None:
        """iasWorld shows a one-time Disclaimer.aspx interstitial for
        sessions that haven't accepted it yet (no DISCLAIMER cookie). POST
        the same 'Agree' click a browser would send; the resulting cookie
        (set on this POST's response) is what makes the real search page
        available on the next GET."""
        soup = BeautifulSoup(disclaimer_html, "html.parser")
        form = soup.find("form")
        action = form.get("action") if form else None
        if not action:
            raise IasWorldError(
                "Disclaimer page found but no <form action> to submit."
            )
        disclaimer_url = urljoin(f"{base}/search/", action)
        hidden = self._extract_hidden_fields(disclaimer_html)
        hd_url_el = soup.find("input", {"name": "hdURL"})
        form_data = {
            "__VIEWSTATE": hidden.get("__VIEWSTATE", ""),
            "__VIEWSTATEGENERATOR": hidden.get("__VIEWSTATEGENERATOR", ""),
            "__EVENTVALIDATION": hidden.get("__EVENTVALIDATION", ""),
            "hdURL": hd_url_el.get("value", "") if hd_url_el else "",
            "action": "",
            "btAgree": "Agree",
        }
        resp = self.session.post(
            disclaimer_url, data=form_data, timeout=self.timeout,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": f"{base}{SEARCH_PATH}",
            },
            allow_redirects=True,
        )
        if resp.status_code >= 400:
            raise IasWorldError(
                f"Disclaimer-accept POST {disclaimer_url} -> {resp.status_code}"
            )

    def _get_search_page(self, base: str) -> str:
        url = f"{base}{SEARCH_PATH}"
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code >= 400:
            raise IasWorldError(
                f"GET {url} -> {resp.status_code}. Body[:500]: {resp.text[:500]!r}"
            )
        html = resp.text

        if BeautifulSoup(html, "html.parser").find(id="btAgree"):
            self._accept_disclaimer(base, html)
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code >= 400:
                raise IasWorldError(
                    f"GET (post-disclaimer) {url} -> {resp.status_code}. "
                    f"Body[:500]: {resp.text[:500]!r}"
                )
            html = resp.text

        return html

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

        if not BeautifulSoup(html, "html.parser").find("input", {"name": "inpParid"}):
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
        """Datalet detail pages render fields in two different table
        shapes (confirmed against a real Chatham Datalet page):

          Shape A -- side-by-side label/value pairs in the same <tr>:
              <td class="DataletSideHeading">Status</td>
              <td class="DataletData">ACTIVE</td>
          Used for: Status, Alternate ID, Bill #, Tax District/Description,
          Legal Description, Appeal Status, Property Class, Mortgage
          Company, Exemptions.

          Shape B -- a header <tr> of column labels followed by a sibling
          <tr> of same-column-index data cells:
              <tr><td class="DataletTopHeading">Current Owner</td>...</tr>
              <tr><td class="DataletData">OHMER LAUREN B</td>...</tr>
          Used for: Parcel Status / Deferral Exist / Total Millage Rate,
          and Current Owner / Co-Owner / Care Of / Mailing Address.
        """
        soup = BeautifulSoup(html, "html.parser")

        def norm(s: str) -> str:
            return re.sub(r"\s+", " ", s).strip()

        def grab_side(label: str) -> Optional[str]:
            for td in soup.find_all("td", class_="DataletSideHeading"):
                if norm(td.get_text(" ", strip=True)) == label:
                    sib = td.find_next_sibling("td", class_="DataletData")
                    if sib:
                        val = sib.get_text(strip=True)
                        return val or None
            return None

        def grab_column(label: str) -> Optional[str]:
            for tr in soup.find_all("tr"):
                headers = tr.find_all("td", class_="DataletTopHeading")
                if not headers:
                    continue
                texts = [norm(h.get_text(" ", strip=True)) for h in headers]
                if label in texts:
                    idx = texts.index(label)
                    data_row = tr.find_next_sibling("tr")
                    if data_row:
                        data_cells = data_row.find_all("td", class_="DataletData")
                        if idx < len(data_cells):
                            val = data_cells[idx].get_text(strip=True)
                            return val or None
            return None

        parid = None
        m = re.search(r"PARID:\s*([^<\n]+)", html)
        if m:
            parid = m.group(1).strip()

        return {
            "parid": parid,
            "status": grab_side("Status"),
            "alternate_id": grab_side("Alternate ID"),
            "bill_number": grab_side("Bill #"),
            "tax_district": grab_side("Tax District/Description") or grab_side("Tax District"),
            "legal_description": grab_side("Legal Description"),
            "appeal_status": grab_side("Appeal Status"),
            "property_class": grab_side("Property Class"),
            "mortgage_company": grab_side("Mortgage Company"),
            "exemptions": grab_side("Exemptions"),
            "parcel_status": grab_column("Parcel Status"),
            "deferral_exist": grab_column("Deferral Exist"),
            "millage_rate": grab_column("Total Millage Rate"),
            "owner_name": grab_column("Current Owner"),
            "co_owner": grab_column("Co-Owner"),
            "care_of": grab_column("Care Of"),
            "mailing_address": grab_column("Mailing Address"),
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
