.PHONY: start stop replay test test-live

UV ?= uv

start:
	$(UV) run --locked --python 3.12 python -m scripts.manage start

stop:
	@if test -S .run/control.sock; then \
		$(UV) run --offline --no-sync python -m scripts.manage stop; \
	else \
		printf '%s\n' 'Server is stopped.'; \
	fi

replay:
	$(UV) run --offline --locked python -m scripts.replay

test:
	$(UV) run --offline --locked python -m pytest -q --disable-warnings -m 'not live'
	$(UV) run --offline --locked ruff check .
	$(UV) run --offline --locked ruff format --check .
	$(UV) run --offline --locked mypy
	$(UV) run --offline --locked python -m scripts.manage test

test-live:
	MEETING_CREW_LIVE_TEST=1 $(UV) run --locked python -m pytest -m live -q -rs
