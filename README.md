# Lube City — Competitor Intelligence

Automated weekly scraper pipeline that tracks competitor promotions and Google Ads for Lube City across Edmonton, Calgary, and Grande Prairie. Results are pushed to a live Google Sheet and a deployed dashboard.

---

## Live Dashboard

**GitHub Pages:** `https://<your-org>.github.io/Competitor-Intelligence/`

Data auto-refreshes every Monday. Trigger a manual refresh anytime from the **Actions** tab.

---

## One-Time Setup

### 1. Add GitHub Secrets

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Description |
|---|---|
| `FIRECRAWL_API_KEY` | Firecrawl API key (scrapes JS-rendered pages) |
| `SERPAPI_KEY` | SerpAPI key (Google Reviews, AI Overview) |
| `ANTHROPIC_API_KEY` | Anthropic API key (promo text cleaning) |
| `GOOGLE_CLOUD_VISION_API_KEY` | Google Cloud Vision key (OCR fallback) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full contents of `service_account.json` |

> `ZENROWS_API_KEY` and `SCRAPERAPI_KEY` are optional — leave blank if not using.

### 2. Enable GitHub Pages

**Settings → Pages → Source:** `main` branch → `/public` folder → **Save**

### 3. Place credentials locally

Copy `.env.example` to `.env` and fill in your API keys:
```bash
cp .env.example .env
```

Place `service_account.json` (Google service account) in the project root.

---

## How It Runs

```
Every Monday 2 AM UTC  (or: Actions → Weekly Competitor Intelligence → Run workflow)
  ↓
All scrapers run (promos for all 3 cities + Google Ads)
  ↓
Promotions merged and pushed to Google Sheets
  ↓
JSON data files committed to repo
  ↓
GitHub Pages dashboard auto-updated
```

---

## Local Development

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run scrapers
python run_merger_v2.py --no-push     # promotions (all cities)
python run_google_ads.py --no-push    # Google Ads

# 3. Serve the dashboard (from project root)
python3 -m http.server 8080

# 4. Open
open http://localhost:8080/public/
```

The dashboard auto-fetches the local JSON files — no commit needed.

---

## Competitors Tracked

| Competitor | Cities |
|---|---|
| Midas | Edmonton, Calgary, Grande Prairie |
| Lube Town | Edmonton, Calgary, Grande Prairie |
| Jiffy Lube | Edmonton, Calgary, Grande Prairie |
| Great Canadian Oil Change | Edmonton, Calgary, Grande Prairie |
| Quick Lane | Edmonton, Calgary, Grande Prairie |
| Valvoline Express Care | Edmonton |
| Econo Lube | Edmonton |
| LubeFx Plus | Edmonton, Calgary |
| Mobil 1 Lube Express | Edmonton, Calgary, Grande Prairie |
| Mr. Lube + Tires | Edmonton, Calgary |

---

## Google Sheet

**Sheet ID:** `11e3ErdYFIQ3MIOEpnLEGS4MH0s2AbSIhiQsgzQG_m88`

| Tab | Contents |
|---|---|
| Edmonton Promos | Edmonton promotions (15 columns) |
| Calgary Promos | Calgary promotions (15 columns) |
| Grande Prairie Promos | Grande Prairie promotions (15 columns) |
| Advertisements | Google Ads Transparency Center data (7 columns) |

The service account `sheet-writer@lubecity-competitor-intel.iam.gserviceaccount.com` must be an **Editor** on the spreadsheet.

---

## Project Structure

```
├── public/
│   ├── index.html              # Live dashboard (GitHub Pages entry point)
│   └── lube-city-logo.webp     # Logo
├── app/
│   ├── scrapers/               # Individual competitor scrapers
│   ├── mergers/                # Promotions merger + dedup logic
│   ├── sheets/                 # Google Sheets writer
│   └── extractors/             # Firecrawl, OCR, PDF, image tools
├── data/
│   ├── ads/google_ads.json     # Latest Google Ads data (committed by CI)
│   └── sheets_ready/           # Latest merged promos (committed by CI)
├── run_*_v2.py                 # Scraper entry points
├── run_merger_v2.py            # Merge + push to Sheets
├── run_google_ads.py           # Google Ads scraper + push
├── .github/workflows/weekly.yml # CI/CD pipeline
├── .env.example                # Environment variable template
└── requirements.txt            # Python dependencies
```
