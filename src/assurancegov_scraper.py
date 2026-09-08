"""
AssuranceWeb Property platform scraper (Phase 2 counties).

This is a completely separate module from tax_scraper.py (Wildfire/Catalis
platform, Phase 1). Nothing here touches or imports Phase 1 logic, so the
6 already-working counties (brantley, chattooga, dawson, hall, pierce,
union) are not affected in any way.

Platform covered here ("AssuranceWeb Property"):
    walkerproperty.assurancegov.com
    forsythproperty.assurancegov.com
    libertyproperty.assurancegov.com
    pickensproperty.assurancegov.com
    quitmanproperty.assurancegov.com
    carrollproperty.assurancegov.com

How it works (reverse-engineered from browser DevTools):
    1. GET /Property/Search
       -> gives us cookies (.AspNetCore.Session, ARRAffinity, etc.) and an
          anti-forgery token embedded in a hidden form field:
          <input name="__RequestVerificationToken" value="...">
    2. POST /Property/Search (same session/cookies) with form data:
          PropertySearchYear, PropertySearchType, UseContains,
          SearchCriteria.Criteria1, SearchCriteria.Criteria2,
          SelectedParcels, __RequestVerificationToken
       Header: X-Requested-With: XMLHttpRequest
       -> Server responds with a small JSON ack, e.g. {"result": true,
          "data": null, "message": ""}. This does NOT contain the actual
          results -- the server stores the search results in the session.
    3. GET /Property/Search again (same session)
       -> Now the page HTML contains a Kendo Grid whose dataSource.data.Data
          is embedded inline as JSON in a <script> tag. We parse that.
    4. Each row already contains "ParcelInfoID" and "Account", which is all
       that's needed to build the details page URL directly, without ever
       clicking "DETAILS" in a browser:
          /Property/Summary?pcliID={ParcelInfoID}&pan={Account}
    5. GET that details URL and parse owner / mailing address / tax info /
       tax history out of the page.

Note: step 5's parser is best-effort (built from a screenshot of the
details page, not its raw HTML). If it doesn't extract cleanly on first
run, "raw_html_snippet" is still included in the result so nothing is lost
-- send that back for a quick regex fix.
"""

import re
import json
import requests
from urllib.parse import quote
from bs4 import BeautifulSoup


ASSURANCE_COUNTIES = {
    "walker": "walkerproperty.assurancegov.com",
    "forsyth": "forsythproperty.assurancegov.com",
    "liberty": "libertyproperty.assurancegov.com",
    "pickens": "pickensproperty.assurancegov.com",
    "quitman": "quitmanproperty.assurancegov.com",
    "carroll": "carrollproperty.assurancegov.com",
}

# Maps our --search-type CLI values to the site's PropertySearchType values
# (seen so far: "parcel"; others assumed from the radio button labels).
SEARCH_TYPE_MAP = {
    "name": "name",
    "bill": "billnum",
    "company": "company",
    "parcel": "parcel",
    "account": "account",
    "address": "address",
}


class AssuranceGovError(Exception):
    """Raised for AssuranceWeb-platform-specific failures."""
    pass


