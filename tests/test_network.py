import json
import os
import socket
from pathlib import Path

import httpx
import pytest
from dotenv import load_dotenv

from meeting_crew.contracts import FALLBACK_RUN_ID, normalize_transcript
from meeting_crew.pipeline import DEFAULT_GEMINI_MODEL, GeminiModel, gemini_api_key, run_crew
from meeting_crew.source import API_URL, fetch_live

FIXTURES = Path("fixtures")


def segments():
    return normalize_transcript(json.loads(next(FIXTURES.glob("transcript-*.json")).read_text()))


def test_only_llm_provider_contact_with_local_transcript(monkeypatch):
    attempted_dns = []
    provider_calls = []
    expected = json.loads((FIXTURES / "crew_responses.json").read_text())

    def forbidden_dns(*args, **kwargs):
        attempted_dns.append(args)
        raise AssertionError("Unexpected network access")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden_dns)

    def provider(request):
        assert request.method == "POST"
        assert str(request.url) == (
            f"https://generativelanguage.googleapis.com/v1beta/models/{DEFAULT_GEMINI_MODEL}:generateContent"
        )
        assert request.headers["x-goog-api-key"] == "test-only"
        body = json.loads(request.content)
        assert "tools" not in body and body["generationConfig"]["responseMimeType"] == "application/json"
        role = ("scribe", "analyst", "writer")[len(provider_calls)]
        provider_calls.append(role)
        messages = " ".join(part["text"] for content in body["contents"] for part in content["parts"])
        if role in ("scribe", "analyst"):
            assert "Merge repeated mentions" in messages
            assert "clearest restatement" in messages
        if role == "writer":
            assert "Mention each distinct item once" in messages
            assert '"id":"D1"' in messages
            assert '"id":"R1"' in messages
            assert "nineteen eligible testers" not in messages
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [
                                {"text": "private reasoning", "thought": True},
                                {"text": json.dumps(expected[role])},
                            ]
                        },
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(provider), trust_env=False) as client:
        run_crew(segments(), GeminiModel(DEFAULT_GEMINI_MODEL, "test-only", client))
    assert provider_calls == ["scribe", "analyst", "writer"]
    assert attempted_dns == []


@pytest.mark.parametrize("run_id", ["run-123", None])
def test_live_transcript_source_only_reads_documented_endpoints(run_id, monkeypatch):
    calls = []
    monkeypatch.setattr("meeting_crew.source.sleep", lambda seconds: None)

    def meetstream(request):
        assert request.method == "GET" and request.url.host == "api.meetstream.ai"
        assert request.headers["Authorization"] == "Token test-token"
        calls.append(request.url.path)
        if request.url.path.endswith("/detail"):
            return httpx.Response(200, json={"bot_details": {"transcript_id": run_id}})
        if len(calls) == 2:
            return httpx.Response(202)
        return httpx.Response(200, json=[{"speaker": "José", "start_time": 1, "transcript": "Agreed."}])

    with httpx.Client(base_url=API_URL, transport=httpx.MockTransport(meetstream)) as client:
        actual, transcript = fetch_live(client, "test-bot", None, "test-token")
    assert actual == (run_id or FALLBACK_RUN_ID)
    assert transcript[0].speaker == "José"
    path = f"/api/v1/transcript/{run_id}/get_transcript" if run_id else "/api/v1/bots/test-bot/get_transcript"
    assert calls == ["/api/v1/bots/test-bot/detail", path, path]


@pytest.mark.live
@pytest.mark.enable_socket
def test_live_gemini_smoke():
    if not os.getenv("MEETING_CREW_LIVE_TEST"):
        pytest.skip("Set MEETING_CREW_LIVE_TEST=1 to opt into live provider calls")
    load_dotenv()
    key = gemini_api_key()
    if not key:
        pytest.skip("GEMINI_API_KEY and GOOGLE_API_KEY are unset")
    with httpx.Client(trust_env=False, timeout=60) as client:
        scribe, analyst, draft = run_crew(
            segments(), GeminiModel(os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL), key, client)
        )
    assert scribe.decisions and scribe.action_items and analyst.risks and draft.referenced_item_ids


def test_gemini_key_prefers_gemini_and_accepts_google_alias(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "alias")
    assert gemini_api_key() == "alias"
    monkeypatch.setenv("GEMINI_API_KEY", "primary")
    assert gemini_api_key() == "primary"


@pytest.mark.parametrize("response", [{"candidates": []}, {"candidates": [{"finishReason": "MAX_TOKENS"}]}])
def test_gemini_rejects_missing_or_truncated_output(response):
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=response))
    with httpx.Client(transport=transport) as client, pytest.raises(RuntimeError, match="Gemini"):
        GeminiModel(DEFAULT_GEMINI_MODEL, "test-only", client).call("Return JSON")


@pytest.mark.parametrize("statuses", [[503, 200], [503, 503, 503], [403]])
def test_gemini_retries_only_temporary_unavailability(statuses, monkeypatch):
    monkeypatch.setattr("meeting_crew.pipeline.sleep", lambda seconds: None)
    seen = []

    def respond(request):
        status = statuses[len(seen)]
        seen.append(status)
        return httpx.Response(
            status,
            json={
                "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "{}"}]}}],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = GeminiModel(DEFAULT_GEMINI_MODEL, "test-only", client)
        if statuses[-1] == 200:
            assert model.call("Return JSON") == "Final Answer: {}"
        else:
            with pytest.raises(httpx.HTTPStatusError, match=str(statuses[-1])):
                model.call("Return JSON")
    assert seen == statuses
