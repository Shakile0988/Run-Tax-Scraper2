"""
Command-line interface for the tax scraper.

Usage:
    python cli.py --county brantley --parcel "B065 172" --year 2019
    python cli.py --county-url https://www.chattoogatax.com --parcel "63B--90"
    python cli.py --county hall --parcel "12-345-6" --year 2024 --json --out result.json
"""

import argparse
import json
import sys

from tax_scraper import TaxScraper, KNOWN_COUNTIES, TaxScraperError


def print_human(result: dict):
    print(f"\nCounty : {result['county_url']}")
    print(f"GUID   : {result['guid']}")
    print(f"Searched: '{result['parcel_query']}'"
          + (f" | Year: {result['year_filter']}" if result['year_filter'] else " | All years"))
    print(f"Total records: {result['total_records']}\n")

    if not result["records"]:
        print("No records found.")
        return

    for i, rec in enumerate(result["records"], 1):
        print(f"--- Record {i} ---")
        print(f"  Year          : {rec['year']}   (Bill #{rec['bill_number']})")
        print(f"  Parcel        : {rec['parcel_number']}")
        print(f"  Owner         : {rec['owner_name']}")
        print(f"  Owner address : {rec['owner_address']}")
        print(f"  Situs address : {rec['situs_address']}")
        print(f"  Description   : {rec['description']}  ({rec['acres']} acres)")
        print(f"  Fair Market   : ${rec['fair_market_value']:,}")
        print(f"  Assessed      : ${rec['assessed_value']:,}")
        print(f"  Base Tax      : ${rec['base_tax']:,}")
        print(f"  Penalty/Interest: ${rec['penalty']} / ${rec['interest']}")
        print(f"  Amount Due    : ${rec['amount_due']:,}")
        print(f"  Payment Status: {rec['payment_status']}"
              + (f" (date: {rec['payment_date']})" if rec['payment_date'] else ""))
        print(f"  Delinquent?   : {'Yes' if rec['is_delinquent'] else 'No'}")
        print(f"  Tax Entities  :")
        for te in rec["tax_entities"]:
            print(f"      - {te['name']:<30} millage={te['millage_rate']:<8} "
                  f"net_tax=${te['net_tax']}")
        print()


def main():
    parser = argparse.ArgumentParser(description="Georgia County Tax Scraper")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--county", help=f"Known county shortcut name: {', '.join(KNOWN_COUNTIES)}")
    group.add_argument("--county-url", help="Direct county site URL, e.g. https://www.brantleytax.com")

    parser.add_argument("--parcel", required=True, help="Parcel ID, e.g. 'B065 172'")
    parser.add_argument("--year", type=int, default=None, help="Specific year (omit for all years)")
    parser.add_argument("--guid", default=None, help="Known TENANT_ID (skips auto-detect)")
    parser.add_argument("--json", action="store_true", help="Print raw JSON to stdout")
    parser.add_argument("--out", default=None, help="Write JSON result to this file path")

    args = parser.parse_args()

    scraper = TaxScraper()

    if args.county:
        info = scraper.search_by_name(args.county)
        if not info:
            print(f"County '{args.county}' not found. Available: {list(KNOWN_COUNTIES)}", file=sys.stderr)
            sys.exit(1)
        county_url = info["url"]
        guid = args.guid or info.get("guid")
    else:
        county_url = args.county_url
        guid = args.guid

    try:
        result = scraper.search(
            county_url=county_url,
            parcel=args.parcel,
            year=args.year,
            guid=guid,
        )
    except TaxScraperError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))

    if not args.json:
        print_human(result)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"\nResult saved to: {args.out}")


if __name__ == "__main__":
    main()
