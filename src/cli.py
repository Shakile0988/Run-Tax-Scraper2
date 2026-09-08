"""
Command-line interface for the tax scraper.

Usage:
    python cli.py --county brantley --parcel "B065 172" --year 2019
    python cli.py --county-url https://www.chattoogatax.com --parcel "63B--90"
    python cli.py --county hall --parcel "12-345-6" --year 2024 --json --out result.json

    # Phase 2 (AssuranceWeb Property platform) -- auto-detected from county name:
    python cli.py --county walker --parcel "0192 132" --year 2025 --json --out result.json
"""

import argparse
import json
import sys

from tax_scraper import TaxScraper, KNOWN_COUNTIES, TaxScraperError
from assurancegov_scraper import AssuranceGovScraper, AssuranceGovError, ASSURANCE_COUNTIES
from iasworld_scraper import IasWorldScraper, IasWorldError, IASWORLD_COUNTIES


def print_human(result: dict):
    print(f"\nCounty : {result['county_url']}")
    if "guid" in result:
        print(f"GUID   : {result['guid']}")
    print(f"Searched: '{result['parcel_query']}'"
          + (f" | Year: {result['year_filter']}" if result['year_filter'] else " | All years"))
    print(f"Total records: {result['total_records']}\n")

    if not result["records"]:
        print("No records found.")
        return

    for i, rec in enumerate(result["records"], 1):
        print(f"--- Record {i} ---")
        print(json.dumps(rec, indent=2, ensure_ascii=False))
        print()


def run_phase1(args):
    """Wildfire/Catalis platform (brantley, chattooga, dawson, hall, pierce, union).
    UNCHANGED from before -- do not modify this function's logic."""
    scraper = TaxScraper()

    if args.county:
        info = scraper.search_by_name(args.county)
        if not info:
            raise TaxScraperError(f"County '{args.county}' not found. Available: {list(KNOWN_COUNTIES)}")
        county_url = info["url"]
        guid = args.guid or info.get("guid")
    else:
        county_url = args.county_url
        guid = args.guid

    return scraper.search(
        county_url=county_url,
        parcel=args.parcel,
        year=args.year,
        guid=guid,
    )


def run_phase2(args):
    """AssuranceWeb Property platform (walker, forsyth, liberty, pickens, quitman, carroll)."""
    scraper = AssuranceGovScraper()
    county = (args.county or args.county_url or "").strip()
    return scraper.search(
        county=county,
        parcel=args.parcel,
        year=args.year,
        search_type=args.search_type,
    )


def run_phase3(args):
    """iasWorld / Tyler Technologies platform (chatham, clayton, dekalb)."""
    scraper = IasWorldScraper()
    county = (args.county or args.county_url or "").strip()
    return scraper.search(
        county=county,
        parcel=args.parcel,
        year=args.year,
    )


def main():
    parser = argparse.ArgumentParser(description="Georgia County Tax Scraper")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--county",
        help=(
            "Known county shortcut name. "
            f"Phase 1 (Wildfire): {', '.join(KNOWN_COUNTIES)}. "
            f"Phase 2 (AssuranceWeb): {', '.join(ASSURANCE_COUNTIES)}. "
            f"Phase 3 (iasWorld): {', '.join(IASWORLD_COUNTIES)}."
        ),
    )
    group.add_argument("--county-url", help="Direct county site URL or host")

    parser.add_argument("--parcel", required=True, help="Parcel ID, e.g. 'B065 172'")
    parser.add_argument("--year", type=int, default=None, help="Specific year (omit for all years)")
    parser.add_argument("--guid", default=None, help="Known TENANT_ID (Phase 1 only, skips auto-detect)")
    parser.add_argument(
        "--search-type", default="parcel",
        choices=["name", "bill", "company", "parcel", "account", "address"],
        help="Search-by field (Phase 2 / AssuranceWeb only, default: parcel)",
    )
    parser.add_argument(
        "--platform", default=None, choices=["wildfire", "assurance", "iasworld"],
        help="Force a platform instead of auto-detecting from --county name",
    )
    parser.add_argument("--json", action="store_true", help="Print raw JSON to stdout")
    parser.add_argument("--out", default=None, help="Write JSON result to this file path")

    args = parser.parse_args()

    # Decide which platform to use. Auto-detect from the known county-name
    # lists so existing Phase 1 calls behave exactly as before; --platform
    # can override this if a URL is passed directly instead of a shortcut.
    # Case-insensitive so "Walker", "WALKER", "walker" all work the same.
    county_key = args.county.strip().lower() if args.county else None

    if args.platform == "assurance":
        phase = 2
    elif args.platform == "iasworld":
        phase = 3
    elif args.platform == "wildfire":
        phase = 1
    elif county_key and county_key in IASWORLD_COUNTIES:
        phase = 3
    elif county_key and county_key in ASSURANCE_COUNTIES:
        phase = 2
    else:
        phase = 1

    try:
        if phase == 3:
            result = run_phase3(args)
        elif phase == 2:
            result = run_phase2(args)
        else:
            result = run_phase1(args)
        result["success"] = True
    except (TaxScraperError, AssuranceGovError, IasWorldError) as e:
        result = {"success": False, "error": str(e)}
    except Exception as e:
        # Production-grade graceful failure: never let one county's crash
        # take down the whole batch/pipeline. Always write a JSON result.
        result = {"success": False, "error": f"{type(e).__name__}: {e}"}

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        if result.get("success"):
            print_human(result)
        else:
            print(f"Error: {result.get('error')}", file=sys.stderr)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"\nResult saved to: {args.out}")

    # Always exit 0 so GitHub Actions doesn't mark the run "failed" --
    # result.json / stdout JSON already carries success:true/false.
    sys.exit(0)


if __name__ == "__main__":
    main()
