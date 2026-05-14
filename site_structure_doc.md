# Competitor Site Structure and Scraping Plan

This document defines the city-wise competitor coverage, source links, and preferred scraping strategy for promo/coupon extraction. Existing scraper code should not be removed yet; these notes are for phased updates and cleaner prompting.

## City Coverage Matrix

| Competitor | Calgary | Edmonton | Grande Prairie |
|---|---:|---:|---:|
| Mr. Lube + Tires | Yes | Yes | No |
| Jiffy Lube | Yes | Yes | Yes |
| Great Canadian Oil Change | Yes | Yes | Yes |
| Valvoline Express Care | No | Yes | No |
| Lube Town | Yes | No | No |
| Quick Lane Tire & Auto Center | No | Yes | Yes |
| Econo Lube | No | Yes | No |
| Pit Stop Oil Change | No | Yes | No |
| LubeFx Plus | No | Yes | No |
| Mobil 1 Lube Express | Yes | Yes | No |
| Midas | Yes | Yes | Yes |

## Standard Service Taxonomy

Every extracted promo should be classified into one of these services:

- Battery
- Oil Change
- Brake
- Tire Sales
- Tire Rotation
- Transmission Fluid
- Radiator Flush
- Fuel System Flush
- Other

## Standard Extraction Methods

Use only the method required for each competitor/site. Do not run all methods blindly across every site.

- `text`: visible page text / HTML / markdown extraction
- `image_ocr`: promo images or coupon banners downloaded and processed with OCR
- `pdf_text`: PDF text extraction
- `pdf_ocr`: OCR on image-based PDF pages
- `popup_interaction`: only for known popup/modal/load-more sites
- `ai_overview_fallback`: only when website scraping returns no reliable promo

## Output Sheet Columns

Current promo sheet columns should remain compatible:

| Column | Meaning |
|---|---|
| website | Competitor domain |
| page_url | Source page URL |
| business_name | Competitor/business name |
| google_reviews | Rating/count if available |
| service_name | Standard service name |
| promo_description | Clean customer-facing promo description |
| category | Standard service category |
| contact | Phone/address/contact if available |
| location | Store/city location |
| offer_details | Short offer summary |
| ad_title | Promo/ad/card title if available |
| ad_text | Full extracted text/OCR/ad copy |
| new_or_updated | New/updated/existing status |
| date_scraped | Scrape date |

Optional but recommended future columns:

- city
- store_name
- source_scope (`national`, `city`, `store`)
- extraction_method
- confidence
- needs_review

---

# Competitor Link Structure

## 1. Mr. Lube + Tires

**Cities:** Calgary, Edmonton  
**Structure:** One shared link. Offer appears as image; offer duration/validity is in nearby text.  
**Preferred extraction:** `image_ocr` + nearby `text`  
**Source scope:** national/shared

### Links

| Scope | City | URL | Notes |
|---|---|---|---|
| Shared | Calgary, Edmonton | https://www.mrlube.com/en/Services/tire-rebates-and-financing | Image offer plus text for duration/validity |

### Scraper Notes

- Extract image URLs from the page.
- OCR only promo/rebate-looking images.
- Also parse nearby text for validity, duration, and financing terms.
- Classify likely services as Tire Sales, Tire Rotation, Oil Change, or Other depending on offer text.

---

## 2. Jiffy Lube

**Cities:** Calgary, Edmonton, Grande Prairie  
**Structure:** National coupons page plus multiple city/store pages. Text + image.  
**Preferred extraction:** `text` + `image_ocr`  
**Source scope:** national + store

### National Coupon Link

| Scope | City | URL | Notes |
|---|---|---|---|
| National | All cities | https://www.jiffylubeservice.ca/coupons | Nationwide coupons; scrape once and attach to relevant city tabs |

### Calgary Store Links

