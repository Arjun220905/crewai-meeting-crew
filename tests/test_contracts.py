import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from meeting_crew.contracts import (
    FALLBACK_RUN_ID,
    MAX_SEGMENTS,
    Analyst,
    Draft,
    Scribe,
    Webhook,
    normalize_transcript,
    run_key,
    verify_signature,
)


def test_event_contract():
    data = Webhook.model_validate({"event": "transcription.processed", "bot_id": "abc-123"})
    assert data.name == "transcription.processed"
    assert data.bot_id == "abc-123"
    assert Webhook.model_validate({"event": "bot.stopped", "bot_event": "bot.failed"}).name == "bot.failed"
    for bad in (
        {},
        {"event": "transcription.processed"},
        {"bot_event": "transcription.processed", "bot_id": ".."},
        {"event": "transcription.processed", "bot_id": "bot", "transcript_status": "Failed"},
    ):
        with pytest.raises(ValidationError):
            Webhook.model_validate(bad)


def test_signing_uses_raw_body_and_aware_optional_header():
    body = b'{"bot_id": "abc"}'
    now = datetime.now(UTC)
    digest = "sha256=" + hmac.new(b"unit-test", body, hashlib.sha256).hexdigest()
    assert verify_signature("unit-test", body, digest, None)
    assert verify_signature("unit-test", body, digest, now.isoformat())
    assert not verify_signature("unit-test", body, None, None)
    assert not verify_signature("unit-test", body + b" ", digest, None)
    assert not verify_signature("unit-test", body, "sha256=é", None)
    assert not verify_signature("unit-test", body, digest, "2026-11-01T12:00:00")
    assert not verify_signature("unit-test", body, digest, "broken")
    assert not verify_signature("unit-test", body, digest, (now - timedelta(minutes=6)).isoformat())


def test_run_identity():
    assert run_key("bot-1", "transcript-1") == ("bot-1", "transcript-1")
    assert run_key("bot-1", None) == ("bot-1", FALLBACK_RUN_ID)
    assert run_key("bot-1", None) != run_key("bot-1", "transcript-1")


def test_both_wire_shapes_preserve_original_speakers_and_times():
    segments = normalize_transcript(
        [
            {"speaker": "José", "transcript": "Go ahead.", "start_time": 3.125, "end_time": 4},
            {
                "participant": {"name": "李芳"},
                "words": [
                    {"text": "明白", "start_timestamp": {"relative": 6, "absolute": 1006}},
                    {"text": "了", "start_timestamp": {"relative": 6.2, "absolute": 1006.2}},
                ],
            },
        ]
    )
    assert [(s.speaker, s.timestamp, s.text) for s in segments] == [
        ("José", 3.125, "Go ahead."),
        ("李芳", 6.0, "明白 了"),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        [{"speaker": "A", "start_time": 0, "transcript": " "}],
        [{"speaker": "A", "start_time": -1, "transcript": "hi"}],
        [{"speaker": "A", "start_time": True, "transcript": "hi"}],
        [{"speaker": "A", "start_time": float("nan"), "transcript": "hi"}],
        [{"speaker": "A", "start_time": 0, "transcript": "a" * 100001}],
        [None] * (MAX_SEGMENTS + 1),
    ],
)
def test_rejects_empty_malformed_and_large_transcript(payload):
    with pytest.raises(ValueError):
        normalize_transcript(payload)


def test_output_schemas_require_typed_citations_and_nullable_assignments():
    action = {
        "id": "A1",
        "task": "Check logs",
        "owner": None,
        "due": None,
        "speaker": "José",
        "timestamp": 3.125,
    }
    parsed = Scribe.model_validate({"decisions": [], "action_items": [action]})
    assert parsed.action_items[0].owner is None
    with pytest.raises(ValidationError):
        Scribe.model_validate({"decisions": [], "action_items": [{**action, "timestamp": "3.125"}]})
    with pytest.raises(ValidationError):
        Scribe.model_validate({"decisions": [], "action_items": [action, action]})
    with pytest.raises(ValidationError):
        Analyst.model_validate(
            {
                "risks": [{"id": "R1", "description": "Delay", "severity": "high", "evidence": []}],
                "open_questions": [],
            }
        )
    with pytest.raises(ValidationError):
        Draft.model_validate({"subject": "Recap", "body": "Review", "referenced_item_ids": ["A1", "A1"]})
