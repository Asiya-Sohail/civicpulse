# Dataset declaration - Pakistan CivicPulse

Committed as the initial source declaration. No synthetic data is used. No Kaggle / GitHub re-uploads.

| | Source 1 - live | Source 2 - historical |
|---|---|---|
| Name | NewsAgent Data API | GDELT Project 2.0 Events |
| Publisher | NewsAgent Data (newsagentdata.com) | The GDELT Project (gdeltproject.org) |
| Access | REST API, free key, 100 requests/day, `country=pk` | Public HTTP files, no key: `http://data.gdeltproject.org/gdeltv2/YYYYMMDDHHMMSS.export.CSV.zip` |
| How we use it | 1 pull per hour (~24 requests/day) | 15-minute export files for the last ~2 years, filtered to `ActionGeo_CountryCode = PK` |
| Sampling | none | `GDELT_PER_DAY` evenly spaced files per day (default 2 of 96). This is a **sample** of GDELT, not the full archive. |
| License | Provider terms (free tier); confirm redistribution rights before publishing raw data | Unlimited, unrestricted use incl. commercial, free; **must cite the GDELT Project and link https://www.gdeltproject.org/** |
| Content | Title, URL, timestamp, topic/country tags, urgency score | Event records: CAMEO code, location, tone, source URL (no article text) |

## Known limitations (stated up front)
- NewsAgent Data advertises Russian/English-first coverage and an archive starting May 2026. Pakistan
  coverage under `country=pk` has to be confirmed with `python -m civicpulse probe-newsagent`.
- GDELT Events have no headline text. Issue type and city for those rows are inferred from the URL slug
  and GDELT's geocoding, so they are noisier than NewsAgent rows. Every row is `verification_status =
  'unverified'` until matched to an official source.

## Provenance
Every raw file is logged in `data/manifest.csv`: source URL, search query, UTC fetch time, SHA-256, license.
Rebuild everything with `make data`.
