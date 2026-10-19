import json

from civicpulse.ingest import freenewsapi
from civicpulse.ingest.manifest import read_manifest


class FakeResponse:
    status_code = 200

    def __init__(self, results):
        self.payload = {"results": results, "total": 3, "total_is_lower_bound": False}
        self.content = json.dumps(self.payload).encode()

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.offsets = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def get(self, url, params, headers, timeout):
        self.offsets.append(int(params["offset"]))
        rows = [{"id": str(i), "title": f"headline {i}"} for i in range(3)]
        return FakeResponse(rows[:2] if params["offset"] == "0" else rows[2:])


def test_fetch_pages_pakistan_window_and_manifest(tmp_path, monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(freenewsapi, "_get_session", lambda: session)
    monkeypatch.setattr(freenewsapi.config, "FREENEWSAPI_PAGE_SIZE", 2)

    paths = freenewsapi.fetch_feed(tmp_path / "bronze", tmp_path / "manifest.csv")

    assert session.offsets == [0, 2]
    assert len(paths) == 2
    assert all(path.exists() for path in paths)
    entries = read_manifest(tmp_path / "manifest.csv")
    assert len(entries) == 2
    assert all(entry["source"] == "freenewsapi" for entry in entries)
    assert all("country=PK" in entry["search_query"] for entry in entries)
    assert all("strict_country=true" in entry["search_query"] for entry in entries)
    assert all("sort=crawled" in entry["search_query"] for entry in entries)
    assert all("from=now-4h" in entry["search_query"] for entry in entries)
