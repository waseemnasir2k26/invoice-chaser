.PHONY: demo test install

install:
	pip install -e ".[dev]"

demo:
	python -m invoice_chaser demo --out out

test:
	python -m pytest -q
