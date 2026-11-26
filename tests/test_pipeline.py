import json
from pathlib import Path

import pytest
from crewai import BaseLLM

from meeting_crew.contracts import normalize_transcript
from meeting_crew.pipeline import RecordedLLM, run_crew

FIXTURES = Path("fixtures")


def fixture_segments():
    return normalize_transcript(json.loads(next(FIXTURES.glob("transcript-*.json")).read_text()))


def test_native_crew_uses_all_three_validated_models_and_real_locations():
    segments = fixture_segments()
    scribe, analyst, draft = run_crew(segments, RecordedLLM(FIXTURES / "crew_responses.json"))
    assert len(segments) >= 40
    assert len({segment.speaker for segment in segments}) >= 3
    assert len(scribe.decisions) >= 2
    assert {"José Rivera", "Amina Shah", None} <= {action.owner for action in scribe.action_items}
    assert analyst.risks and analyst.open_questions
    locations = {(segment.speaker, segment.timestamp) for segment in segments}
    evidence = [*scribe.decisions, *scribe.action_items]
    evidence.extend(ref for item in [*analyst.risks, *analyst.open_questions] for ref in item.evidence)
    assert all((ref.speaker, ref.timestamp) in locations for ref in evidence)
    assert "D1" in draft.referenced_item_ids


@pytest.mark.parametrize(
    ("section", "change", "message"),
    [
        (
            "scribe",
            {"decisions": [{"id": "D1", "description": "Launch", "speaker": "Ghost", "timestamp": 42}]},
            "citation",
        ),
        (
            "scribe",
            {
                "decisions": [
                    {"id": "D1", "description": "Launch", "speaker": "Maya Chen ", "timestamp": 56.125}
                ]
            },
            "citation",
        ),
        (
            "analyst",
            {
                "risks": [
                    {
                        "id": "R1",
                        "description": "Delay",
                        "severity": "high",
                        "evidence": [{"speaker": "José Rivera", "timestamp": 999999}],
                    }
                ]
            },
            "citation",
        ),
        ("writer", {"referenced_item_ids": ["A99"]}, "reference"),
    ],
)
def test_guardrails_fail_closed(tmp_path, section, change, message):
    recorded = json.loads((FIXTURES / "crew_responses.json").read_text())
    recorded[section].update(change)
    bad = tmp_path / "answers.json"
    bad.write_text(json.dumps(recorded))
    with pytest.raises((RuntimeError, ValueError), match=message):
        run_crew(fixture_segments(), RecordedLLM(bad))


def test_recorded_model_uses_crewai_boundary():
    assert isinstance(RecordedLLM(FIXTURES / "crew_responses.json"), BaseLLM)
