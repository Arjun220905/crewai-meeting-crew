import json
from pathlib import Path
from time import sleep

import httpx
from pydantic import BaseModel

from meeting_crew.contracts import (
    FALLBACK_RUN_ID,
    MAX_TRANSCRIPT_BYTES,
    SafeID,
    Segment,
    normalize_transcript,
    run_key,
)

API_URL = "https://api.meetstream.ai/api/v1/"
POLL_ATTEMPTS = 6


class BotDetails(BaseModel):
    bot_id: SafeID | None = None
    transcript_id: SafeID | None = None


class DetailResponse(BaseModel):
    bot_details: BotDetails


class TranscriptUnavailable(RuntimeError):
    pass


def _read_fixture(path: Path) -> object:
    with path.open("rb") as file:
        raw = file.read(MAX_TRANSCRIPT_BYTES + 1)
    if len(raw) > MAX_TRANSCRIPT_BYTES:
        raise ValueError("Fixture exceeds the transcript byte limit")
    return json.loads(raw)


def fetch_fixture(root: Path, bot_id: str, pinned_run_id: str | None) -> tuple[str, list[Segment]]:
    details = DetailResponse.model_validate(_read_fixture(root / "bot_detail.json")).bot_details
    if bot_id != details.bot_id:
        raise TranscriptUnavailable("No local replay transcript for this bot")
    _, run_id = run_key(bot_id, pinned_run_id or details.transcript_id)
    raw = _read_fixture(root / f"transcript-{run_id}.json")
    return run_id, normalize_transcript(raw)


def _read_api(client: httpx.Client, path: str, key: str, attempts: int = 1) -> object:
    headers = {"Authorization": f"Token {key}", "Accept-Encoding": "identity"}
    for attempt in range(attempts):
        with client.stream("GET", path, headers=headers) as response:
            if response.status_code == 200:
                raw = bytearray()
                for chunk in response.iter_bytes(chunk_size=16 * 1024):
                    raw.extend(chunk)
                    if len(raw) > MAX_TRANSCRIPT_BYTES:
                        raise ValueError("MeetStream response exceeds the transcript byte limit")
                return json.loads(raw)
            if response.status_code != 202:
                raise TranscriptUnavailable(f"MeetStream GET returned HTTP {response.status_code}")
        if attempt + 1 < attempts:
            sleep(1)
    raise TranscriptUnavailable("MeetStream transcript is still processing")


def fetch_live(
    client: httpx.Client, bot_id: str, pinned_run_id: str | None, api_key: str
) -> tuple[str, list[Segment]]:
    run_id = pinned_run_id
    if run_id is None:
        data = _read_api(client, f"bots/{bot_id}/detail", api_key)
        details = DetailResponse.model_validate(data).bot_details
        _, run_id = run_key(bot_id, details.transcript_id)
    path = (
        f"bots/{bot_id}/get_transcript"
        if run_id == FALLBACK_RUN_ID
        else f"transcript/{run_id}/get_transcript"
    )
    return run_id, normalize_transcript(_read_api(client, path, api_key, POLL_ATTEMPTS))
