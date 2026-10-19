PYTHON ?= python

.PHONY: setup test probe newsagent gdelt data build stats

setup:
	$(PYTHON) -m pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest -q

probe:
	$(PYTHON) -m civicpulse probe-newsagent

newsagent:
	$(PYTHON) -m civicpulse fetch-newsagent

gdelt:
	$(PYTHON) -m civicpulse history

data:
	$(PYTHON) -m civicpulse data

build:
	$(PYTHON) -m civicpulse build

stats:
	$(PYTHON) -m civicpulse stats
