from __future__ import annotations

from typing import Any

import duckdb
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder
from sklearn.linear_model import PoissonRegressor

def fit_explainable_poisson_model(
    con: duckdb.DuckDBPyConnection,
    cutoff_utc: str,
    alpha: float,
) -> Any:
    """
    Fit an explainable Poisson regression using training data only.

    Required constraints:
    - Read Gold data only.
    - Exclude rows at or after cutoff_utc.
    - Use city, issue type, calendar features, and approved weather features.
    - Do not use the chronological test period during fitting.
    - Return the fitted model and preprocessing information.
    """
    frame = build_model_frame(
        con=con,
        cutoff_utc=cutoff_utc,
    )

    train = frame[frame["split"] == "train"].copy()

    feature_columns = [
        "city",
        "issue_type",
        "hour_of_day",
        "day_of_week",
        "temperature_2m",
        "precipitation",
        "wind_speed_10m",
    ]

    X_train = train[feature_columns]
    y_train = train["n_reports"]

    preprocessor = make_poisson_preprocessor()
    X_train_transformed = preprocessor.fit_transform(X_train)

    model = PoissonRegressor(
        alpha=alpha,
        max_iter=1000,
    )

    model.fit(X_train_transformed, y_train)

    return {
        "model": model,
        "preprocessor": preprocessor,
        "training_rows": len(train),
        "feature_columns": feature_columns,
    }
def build_model_frame(
    con: duckdb.DuckDBPyConnection,
    cutoff_utc: str,
) -> pd.DataFrame:
    """
    Load the Gold modeling frame.

    Required behavior:
    - Read only gold.hourly_issue_counts_with_weather.
    - Keep n_reports as the target.
    - Create calendar features from hour_utc.
    - Keep city and issue_type as categorical fields.
    - Preserve weather NULLs until preprocessing.
    - Add a train/test split using cutoff_utc.
    - Never use test rows to fit preprocessing or the model.
    """
    
    frame = con.execute(
        """
        SELECT
            hour_utc,
            city,
            issue_type,
            n_reports,
            temperature_2m,
            precipitation,
            wind_speed_10m
        FROM gold.hourly_issue_counts_with_weather
        ORDER BY hour_utc, city, issue_type
        """
    ).df()

    frame["hour_utc"] = pd.to_datetime(
        frame["hour_utc"],
        utc=True,
    )

    cutoff = pd.Timestamp(cutoff_utc)

    if cutoff.tzinfo is None:
        cutoff = cutoff.tz_localize("UTC")
    else:
        cutoff = cutoff.tz_convert("UTC")

    frame["hour_of_day"] = frame["hour_utc"].dt.hour
    frame["day_of_week"] = frame["hour_utc"].dt.dayofweek

    frame["split"] = "test"
    frame.loc[
        frame["hour_utc"] < cutoff,
        "split",
    ] = "train"

    return frame
def make_poisson_preprocessor() -> ColumnTransformer:
    """
    Create preprocessing for the explainable Poisson model.

    Categorical features:
    - city
    - issue_type
    - hour_of_day
    - day_of_week

    Numeric weather features:
    - temperature_2m
    - precipitation
    - wind_speed_10m

    Missing weather values are imputed during training only.
    """
    categorical_features = [
        "city",
        "issue_type",
        "hour_of_day",
        "day_of_week",
    ]

    numeric_features = [
        "temperature_2m",
        "precipitation",
        "wind_speed_10m",
    ]

    categorical_pipeline = Pipeline(
        steps=[
            (
                "imputer",
                SimpleImputer(strategy="most_frequent"),
            ),
            (
                "one_hot",
                OneHotEncoder(
                    handle_unknown="ignore",
                    sparse_output=False,
                ),
            ),
        ]
    )

    numeric_pipeline = Pipeline(
        steps=[
            (
                "imputer",
                SimpleImputer(strategy="median"),
            ),
        ]
    )

    return ColumnTransformer(
        transformers=[
            (
                "categorical",
                categorical_pipeline,
                categorical_features,
            ),
            (
                "numeric",
                numeric_pipeline,
                numeric_features,
            ),
        ]
    )