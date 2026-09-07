# Georgia County Tax Scraper

Scrapes property tax records from Georgia county tax commissioner websites
that run on the Catalis / Avalon / Sturgis "Wildfire" search platform.

## Supported counties (never removed, only added to)

| Shortcut    | URL                                     |
|-------------|------------------------------------------|
| `brantley`  | https://www.brantleytax.com              |
| `chattooga` | https://www.chattoogatax.com             |
| `dawson`    | https://www.dawsoncountytax.com          |
| `hall`      | https://hallcountytax.org                |
| `pierce`    | https://piercegatax.com                  |
| `union`     | https://www.uniongatax.com               |

If a county isn't in this list, you can still use it directly with
`--county-url <site>` — the scraper will auto-detect the site's TENANT_ID
(GUID) from its HTML.

## How it works

1. Fetches the county's `taxes.html` page and extracts the site's
   TENANT_ID (a GUID) from the HTML source.
2. Calls the CloudFront JSON API:
   `POST https://d1ebsyxxbc7tep.cloudfront.net/data/{GUID}/Wildfire/Records`
   with the parcel number and (optionally) a year filter.
3. Parses the JSON response into a clean record: owner, address, parcel,
   assessed value, tax breakdown, payment status, amount due, etc.

## Repo structure

```
.
├── .github/workflows/run_scraper.yml   # GitHub Actions workflow
├── src/
│   ├── tax_scraper.py                  # Core scraping logic
│   └── cli.py                          # Command-line interface
├── requirements.txt
└── README.md
```

## Running from GitHub (no local setup needed)

1. Push this repo to GitHub.
2. Go to the **Actions** tab → select **"Run Tax Scraper"** → click
   **"Run workflow"**.
3. Fill in the form:
   - `county`: one of `brantley`, `chattooga`, `dawson`, `hall`, `pierce`,
     `union` (leave blank if using `county_url` instead)
   - `county_url`: a direct site URL (only needed if `county` is blank)
   - `parcel`: the parcel ID to search, e.g. `B065 172`
   - `year`: optional — leave blank to get all years
4. Click **Run workflow**. When it finishes, open the run and download the
   **`tax-scraper-result`** artifact (a `result.json` file), or just read
   the **"Print result to log"** step for the output directly.

## Triggering from n8n (webhook)

Once you've confirmed the GitHub Actions run works, you can trigger it
from n8n using an **HTTP Request** node:

- **Method:** POST
- **URL:** `https://api.github.com/repos/<YOUR_USERNAME>/<YOUR_REPO>/dispatches`
- **Headers:**
  - `Authorization: Bearer <YOUR_GITHUB_PERSONAL_ACCESS_TOKEN>`
  - `Accept: application/vnd.github+json`
- **Body (JSON):**
  ```json
  {
    "event_type": "run-tax-scraper",
    "client_payload": {
      "county": "brantley",
      "county_url": "",
      "parcel": "B065 172",
      "year": "2019"
    }
  }
  ```

You'll need a GitHub Personal Access Token with `repo` scope (Settings →
Developer settings → Personal access tokens) to authenticate this request.

To read the result back in n8n, add a second step that polls the
**GitHub Actions API** for the latest run's status, then downloads the
`tax-scraper-result` artifact once the run completes.

## Running locally (optional)

```bash
pip install -r requirements.txt
cd src
python cli.py --county brantley --parcel "B065 172" --year 2019
```
