# CivicPulse Model Card

## Purpose and intended use

CivicPulse groups Pakistani news reports about electricity, water, mobility, and municipal issues by city and hour. It forecasts the **number of classified news reports** (`n_reports`) to support exploratory monitoring. A report count is not a count of verified incidents and is not an emergency alert or professional advice.

## Data and coverage

- FreeNewsAPI supplies recent news articles tagged for Pakistan. The collector searches hourly with a four-hour overlap; the service's searchable history is limited.
- GDELT Project 2.0 supplies historical event records. CivicPulse samples four 15-minute exports per day by default over a configured window of at least 730 days and filters to Pakistan action locations. This is a sample, not the complete archive.
- Open-Meteo historical weather supplies hourly temperature, precipitation, and wind speed as model features. Weather does not label civic events.
- Project configuration recognizes 18 city names and aliases. Actual data can also include `Unknown`, neighborhood names, or non-city places; coverage is not uniform.
- See `DATA_SOURCES.md` and `data/manifest.csv` for collection details, source terms, provenance, and the files actually present in a run.

## Model and evaluation

The explainable model is Poisson regression. It uses city, issue type, hour of day, day of week, temperature, precipitation, and wind speed. The locked evaluation uses a chronological cutoff of 2026-04-06 18:00 UTC. Missing city/issue/hour combinations in the retrospective evaluation grid are represented as zero. The model's regularization is selected using training data only. Central 95% Poisson intervals are reported as uncertainty estimates, not as demonstrated calibrated coverage.

In the recorded evaluation, the complete test grid contained 996,824 rows but only 28 actual reports. The weekly seasonal-naive baseline (same city and issue, 168 hours earlier) achieved full-grid MAE 0.0000551752; Poisson regression achieved 0.0000690631. The baseline performed better, so CivicPulse does not claim that the Poisson model improves forecasting. See `MODEL_EVALUATION.md` for the evaluation details and group errors.

## Known limitations and failure cases

- The target is sparse, and zero-count grid cells dominate full-grid MAE.
- FreeNewsAPI and GDELT are both media-derived; their reports are not independent primary confirmation of real-world events.
- GDELT issue labels are inferred from event codes and URL text; city labels can be noisy.
- Report volume depends on source coverage and media attention. A low count does not prove that a service is working, and a high count does not prove an incident is confirmed.
- The model has limited evidence for rare city/issue combinations and may produce weak local comparisons.
- Weather availability and geographic coverage may differ from civic-report coverage.
- The model should not be used to make individual-level decisions, claim causal effects, or replace official alerts and services.

## RAG use

This card is reference context for the retrieval layer. Numeric report counts must be read from the current Gold tables and accompanied by their Gold row IDs. Do not treat the evaluation metrics above as live forecasts.
