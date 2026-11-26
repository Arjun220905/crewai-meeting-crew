import hashlib
import hmac
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from meeting_crew.contracts import MAX_WEBHOOK_BYTES, Recap
from meeting_crew.server import Settings, create_app
from meeting_crew.store import Store

FIXTURES = Path("fixtures")
BODY = (FIXTURES / "webhook.json").read_bytes()


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch):
    if not os.getenv("MEETING_CREW_LIVE_TEST"):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("MEETSTREAM_API_KEY", raising=False)


def settings(tmp_path, **kwargs):
    return Settings(output_dir=tmp_path, fixture_dir=FIXTURES, **kwargs)


def test_webhook_validates_signature_and_envelope(tmp_path):
    with TestClient(create_app(settings(tmp_path, webhook_secret="test-only"))) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.post("/webhook", content=BODY).status_code == 401
        assert (
            client.post(
                "/webhook", content=BODY, headers={"X-MeetStream-Signature": "sha256=bad"}
            ).status_code
            == 401
        )

        def post(body):
            signature = hmac.new(b"test-only", body, hashlib.sha256).hexdigest()
            return client.post(
                "/webhook", content=body, headers={"X-MeetStream-Signature": f"sha256={signature}"}
            )

        for body in (b"{", b"{}", b'{"event":"transcription.processed"}'):
            res = post(body)
            assert res.status_code == 400 and res.json()["status"] == "invalid"
        assert post(b" " * (MAX_WEBHOOK_BYTES + 1)).status_code == 413
        assert post(b'{"event":"bot.done","bot_id":"demo"}').json() == {"status": "ignored"}
        assert post(BODY).status_code == 202


def test_twenty_concurrent_deliveries_and_sequential_replay_run_exactly_once(tmp_path, monkeypatch, recwarn):
    from meeting_crew import server

    entered, release = Event(), Event()
    calls = []
    original = server.run_crew

    def counted(segments, model):
        calls.append(1)
        entered.set()
        assert release.wait(10)
        return original(segments, model)

    monkeypatch.setattr(server, "run_crew", counted)
    with TestClient(create_app(settings(tmp_path))) as client:
        with ThreadPoolExecutor(max_workers=20) as pool:
            responses = list(pool.map(lambda _: client.post("/webhook", content=BODY), range(20)))
        try:
            assert [r.json()["status"] for r in responses].count("accepted") == 1
            assert [r.json()["status"] for r in responses].count("duplicate") == 19
            assert entered.wait(5)
            pending = client.post("/webhook", content=BODY).json()
            assert pending["status"] == "duplicate" and pending["run_state"] == "running"
        finally:
            release.set()
        bot_id = json.loads(BODY)["bot_id"]
        assert Store(tmp_path).wait(bot_id, "processed", timeout=20)
        assert client.post("/webhook", content=BODY).json()["run_state"] == "processed"
    assert calls == [1]
    [recap_path] = list(tmp_path.glob("*/*/recap.json"))
    [draft_path] = list(tmp_path.glob("*/*/followup_draft.md"))
    assert recap_path.parent == draft_path.parent
    assert set(p.name for p in recap_path.parent.iterdir()) == {"recap.json", "followup_draft.md"}
    recap = Recap.model_validate_json(recap_path.read_bytes())
    assert recap.bot_id == bot_id and recap.run_id == recap_path.parent.name
    assert recap.generated_at.utcoffset().total_seconds() == 0
    assert draft_path.read_text().startswith("# DRAFT")
    assert recap.generated_at.isoformat() in draft_path.read_text()
    assert not any("A test tried to use socket" in str(warning.message) for warning in recwarn)


