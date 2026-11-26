import hashlib
import hmac
import re
from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, model_validator

EVENT = FALLBACK_RUN_ID = "transcription.processed"
MAX_WEBHOOK_BYTES = 64 * 1024
MAX_TRANSCRIPT_BYTES = 2 * 1024 * 1024
MAX_SEGMENTS = 3000
MAX_TRANSCRIPT_CHARS = 100_000

SafeID = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,127}$")]
Text = Annotated[str, Field(min_length=1, max_length=12_000, pattern=r"\S")]
Speaker = Annotated[str, Field(min_length=1, max_length=256, pattern=r"\S")]
Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
ItemID = Annotated[str, Field(pattern=r"^[DARQ][1-9][0-9]{0,7}$")]


class Webhook(BaseModel):
    model_config = ConfigDict(strict=True)

    event: Annotated[str, Field(min_length=1)] | None = None
    bot_event: Annotated[str, Field(min_length=1)] | None = None
    bot_id: SafeID | None = None
    transcript_status: str | None = None

    @property
    def name(self) -> str:
        return self.bot_event or self.event or ""

    @model_validator(mode="after")
    def validate_event(self) -> Self:
        if not self.name:
            raise ValueError("Missing event name")
        if self.name == EVENT and (self.bot_id is None or self.transcript_status not in (None, "Success")):
            raise ValueError("Expected a bot ID and a successful transcript")
        return self


def run_key(bot_id: str, transcript_id: str | None) -> tuple[str, str]:
    return bot_id, transcript_id or FALLBACK_RUN_ID


def verify_signature(secret: str, raw: bytes, signature: str | None, timestamp: str | None) -> bool:
    if signature is None or not re.fullmatch(r"sha256=[0-9a-f]{64}", signature):
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.removeprefix("sha256=")):
        return False
    if timestamp is None:
        return True
    try:
        received = datetime.fromisoformat(timestamp)
        return received.utcoffset() is not None and abs((datetime.now(UTC) - received).total_seconds()) <= 300
    except (ValueError, OverflowError):
        return False


class Location(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    speaker: Speaker
    timestamp: Seconds


class Segment(Location):
    text: Text


class StandardSegment(BaseModel):
    speaker: Speaker
    start_time: Seconds
    transcript: Text


class Participant(BaseModel):
    name: Speaker


class Offset(BaseModel):
    relative: Seconds


class Word(BaseModel):
    text: Annotated[str, Field(max_length=1000)]
    start_timestamp: Offset


class ParticipantSegment(BaseModel):
    participant: Participant
    words: Annotated[list[Word], Field(min_length=1)]


WIRE_SEGMENTS = TypeAdapter(list[StandardSegment | ParticipantSegment])


def normalize_transcript(raw: object) -> list[Segment]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_SEGMENTS:
        raise ValueError(f"Expected 1–{MAX_SEGMENTS} transcript segments")
    segments: list[Segment] = []
    for entry in WIRE_SEGMENTS.validate_python(raw, strict=True):
        if isinstance(entry, StandardSegment):
            segments.append(Segment(speaker=entry.speaker, timestamp=entry.start_time, text=entry.transcript))
        else:
            segments.append(
                Segment(
                    speaker=entry.participant.name,
                    timestamp=entry.words[0].start_timestamp.relative,
                    text=" ".join(word.text for word in entry.words),
                )
            )
    if sum(len(s.text) + len(s.speaker) for s in segments) > MAX_TRANSCRIPT_CHARS:
        raise ValueError("Transcript text exceeds the example's context limit")
    return segments


class Decision(Location):
    id: Annotated[str, Field(pattern=r"^D[1-9][0-9]{0,7}$")]
    description: Text


class ActionItem(Location):
    id: Annotated[str, Field(pattern=r"^A[1-9][0-9]{0,7}$")]
    task: Text
    owner: Speaker | None
    due: Text | None


class StructuredOutput(BaseModel):
    model_config = ConfigDict(strict=True, str_strip_whitespace=True, extra="forbid")


class Risk(StructuredOutput):
    id: Annotated[str, Field(pattern=r"^R[1-9][0-9]{0,7}$")]
    description: Text
    severity: Literal["low", "medium", "high"]
    evidence: Annotated[list[Location], Field(min_length=1)]


class OpenQuestion(StructuredOutput):
    id: Annotated[str, Field(pattern=r"^Q[1-9][0-9]{0,7}$")]
    question: Text
    who_should_answer: Speaker | None
    evidence: Annotated[list[Location], Field(min_length=1)]


def unique_ids(ids: list[str]) -> None:
    if len(set(ids)) != len(ids):
        raise ValueError("Item IDs must be unique")


class Scribe(StructuredOutput):
    decisions: list[Decision]
    action_items: list[ActionItem]

    @property
    def items(self) -> list[Decision | ActionItem]:
        return [*self.decisions, *self.action_items]

    @model_validator(mode="after")
    def check_ids(self) -> Self:
        unique_ids([item.id for item in self.items])
        return self


class Analyst(StructuredOutput):
    risks: list[Risk]
    open_questions: list[OpenQuestion]

    @property
    def items(self) -> list[Risk | OpenQuestion]:
        return [*self.risks, *self.open_questions]

    @model_validator(mode="after")
    def check_ids(self) -> Self:
        unique_ids([item.id for item in self.items])
        return self


class Draft(StructuredOutput):
    subject: Annotated[str, Field(min_length=1, max_length=240, pattern=r"^[^\r\n]+$")]
    body: Text
    referenced_item_ids: list[ItemID]

    @model_validator(mode="after")
    def check_ids(self) -> Self:
        unique_ids(self.referenced_item_ids)
        return self


class Recap(StructuredOutput):
    bot_id: SafeID
    run_id: SafeID | Literal["transcription.processed"]
    generated_at: AwareDatetime
    scribe: Scribe
    analyst: Analyst