| Store | City | URL |
|---|---|---|
| Calgary 16th Avenue NW | Calgary | https://www.jiffylubeservice.ca/oil-change-locations/calgary-16th-avenue-nw |
| Calgary 17th Ave SE | Calgary | https://www.jiffylubeservice.ca/oil-change-locations/calgary-17th-ave-se |
| Calgary Aviation Road | Calgary | https://www.jiffylubeservice.ca/oil-change-locations/calgary-aviation-road |
| Calgary Bowness | Calgary | https://www.jiffylubeservice.ca/oil-change-locations/calgary-bowness |
| Calgary Foothills | Calgary | https://www.jiffylubeservice.ca/oil-change-locations/calgary-foothills |
| Calgary Forest Lawn | Calgary | https://www.jiffylubeservice.ca/oil-change-locations/calgary-forest-lawn |
| Calgary Macleod | Calgary | https://www.jiffylubeservice.ca/oil-change-locations/calgary-macleod |
| Calgary Marlborough | Calgary | https://www.jiffylubeservice.ca/oil-change-locations/calgary-marlborough |
| Calgary McCall | Calgary | https://www.jiffylubeservice.ca/oil-change-locations/calgary-mccall |
| Calgary Nolan Hill | Calgary | https://www.jiffylubeservice.ca/oil-change-locations/calgary-nolan-hill |
| Calgary Sunridge | Calgary | https://www.jiffylubeservice.ca/oil-change-locations/calgary-sunridge |
| Calgary Symons Valley | Calgary | https://www.jiffylubeservice.ca/oil-change-locations/calgary-symons-valley |
| Calgary The District | Calgary | https://www.jiffylubeservice.ca/oil-change-locations/calgary-the-district |

### Edmonton Store Links

| Store | City | URL |
|---|---|---|
| Edmonton Beverly | Edmonton | https://www.jiffylubeservice.ca/oil-change-locations/edmonton-beverly |
| Edmonton Clareview | Edmonton | https://www.jiffylubeservice.ca/oil-change-locations/edmonton-clareview |
| Edmonton Downtown | Edmonton | https://www.jiffylubeservice.ca/oil-change-locations/edmonton-downtown |
| Edmonton Eaux Claires | Edmonton | https://www.jiffylubeservice.ca/oil-change-locations/edmonton-eaux-claires |
| Edmonton Granville | Edmonton | https://www.jiffylubeservice.ca/oil-change-locations/edmonton-granville |
| Edmonton Killarney | Edmonton | https://www.jiffylubeservice.ca/oil-change-locations/edmonton-killarney |
| Edmonton Manning Town Centre | Edmonton | https://www.jiffylubeservice.ca/oil-change-locations/edmonton-manning-town-centre |
| Edmonton North East | Edmonton | https://www.jiffylubeservice.ca/oil-change-locations/edmonton-north-east |
| Edmonton Northgate | Edmonton | https://www.jiffylubeservice.ca/oil-change-locations/edmonton-northgate |
| Edmonton North West | Edmonton | https://www.jiffylubeservice.ca/oil-change-locations/edmonton-north-west |
| Edmonton South Common | Edmonton | https://www.jiffylubeservice.ca/oil-change-locations/edmonton-south-common |
| Edmonton South Ellerslie | Edmonton | https://www.jiffylubeservice.ca/oil-change-locations/edmonton-south-ellersie |
| Edmonton Tamarack | Edmonton | https://www.jiffylubeservice.ca/oil-change-locations/edmonton-tamarack |
| Edmonton Terra Losa | Edmonton | https://www.jiffylubeservice.ca/oil-change-locations/edmonton-terra-losa |
| Edmonton Terwillegar | Edmonton | https://www.jiffylubeservice.ca/oil-change-locations/edmonton-terwillegar |
| Edmonton Whyte Ave | Edmonton | https://www.jiffylubeservice.ca/oil-change-locations/edmonton-whyte-ave |
| Edmonton Windermere Blvd | Edmonton | https://www.jiffylubeservice.ca/oil-change-locations/edmonton-windermere-blvd |

### Grande Prairie Store Links

| Store | City | URL |
|---|---|---|
| Grande Prairie | Grande Prairie | https://www.jiffylubeservice.ca/oil-change-locations/grande-prairie |
| Grande Prairie Highland | Grande Prairie | https://www.jiffylubeservice.ca/oil-change-locations/grande-prairie-highland |

### Scraper Notes

- Scrape national coupon page once; dedupe before copying rows to city tabs.
- Store pages should be used for store/location-specific offers and metadata.
- Avoid duplicate rows when the same national coupon appears across multiple store pages.

