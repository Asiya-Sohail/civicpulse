# Model Evaluation

## Frozen evaluation design

- Gold table: `gold.hourly_issue_counts`
- Split type: chronological
- Cutoff: `2026-04-06 18:00 UTC`
- Training rows: 113
- Test rows: 28
- Baseline lag: 168 hours
- Unobserved city/issue/hour combinations: treated as zero

## Locked weekly seasonal-naive baseline

The baseline predicts each city and issue type using the count from the same UTC hour 168 hours earlier.

Results:

- Full zero-filled-grid test rows: 996,824
- Full-grid MAE: 0.0000551752
- Event-bearing rows: 55
- Event-bearing MAE: 1.0
- Actual test reports: 28
- Predicted test reports: 27

## Interpretation and limitation

The full-grid MAE is very small because most city/issue/hour combinations have zero reports. Therefore, the event-bearing MAE is reported as an additional diagnostic.

The test period is sparse, with only 28 actual reports. No complex-model performance claim has been made yet.