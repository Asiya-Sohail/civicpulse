PY ?= python

.PHONY: setup data build gdelt newsagent probe stats test clean

setup:            ## install the package + dev tools into the active venv
	$(PY) -m pip install -U pip
	$(PY) -m pip install -e ".[dev]"

data:             ## full pipeline: fetch sources, rebuild bronze -> silver -> gold, run DQ gate
	$(PY) -m civicpulse data

build:            ## rebuild the warehouse from files already on disk (no network)
	$(PY) -m civicpulse build

gdelt:            ## download the GDELT history only (resumable)
	$(PY) -m civicpulse fetch-gdelt

newsagent:        ## one hourly pull from the NewsAgent API
	$(PY) -m civicpulse fetch-newsagent

probe:            ## check the NewsAgent response shape / Pakistan coverage
	$(PY) -m civicpulse probe-newsagent

stats:            ## row counts per layer + hackathon volume check
	$(PY) -m civicpulse stats

test:
	$(PY) -m pytest -q

clean:            ## delete the warehouse database only (raw files are kept)
	$(PY) -c "import pathlib; [p.unlink() for p in pathlib.Path('data/warehouse').glob('*.duckdb*')]"