class AssuranceGovScraper:
    """Scraper for the AssuranceWeb Property platform (Phase 2 counties)."""

    def __init__(self, timeout=None):
        self.session = requests.Session()
        # Same policy as Phase 1: no client-side timeout, let the server
        # take as long as it needs (set via CLI/env if ever needed).
        self.timeout = timeout
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _base_url(self, county_key_or_url_or_host):
        """Accept a known shortcut key (case-insensitive), a full URL, or a bare host."""
        key = county_key_or_url_or_host.strip().lower()
        if key in ASSURANCE_COUNTIES:
            host = ASSURANCE_COUNTIES[key]
        else:
            host = (
                county_key_or_url_or_host
                .replace("https://", "")
                .replace("http://", "")
                .split("/")[0]
            )
        return f"https://{host}"

    def _get_antiforgery_token(self, base_url):
        resp = self.session.get(f"{base_url}/Property/Search", timeout=self.timeout)
        resp.raise_for_status()
        match = re.search(
            r'name="__RequestVerificationToken"[^>]*value="([^"]+)"',
            resp.text,
        )
        if not match:
            raise AssuranceGovError(
                "Could not find __RequestVerificationToken on the search page. "
                "Site markup may have changed."
            )
        return match.group(1)

    def _parse_grid(self, html, base_url):
        """Extract the Kendo grid's embedded row data from the search page."""
        start = html.find('"Data"')
        if start == -1:
            return []

        array_start = html.find('[', start)
        if array_start == -1:
            return []

        # Balance brackets manually -- regex can't reliably match nested
        # JSON, and the row objects can contain '[' / ']' inside HTML
        # snippets (e.g. the Description field has inline <span> markup).
        depth = 0
        i = array_start
        in_string = False
        escape = False
        while i < len(html):
            ch = html[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == '[':
                    depth += 1
                elif ch == ']':
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
            i += 1
        array_text = html[array_start:i]

        try:
            rows = json.loads(array_text)
        except json.JSONDecodeError as e:
            raise AssuranceGovError(f"Could not parse grid JSON: {e}")

        records = []
        for row in rows:
            parcel_info_id = row.get("ParcelInfoID")
            account = (row.get("Account") or "").strip()
            records.append({
                "parcel_info_id": parcel_info_id,
                "account": account,
                "parcel_number": (row.get("ParcelNumberFormatted") or row.get("ParcelNumber") or "").strip(),
                "owner_name": (row.get("FullName") or "").strip(),
                "situs_address": (row.get("PhysAddress") or "").strip(),
                "bill_number": (row.get("BillNum") or "").strip(),
                "year": row.get("tyYEAR"),
                "billing_year": row.get("tyYEAR_BILLING"),
                "total_tax": row.get("TotalTax"),
                "balance_due": row.get("BalanceDue"),
                "amount_paid": row.get("AmountPaid"),
                "tax_type": row.get("TaxType"),
                "details_url": (
                    f"{base_url}/Property/Summary?pcliID={parcel_info_id}&pan={quote(account)}"
                    if parcel_info_id is not None else None
                ),
            })
        return records

    def _label_value(self, soup, label):
        """Find a bold/label text node and return the text right after it."""
        node = soup.find(string=re.compile(rf"^\s*{re.escape(label)}\s*$", re.I))
        if node is None:
            return None
        parent = node.parent
        nxt = parent.find_next_sibling()
        if nxt is not None and nxt.get_text(strip=True):
            return nxt.get_text(" ", strip=True)
        # Fallback: text right after the label within the same parent
        tail = node.next_sibling
        if tail and str(tail).strip():
            return str(tail).strip()
        return None

    def _table_after_label(self, soup, must_contain):
        """Find the <table> whose header/first row text contains all of the
        given substrings (case-insensitive) -- used to locate the Parcel
        Info / Tax Information / Tax History tables regardless of their
        exact position on the page."""
        for table in soup.find_all("table"):
            text = table.get_text(" ", strip=True).upper()
            if all(s.upper() in text for s in must_contain):
                return table
        return None

    def _parse_kv_table(self, table):
        """Parse a 2-column label/value table (e.g. Parcel Info block) into
        a flat dict. Empty spacer rows are skipped."""
        result = {}
        if table is None:
            return result
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            label = cells[0].get_text(" ", strip=True)
            value = cells[1].get_text(" ", strip=True)
            if not label:
                continue
            result[label] = value
        return result

    def _parse_rows_table(self, table):
        """Parse a header-row + data-rows table (Tax Information / Tax
        History) into a list of {header: value} dicts."""
        rows = []
        if table is None:
            return rows
        trs = table.find_all("tr")
        if not trs:
            return rows
        header_cells = trs[0].find_all(["th", "td"])
        headers = [c.get_text(" ", strip=True) for c in header_cells]
        for tr in trs[1:]:
            cells = tr.find_all(["td", "th"])
            if not cells or len(cells) != len(headers):
                continue
            row = {headers[i]: cells[i].get_text(" ", strip=True) for i in range(len(headers))}
            # Skip fully-empty rows (spacer rows)
            if any(v for v in row.values()):
                rows.append(row)
        return rows

    def get_details(self, base_url, parcel_info_id, account):
        """Fetch and parse the /Property/Summary details page.

        Confirmed structure (2026-09-08, live fetch of a real Liberty
        County parcel): three plain HTML <table> blocks on the page --
        Parcel Info (2-column label/value), Tax Information (header row +
        one row per bill/year), Tax History (header row + one row per
        year). No Kendo grid / embedded JSON here, unlike the search page.
        """
        account = (account or "").strip()
        url = f"{base_url}/Property/Summary?pcliID={parcel_info_id}&pan={quote(account)}"
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        page_text = soup.get_text(" ", strip=True)
        if "Error!" in page_text and "problem retrieving" in page_text:
            return {"details_url": url, "error": "Details page returned an error for this parcel/account."}

        parcel_table = self._table_after_label(soup, ["BILL NUMBER", "OWNER"])
        tax_info_table = self._table_after_label(soup, ["TAXES", "BALANCE DUE"])
        tax_history_table = self._table_after_label(soup, ["APPRAISED", "ASSESSED"])

        kv = self._parse_kv_table(parcel_table)

        total_due_match = re.search(r"Total Due:\s*\$?\s*([\d,]+\.\d{2})", page_text)
        last_payment_match = re.search(r"LAST PAYMENT DATE\s*([\d/]+)", page_text, re.I)
        paid_by_match = re.search(r"PAID BY\s*([A-Z0-9 ,.'&-]+?)(?:\s{2,}|$|©)", page_text, re.I)

        return {
            "details_url": url,
            "bill_number": kv.get("BILL NUMBER"),
            "parcel": kv.get("PARCEL"),
            "account_number": kv.get("ACCOUNT NUMBER"),
            "owner": kv.get("OWNER"),
            "mailing_address": kv.get("MAILING ADDRESS"),
            "property_address": kv.get("PROPERTY ADDRESS"),
            "legal_description": kv.get("LEGAL DESCRIPTION"),
            "exempt_code": kv.get("EXEMPT CODE"),
            "tax_district": kv.get("TAX DISTRICT"),
            "tax_information": self._parse_rows_table(tax_info_table),
            "total_due": total_due_match.group(1) if total_due_match else None,
            "last_payment_date": last_payment_match.group(1) if last_payment_match else None,
            "paid_by": paid_by_match.group(1).strip() if paid_by_match else None,
            "tax_history": self._parse_rows_table(tax_history_table),
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def _find_radio_values(self, html, name_attr="PropertySearchType"):
        """Debug helper: find the actual value="" attributes for a radio
        group by name, so we can confirm PropertySearchType's real values
        (e.g. is it "parcel" or "Parcel"?) instead of guessing."""
        pattern = re.compile(
            rf'<input[^>]*name="{re.escape(name_attr)}"[^>]*value="([^"]*)"',
            re.IGNORECASE,
        )
        return pattern.findall(html)

    def search(self, county, parcel, year=None, search_type="parcel", fetch_details=True):
        base_url = self._base_url(county)
        search_url = f"{base_url}/Property/Search"

        get_resp = self.session.get(search_url, timeout=self.timeout)
        get_resp.raise_for_status()

        token_match = re.search(
            r'name="__RequestVerificationToken"[^>]*value="([^"]+)"',
            get_resp.text,
        )
        if not token_match:
            raise AssuranceGovError(
                "Could not find __RequestVerificationToken on the search page."
            )
        token = token_match.group(1)
        radio_values = self._find_radio_values(get_resp.text)

        # NOTE: the search form uses old-style ASP.NET MVC "Ajax.BeginForm"
        # (data-ajax="true", data-ajax-mode="replace",
        # data-ajax-update="#pt-results-panel-data"). That means the POST
        # response itself IS the results-panel HTML (with the grid + data
        # embedded), NOT a small JSON ack, and NOT something stored
        # server-side for a later GET to pick up. Confirmed from the raw
        # page source (2026-09-08).
        form_data = {
            "PropertySearchYear": str(year) if year else "0",  # "0" = the kendo dropdown's "-- ANY --" value
            "PropertySearchType": SEARCH_TYPE_MAP.get(search_type, search_type),
            "UseContains": "False",
            "SearchCriteria.Criteria1": parcel,
            "SearchCriteria.Criteria2": "",
            "SelectedParcels": "",
            "__RequestVerificationToken": token,
        }
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": search_url,
            "Origin": base_url,
        }

        post_resp = self.session.post(
            search_url,
            data=form_data,
            headers=headers,
            timeout=self.timeout,
        )
        post_resp.raise_for_status()

        # Primary path: parse the grid directly out of the POST response.
        records = self._parse_grid(post_resp.text, base_url)

        followup_status = None
        followup_len = None
        if not records:
            # Fallback in case this particular deployment DOES store
            # results server-side (behavior can vary by county instance).
            grid_resp = self.session.get(search_url, timeout=self.timeout)
            grid_resp.raise_for_status()
            followup_status = grid_resp.status_code
            followup_len = len(grid_resp.text)
            records = self._parse_grid(grid_resp.text, base_url)

        debug = {
            "search_type_sent": form_data["PropertySearchType"],
            "year_sent": form_data["PropertySearchYear"],
            "radio_values_found_on_page": radio_values,
            "post_status": post_resp.status_code,
            "post_content_type": post_resp.headers.get("Content-Type"),
            "post_response_snippet": post_resp.text[:800],
            "post_html_contains_Data_key": '"Data"' in post_resp.text,
            "followup_get_status": followup_status,
            "followup_html_length": followup_len,
        }

        result = {
            "county_url": base_url,
            "parcel_query": parcel,
            "year_filter": year,
            "total_records": len(records),
            "records": records,
        }
        if not records:
            result["message"] = ack.get("message") or "No records parsed."
            result["debug"] = debug

        if fetch_details:
            for rec in records:
                if rec["parcel_info_id"] is not None:
                    try:
                        rec["details"] = self.get_details(
                            base_url, rec["parcel_info_id"], rec["account"]
                        )
                    except Exception as e:
                        rec["details"] = {"error": str(e)}

        return result
