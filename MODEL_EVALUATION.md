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

## Final explainable Poisson evaluation

The explainable model was evaluated after the weekly seasonal-naive baseline had been frozen.

Evaluation settings:

- Forecast cutoff: `2026-04-06 18:00 UTC`
- Test rows: `996,824`
- Model: explainable Poisson regression
- Selected alpha: `0.01`
- Alpha selection: chronological validation using training data only
- Target grid: complete city/issue/hour grid with missing counts filled as zero
- Weather features: temperature, precipitation, and wind speed
- Uncertainty intervals: central 95% Poisson prediction intervals

Final full-grid results:

| Model | Test MAE |
|---|---:|
| Weekly seasonal naive, lag 168 hours | 0.0000551752 |
| Explainable Poisson regression | 0.0000690631 |

The weekly seasonal-naive baseline performs better on the full zero-filled test grid. Therefore, no claim is made that the Poisson model improves forecasting performance.

The Poisson model produced nearly constant low positive predictions because the civic-event target is extremely sparse. The test period contains very few nonzero reports relative to the complete city/issue/hour grid. Consequently, the full-grid MAE is dominated by zero-count cells.

Grouped error analysis was calculated by city and issue type. Example groups with the largest MAE included Karachi Mobility, Islamabad Mobility, Lahore Mobility, and Islamabad Electricity. Each complete-grid group contains 4,702 test rows, so these group errors are also strongly affected by zero-count cells.

The data contains noisy and sparse city labels, including `Unknown`, neighborhood names, rivers, and other non-city locations. These limitations reduce the reliability of city-level model comparisons.

The prediction intervals are Poisson intervals based on the model's predicted mean. They are reported as uncertainty estimates, not as proof of calibrated coverage. No complex model is claimed to outperform the locked baseline.