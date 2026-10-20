# Dataset declaration - Pakistan CivicPulse

Committed as the initial source declaration. No synthetic data is used. No Kaggle / GitHub re-uploads.

| | Source 1 - live | Source 2 - historical |
|---|---|---|
| Name | FreeNewsAPI | GDELT Project 2.0 Events |
| Publisher | FreeNewsAPI.ai (freenewsapi.ai) | The GDELT Project (gdeltproject.org) |
| Access | Keyless REST search, `country=PK`, strict country filter | Public HTTP files, no key: `http://data.gdeltproject.org/gdeltv2/YYYYMMDDHHMMSS.export.CSV.zip` |
| How we use it | Hourly search, sorted by crawl time, with a 4-hour overlap and paged results | 15-minute export files for the last ~2 years, filtered to `ActionGeo_CountryCode = PK` |
| Sampling | none | `GDELT_PER_DAY` evenly spaced files per day (default 4 of 96). This is a **sample** of GDELT, not the full archive. |
| License | FreeNewsAPI.ai permits use, storage, and processing of API data; link to original publisher URLs. Article text remains publisher-owned and FreeNewsAPI grants no licence to it: https://freenewsapi.ai/legal/terms | Unlimited, unrestricted use incl. commercial, free; **must cite the GDELT Project and link https://www.gdeltproject.org/** |
| Content | Stable article ID, title, publication/crawl timestamps, publisher, URL, country and categories | Event records: CAMEO code, location, tone, source URL (no article text) |
### Source 3 - independent weather features

| Field | Details |
|---|---|
| Name | Open-Meteo Historical Weather API |
| URL | https://open-meteo.com/en/docs/historical-weather-api |
| Use | Hourly temperature, precipitation and wind speed for Pakistan city coordinates |
| Licence | CC BY 4.0; credit Open-Meteo and the underlying data providers |
| Frequency | Historical hourly archive |
| Role | Independent weather features; it does not label civic-issue events |

Weather values will be joined to the Gold hourly timeline by UTC hour and city. The source URL, query, fetch time, SHA-256 and licence will be recorded in the manifest when this source is ingested.Current ingestion coverage: 12 recognized Pakistan cities, with hourly observations covering the civic-event window from 2024-10-11 through 2026-10-19. The stored weather feature table contains 212,880 rows. Karachi also retains a separate two-day validation sample from 2024-10-08 through 2024-10-09.


## Known limitations (stated up front)
- FreeNewsAPI country is inferred from publisher information; `strict_country=true` excludes its weakest country inferences. It represents publisher country, not necessarily where the article's events happened.
- FreeNewsAPI retains a rolling 30-day index. Hourly collection re-requests an overlapping crawl-time window; URL deduplication removes repeated articles during rebuilds.
- GDELT Events have no headline text. Issue type and city for those rows are inferred from the URL slug
  and GDELT's geocoding, so they are noisier than FreeNewsAPI rows. Every row is `verification_status =
  'unverified'` until matched to an official source.

## Provenance
Every raw file is logged in `data/manifest.csv`: source URL, request/query, UTC fetch time, SHA-256, byte count, and license/terms. Each build rehashes every Bronze file and fails if the manifest and filesystem disagree. Silver and Gold event rows retain the manifest ID, raw path, SHA-256, request, fetch time, source URL, and terms; hourly Gold rows retain their contributing manifest IDs and raw paths.

`make data` performs the configured historical backfill if its completion marker is absent or stale, then fetches current FreeNewsAPI results and recent GDELT slots before rebuilding Bronze, Silver, and Gold. Backfill samples at least four evenly spaced GDELT exports per day over at least 730 days. The build command enforces at least 60,000 legitimate Bronze rows before reporting success. A 404 is counted as an unavailable export slot; network or parse failures stop the backfill before the completion marker is written.

## Official-source status
NDMA is a credible original Pakistani civic/disaster source, but its [Terms and Conditions](https://ndma.gov.pk/terms-and-conditions) state that content is for personal, non-commercial access and prohibit unauthorized reproduction/distribution. Its [robots.txt](https://ndma.gov.pk/robots.txt) disallows `/api`. Therefore NDMA is **not ingested** by this project without explicit reuse permission or a suitable open licence. This means the project currently has two media-derived civic-event sources and one independent weather feature source, but it still does not have an authoritative official civic-event source.

The deterministic pre-evaluation forecasting baseline is weekly seasonal naive: predict each city's and issue type's count for an hour using the count from the same UTC hour one week earlier (t−168 hours), treating unobserved city/issue/hour bins as zero within the declared observation window. Freeze the chronological holdout and report MAE against it before evaluating any more complex model. No performance result is claimed.