def test_sequential_retry_after_mid_crew_failure(tmp_path, monkeypatch):
    from meeting_crew.pipeline import RecordedLLM

    original = RecordedLLM.call
    calls = []

    def intermittent(self, messages, *args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("temporary provider failure")
        return original(self, messages, *args, **kwargs)

    monkeypatch.setattr(RecordedLLM, "call", intermittent)
    with TestClient(create_app(settings(tmp_path))) as client:
        assert client.post("/webhook", content=BODY).json()["status"] == "accepted"
        bot_id = json.loads(BODY)["bot_id"]
        assert Store(tmp_path).wait(bot_id, "failed", timeout=20)
        assert not list(tmp_path.glob("*/*/recap.json"))
        assert client.post("/webhook", content=BODY).json()["status"] == "accepted"
        assert Store(tmp_path).wait(bot_id, "processed", timeout=20)
        assert client.post("/webhook", content=BODY).json()["status"] == "duplicate"
    assert len(calls) == 5


def test_signed_mode_requires_a_secret(tmp_path):
    with pytest.raises(ValueError, match="MEETSTREAM_WEBHOOK_SECRET"):
        settings(tmp_path, source="meetstream", meetstream_api_key="test-token")


def test_worker_retries_a_transient_ledger_error_after_publication(tmp_path, monkeypatch):
    from sqlite3 import OperationalError

    finish = Store.finish
    writes = []

    def transient(self, bot_id, *, succeeded):
        writes.append(succeeded)
        if len(writes) == 1:
            raise OperationalError("database is locked")
        return finish(self, bot_id, succeeded=succeeded)

    monkeypatch.setattr(Store, "finish", transient)
    bot_id = json.loads(BODY)["bot_id"]
    with TestClient(create_app(settings(tmp_path))) as client:
        assert client.post("/webhook", content=BODY).json()["status"] == "accepted"
        assert Store(tmp_path).wait(bot_id, "processed", timeout=4)
        assert client.post("/webhook", content=BODY).json()["status"] == "duplicate"
    assert writes == [True, True]
    assert len(list(tmp_path.glob("*/*/recap.json"))) == 1


@pytest.mark.parametrize("already_published", [False, True])
def test_restart_recovers_an_interrupted_reservation(tmp_path, monkeypatch, already_published):
    from meeting_crew import server
    from meeting_crew.pipeline import RecordedLLM
    from meeting_crew.source import fetch_fixture

    bot_id = json.loads(BODY)["bot_id"]
    run_id, segments = fetch_fixture(FIXTURES, bot_id, None)
    store = Store(tmp_path)
    with store.own_worker():
        assert store.enqueue(bot_id) == (True, "queued")
        assert store.take() == (bot_id, None)
        store.pin(bot_id, run_id)
        if already_published:
            store.publish(
                bot_id, run_id, *server.run_crew(segments, RecordedLLM(FIXTURES / "crew_responses.json"))
            )
        (tmp_path / ".staging" / "interrupted.tmp").write_text("unpublished")
    original = server.run_crew
    calls = []

    def counted(segments, model):
        calls.append(1)
        return original(segments, model)

    monkeypatch.setattr(server, "run_crew", counted)
    with TestClient(create_app(settings(tmp_path))) as client:
        assert store.wait(bot_id, "processed", timeout=10)
        assert client.post("/webhook", content=BODY).json()["status"] == "duplicate"
    assert calls == ([] if already_published else [1])
    assert not list((tmp_path / ".staging").iterdir())
    assert store.published(bot_id, run_id)


@pytest.mark.parametrize(
    "bad_transcript", [[], [{"speaker": "José", "start_time": 1, "transcript": "x" * 2_100_000}]]
)
def test_accepted_delivery_with_invalid_transcript_fails_without_running_crew(
    tmp_path, monkeypatch, bad_transcript
):
    from shutil import copy2

    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    copy2(FIXTURES / "bot_detail.json", fixture_dir)
    (fixture_dir / next(FIXTURES.glob("transcript-*.json")).name).write_text(json.dumps(bad_transcript))
    crew_calls = []
    monkeypatch.setattr("meeting_crew.server.run_crew", lambda *args: crew_calls.append(args))
    output_dir = tmp_path / "out"
    bot_id = json.loads(BODY)["bot_id"]
    with TestClient(create_app(Settings(output_dir=output_dir, fixture_dir=fixture_dir))) as client:
        assert client.post("/webhook", content=BODY).json()["status"] == "accepted"
        assert Store(output_dir).wait(bot_id, "failed", timeout=4)
    assert not crew_calls
    assert not list(output_dir.glob("*/*/recap.json"))
