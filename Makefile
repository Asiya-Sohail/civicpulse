PYTHON ?= python

.PHONY: setup test probe freenewsapi gdelt data build stats

setup:
	$(PYTHON) -m pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest -q

probe:
	$(PYTHON) -m civicpulse probe-freenewsapi

freenewsapi:
	$(PYTHON) -m civicpulse fetch-freenewsapi

gdelt:
	$(PYTHON) -m civicpulse history

data:
	$(PYTHON) -m civicpulse data

build:
	$(PYTHON) -m civicpulse build

stats:
	$(PYTHON) -m civicpulse stats