---

## 3. Great Canadian Oil Change

**Cities:** Calgary, Edmonton, Grande Prairie  
**Structure:** Main website and service pages. Mostly image/content driven.  
**Preferred extraction:** `image_ocr` + `text`  
**Source scope:** national/shared service pages

### Links

| Service Focus | URL |
|---|---|
| Home | https://www.gcoc.ca/ |
| Oil Change | https://www.gcoc.ca/oil-change/ |
| Battery | https://www.gcoc.ca/services/battery-service/ |
| Tire Services | https://www.gcoc.ca/services/tire-services/ |
| Fuel System Flush | https://www.gcoc.ca/services/fuel-system-cleaning/ |
| Transmission Fluid | https://www.gcoc.ca/services/transmission-fluid-service/ |
| Radiator Flush | https://www.gcoc.ca/services/radiator-fluid-service/ |

### Scraper Notes

- Service page URL should pre-seed expected service category.
- OCR promo-looking images only.
- Ignore generic service descriptions unless there is a rebate, coupon, discount, free service, financing, or limited-time offer.

---

## 4. Valvoline Express Care

**Cities:** Edmonton only  
**Structure:** One image + text coupons page and one text-based coupon page.  
**Preferred extraction:** `text` + `image_ocr`  
**Source scope:** city/store

### Links

| Page Type | City | URL | Notes |
|---|---|---|---|
| Rewards/Coupons | Edmonton | https://valvolineexpresscare.ca/rewards-coupons/ | Image + text |
| Coupon | Edmonton | https://valvolineedmonton.ca/coupon/ | Text based |

### Scraper Notes

- Treat both URLs as Edmonton-only.
- Dedupe if same coupon appears on both pages.

---

## 5. Lube Town

**Cities:** Calgary only  
**Structure:** Coupon page is text based.  
**Preferred extraction:** `text`  
**Source scope:** city/store

### Links

| Scope | City | URL | Notes |
|---|---|---|---|
| Coupon | Calgary | https://lubetown.com/coupon/ | Text based |

### Scraper Notes

- Text extraction should be enough.
- OCR should not run unless future page changes introduce image coupons.

---

## 6. Quick Lane Tire & Auto Center

**Cities:** Edmonton, Grande Prairie  
**Structure:** Coupons are split by service-specific pages.  
**Preferred extraction:** `text`  
**Source scope:** national/service-specific

### Links

| Service Focus | Cities | URL |
|---|---|---|
| Battery | Edmonton, Grande Prairie | https://www.quicklane.com/en-us/savings/coupons-offers-rebates/battery-coupons/ |
| Oil Change | Edmonton, Grande Prairie | https://www.quicklane.com/en-us/savings/coupons-offers-rebates/oil-change-coupons/ |
| Brake | Edmonton, Grande Prairie | https://www.quicklane.com/en-us/savings/coupons-offers-rebates/brake-coupons/ |
| Tire Sales | Edmonton, Grande Prairie | https://www.quicklane.com/en-us/savings/coupons-offers-rebates/tire-coupons/ |

### Scraper Notes

- Page URL should pre-seed expected service category.
- Assign output rows to both Edmonton and Grande Prairie unless page text/location logic says otherwise.

---

## 7. Econo Lube

**Cities:** Edmonton only  
**Structure:** No dedicated coupon section. One coupon appears on homepage.  
**Preferred extraction:** `text`  
**Source scope:** city/store

### Links

| Scope | City | URL | Notes |
|---|---|---|---|
| Homepage | Edmonton | https://econolube.ca/ | Homepage coupon only |

### Scraper Notes

- Focus on homepage text and visible offer blocks.
- Do not crawl the whole site unless homepage no longer exposes the coupon.

---

## 8. Pit Stop Oil Change

**Cities:** Edmonton target  
**Structure:** Company/site not verified yet. Similar named company found but not confirmed.  
**Preferred extraction:** manual verification first  
**Source scope:** unknown

### Links

| Status | Notes |
|---|---|
| Needs research | Correct Edmonton business website not confirmed |

### Scraper Notes

- Do not add automated scraping until official/accurate website is confirmed.
- Mark as `needs_review` in planning/status outputs.

