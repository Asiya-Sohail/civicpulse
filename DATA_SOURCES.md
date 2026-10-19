# Dataset declaration - Pakistan CivicPulse

Committed as the initial source declaration. No synthetic data is used. No Kaggle / GitHub re-uploads.

| | Source 1 - live | Source 2 - historical |
|---|---|---|
| Name | FreeNewsAPI | GDELT Project 2.0 Events |
| Publisher | FreeNewsAPI.ai (freenewsapi.ai) | The GDELT Project (gdeltproject.org) |
| Access | Keyless REST search, `country=PK`, strict country filter | Public HTTP files, no key: `http://data.gdeltproject.org/gdeltv2/YYYYMMDDHHMMSS.export.CSV.zip` |
| How we use it | Hourly search, sorted by crawl time, with a 4-hour overlap and paged results | 15-minute export files for the last ~2 years, filtered to `ActionGeo_CountryCode = PK` |
| Sampling | none | `GDELT_PER_DAY` evenly spaced files per day (default 2 of 96). This is a **sample** of GDELT, not the full archive. |
| License | FreeNewsAPI.ai terms; link to original publisher URLs and review provider terms before redistributing data | Unlimited, unrestricted use incl. commercial, free; **must cite the GDELT Project and link https://www.gdeltproject.org/** |
| Content | Stable article ID, title, publication/crawl timestamps, publisher, URL, country and categories | Event records: CAMEO code, location, tone, source URL (no article text) |

## Known limitations (stated up front)
- FreeNewsAPI country is inferred from publisher information; `strict_country=true` excludes its weakest country inferences. It represents publisher country, not necessarily where the article's events happened.
- FreeNewsAPI retains a rolling 30-day index. Hourly collection re-requests an overlapping crawl-time window; URL deduplication removes repeated articles during rebuilds.
- GDELT Events have no headline text. Issue type and city for those rows are inferred from the URL slug
  and GDELT's geocoding, so they are noisier than FreeNewsAPI rows. Every row is `verification_status =
  'unverified'` until matched to an official source.

## Provenance
Every raw file is logged in `data/manifest.csv`: source URL, search query, UTC fetch time, SHA-256, license.
Rebuild everything with `make data`.
