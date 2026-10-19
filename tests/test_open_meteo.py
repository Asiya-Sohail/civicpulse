import pytest

from civicpulse.ingest.open_meteo import validate_weather_payload


def valid_payload() -> dict:
    return {
        "hourly": {
            "time": ["2024-10-08T00:00", "2024-10-08T01:00"],
            "temperature_2m": [25.0, 24.5],
            "precipitation": [0.0, 0.0],
            "wind_speed_10m": [10.0, 11.0],
        }
    }


def test_valid_weather_payload_passes() -> None:
    validate_weather_payload(valid_payload())


def test_missing_weather_field_fails() -> None:
    payload = valid_payload()
    del payload["hourly"]["precipitation"]

    with pytest.raises(ValueError):
        validate_weather_payload(payload)


def test_unequal_weather_lengths_fail() -> None:
    payload = valid_payload()
    payload["hourly"]["wind_speed_10m"] = [10.0]

    with pytest.raises(ValueError):
        validate_weather_payload(payload)

def test_fetch_historical_weather_saves_raw_file(tmp_path, monkeypatch):
    payload = {
        "hourly": {
            "time": ["2024-10-08T00:00"],
            "temperature_2m": [25.0],
            "precipitation": [0.0],
            "wind_speed_10m": [10.0],
        }
    }

    class FakeResponse:
        url = "https://archive-api.open-meteo.com/v1/archive"
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    monkeypatch.setattr(
        "civicpulse.ingest.open_meteo.requests.get",
        lambda *args, **kwargs: FakeResponse(),
    )

    from datetime import date
    from civicpulse.ingest.open_meteo import fetch_historical_weather

    saved_path = fetch_historical_weather(
        start_date=date(2024, 10, 8),
        end_date=date(2024, 10, 8),
        city="Karachi",
        latitude=24.86,
        longitude=67.01,
        output_dir=tmp_path,
        manifest_path=tmp_path / "manifest.csv",
    )
        
    from civicpulse.ingest.manifest import read_manifest

    entries = read_manifest(tmp_path / "manifest.csv")

    assert len(entries) == 1

    row = entries[0]

    assert row["source"] == "open_meteo"
    assert row["source_url"] == (
        "https://archive-api.open-meteo.com/v1/archive"
    )
    assert row["search_query"]
    assert row["license_terms"]
    assert len(row["sha256"]) == 64
    assert int(row["bytes"]) == saved_path.stat().st_size

    assert saved_path.exists()
    assert saved_path.name == "open_meteo_karachi_2024-10-08_2024-10-08.json"

def test_fetch_historical_weather_refuses_existing_file(
    tmp_path,
    monkeypatch,
):
    from datetime import date
    from civicpulse.ingest.open_meteo import fetch_historical_weather

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "hourly": {
                    "time": ["2024-10-08T00:00"],
                    "temperature_2m": [25.0],
                    "precipitation": [0.0],
                    "wind_speed_10m": [10.0],
                }
            }

    monkeypatch.setattr(
        "civicpulse.ingest.open_meteo.requests.get",
        lambda *args, **kwargs: FakeResponse(),
    )

    existing_file = (
        tmp_path / "open_meteo_karachi_2024-10-08_2024-10-08.json"
    )
    existing_file.write_text("original", encoding="utf-8")

    try:
        fetch_historical_weather(
            start_date=date(2024, 10, 8),
            end_date=date(2024, 10, 8),
            city="Karachi",
            latitude=24.86,
            longitude=67.01,
            output_dir=tmp_path,
            manifest_path=tmp_path / "manifest.csv",
        )
    except FileExistsError:
        pass
    else:
        raise AssertionError("Existing weather file was overwritten")