---

## 9. LubeFx Plus

**Cities:** Edmonton only  
**Structure:** Coupon images; rewards page may be relevant.  
**Preferred extraction:** `image_ocr` + optional `text`  
**Source scope:** city/store

### Links

| Page Type | City | URL | Notes |
|---|---|---|---|
| Coupons | Edmonton | https://lubefx.com/lubefx-coupons/ | Image coupons |
| Rewards | Edmonton | https://lubefx.com/lubefx-rewards/ | Check whether rewards should be included |

### Scraper Notes

- OCR coupon images.
- Rewards page should only produce rows if there is a clear customer benefit/reward offer.
- Avoid treating generic loyalty text as a promo unless it has concrete benefit terms.

---

## 10. Mobil 1 Lube Express

**Cities:** Calgary, Edmonton  
**Structure:** Links not provided yet.  
**Preferred extraction:** pending URL discovery/input  
**Source scope:** unknown

### Links

| Status | Notes |
|---|---|
| Needs links | Calgary and Edmonton source URLs still needed |

### Scraper Notes

- Do not automate until source URLs are confirmed.
- Once links are provided, decide between text/image OCR based on page structure.

---

## 11. Midas

**Cities:** Calgary, Edmonton, Grande Prairie  
**Structure:** Store-specific offers pages, text based. Coupons may be same now but can differ later.  
**Preferred extraction:** `text`  
**Source scope:** store

### Edmonton Store Links

| Store | City | URL |
|---|---|---|
| Edmonton 13038 97th Street | Edmonton | https://www.midas.com/store/ab/edmonton/13038-97th-street-t5e-4c6/offers?shopnum=9351 |
| Edmonton 7120 82nd Ave NW | Edmonton | https://www.midas.com/store/ab/edmonton/7120-82nd-ave-nw-t6b-0g1/offers?shopnum=9360 |
| Edmonton 6316 104th Street | Edmonton | https://www.midas.com/store/ab/edmonton/6316-104th-street-t6h-2k9/offers?shopnum=9350 |

### Grande Prairie Store Links

| Store | City | URL |
|---|---|---|
| Grande Prairie 11211 100th Street | Grande Prairie | https://www.midas.com/store/ab/grande-prairie/11211-100th-street-t8v-6p7/offers?shopnum=9426 |

### Calgary Store Links

| Store | City | URL |
|---|---|---|
| Calgary 624 16th Avenue NW | Calgary | https://www.midas.com/store/ab/calgary/624-16th-avenue-n-w-t2m-0j7/offers?shopnum=9300 |
| Calgary 4121 Macleod Trail South | Calgary | https://www.midas.com/store/ab/calgary/4121-macleod-trail-south-t2g-2r6/offers?shopnum=9302 |
| Calgary 2529 17th Avenue SW | Calgary | https://www.midas.com/store/ab/calgary/2529-17th-avenue-s-w-t3e-0a2/offers?shopnum=9301 |

### Scraper Notes

- Scrape each store URL separately.
- Dedupe within the same city when coupons are identical across stores.
- Keep store/source URL metadata because coupons may differ later.

---

# Recommended Phase Order

1. Configuration only: convert this doc into structured city/store config without changing existing scraper behavior.
2. Shared taxonomy: centralize service classification into the standard service list.
3. City-wise output: support Calgary, Edmonton, and Grande Prairie promo tabs.
4. Competitor-by-competitor updates:
   - Midas
   - Jiffy Lube
   - Great Canadian Oil Change
   - Mr. Lube + Tires
   - Valvoline Express Care
   - Lube Town
   - Quick Lane Tire & Auto Center
   - Econo Lube
   - LubeFx Plus
   - Mobil 1 Lube Express
   - Pit Stop Oil Change
5. Ads Library remains separate and should not be mixed into promo scraping.

# Notes for Prompting AI Coding Agents

Use this instruction before each competitor update:

```text
Work only on the selected competitor. Do not delete existing scrapers. Preserve existing output schema. Use the site-specific extraction strategy listed in this document. Run only the required extraction methods for that competitor. Classify services only into the standard taxonomy. Report failed/low-confidence rows as needs_review.
```
