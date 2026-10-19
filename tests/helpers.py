"""Test-only fixtures. These tiny files exist to exercise the code; they are never loaded by
`make data` and are not training data."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

from civicpulse.ingest.gdelt import GDELT_COLS


def gdelt_line(eid, url, fullname, lat, lon, country="PK", dateadded="20260301061500"):
    row = {c: "" for c in GDELT_COLS}
    row.update(GLOBALEVENTID=str(eid), SQLDATE=dateadded[:8], SOURCEURL=url, EventCode="140",
               ActionGeo_FullName=fullname, ActionGeo_Lat=str(lat), ActionGeo_Long=str(lon),
               ActionGeo_CountryCode=country, DATEADDED=dateadded, AvgTone="-2.5")
    return "\t".join(row[c] for c in GDELT_COLS)


def write_gdelt_zip(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(path.name.replace(".zip", ""), "\n".join(lines) + "\n")
    return path


def write_freenewsapi(path: Path, items: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"results": items}), encoding="utf-8")
    return path
