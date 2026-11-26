import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import OperationalError
from threading import Event, Thread
from typing import Literal, cast

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from meeting_crew.contracts import EVENT, MAX_WEBHOOK_BYTES, Webhook, verify_signature
from meeting_crew.pipeline import DEFAULT_GEMINI_MODEL, GeminiModel, RecordedLLM, gemini_api_key, run_crew
from meeting_crew.source import API_URL, TranscriptUnavailable, fetch_fixture, fetch_live
from meeting_crew.store import Store

LOG = logging.getLogger("uvicorn.error")
TranscriptSource = Literal["fixture", "meetstream"]
ModelMode = Literal["recorded", "gemini"]


@dataclass(frozen=True)
class Settings:
    output_dir: Path = Path("out")
    fixture_dir: Path = Path("fixtures")
    source: TranscriptSource = "fixture"
    model_mode: ModelMode = "recorded"
    webhook_secret: str = ""
    meetstream_api_key: str = ""
    gemini_api_key: str = ""
    gemini_model: str = DEFAULT_GEMINI_MODEL
    port: int = 3000
    host: str = "127.0.0.1"

    def __post_init__(self) -> None:
        if self.source == "meetstream" and not self.meetstream_api_key:
            raise ValueError("MEETSTREAM_API_KEY is required for TRANSCRIPT_SOURCE=meetstream")
        if self.model_mode == "gemini" and not self.gemini_api_key:
            raise ValueError("GEMINI_API_KEY or GOOGLE_API_KEY is required for LLM_MODE=gemini")
        if (self.source == "meetstream" or self.model_mode == "gemini") and not self.webhook_secret:
            raise ValueError("MEETSTREAM_WEBHOOK_SECRET is required in live modes")
        if self.source == "meetstream" and self.model_mode == "recorded":
            raise ValueError("TRANSCRIPT_SOURCE=meetstream requires LLM_MODE=gemini")

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        source = os.getenv("TRANSCRIPT_SOURCE", "fixture")
        mode = os.getenv("LLM_MODE", "recorded")
        if source != "fixture" and source != "meetstream":
            raise ValueError("TRANSCRIPT_SOURCE must be fixture or meetstream")
        if mode != "recorded" and mode != "gemini":
            raise ValueError("LLM_MODE must be recorded or gemini")
        return cls(
            output_dir=Path(os.getenv("OUTPUT_DIR", "out")),
            source=cast(TranscriptSource, source),
            model_mode=cast(ModelMode, mode),
            webhook_secret=os.getenv("MEETSTREAM_WEBHOOK_SECRET") or os.environ.get("WEBHOOK_SECRET", ""),
            meetstream_api_key=os.getenv("MEETSTREAM_API_KEY", ""),
            gemini_api_key=gemini_api_key(),
            gemini_model=os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
            port=int(os.getenv("PORT", "3000")),
            host=os.getenv("WEBHOOK_HOST", "127.0.0.1"),
        )


def _process(store: Store, config: Settings, bot_id: str, pinned_run_id: str | None) -> None:
    if pinned_run_id is not None and store.published(bot_id, pinned_run_id):
        return
    if config.source == "meetstream":
        with httpx.Client(base_url=API_URL, timeout=10, trust_env=False, follow_redirects=False) as client:
            run_id, segments = fetch_live(client, bot_id, pinned_run_id, config.meetstream_api_key)
    else:
        run_id, segments = fetch_fixture(config.fixture_dir, bot_id, pinned_run_id)
    store.pin(bot_id, run_id)
    if config.model_mode == "gemini":
        with httpx.Client(timeout=60, trust_env=False, follow_redirects=False) as client:
            scribe, analyst, draft = run_crew(
                segments, GeminiModel(config.gemini_model, config.gemini_api_key, client)
            )
    else:
        model = RecordedLLM(config.fixture_dir / "crew_responses.json")
        scribe, analyst, draft = run_crew(segments, model)
    store.publish(bot_id, run_id, scribe, analyst, draft)


def _worker(store: Store, config: Settings, stop: Event) -> None:
    completion: tuple[str, bool] | None = None
    while completion is not None or not stop.is_set():
        try:
            if completion is not None:
                bot_id, succeeded = completion
                store.finish(bot_id, succeeded=succeeded)
                if succeeded:
                    LOG.info("Post-meeting run %s processed", bot_id)
                completion = None
                continue
            job = store.take()
        except OperationalError:
            LOG.warning("The run ledger is temporarily unavailable")
            if stop.wait(0.25):
                return
            continue
        if job is None:
            stop.wait(0.1)
            continue
        bot_id, pinned_run_id = job
        succeeded = False
        try:
            _process(store, config, bot_id, pinned_run_id)
            succeeded = True
        except TranscriptUnavailable as exc:
            LOG.warning("Post-meeting run %s failed: %s. Replay the event to retry.", bot_id, exc)
        except Exception as exc:
            # Provider and validation exceptions may contain transcript text or credentials.
            LOG.warning(
                "Post-meeting run %s failed (%s). Replay the event to retry.", bot_id, type(exc).__name__
            )
        completion = (bot_id, succeeded)


def create_app(config: Settings) -> FastAPI:
    store = Store(config.output_dir)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        stop = Event()
        with store.own_worker():
            worker = Thread(target=_worker, args=(store, config, stop), name="post-meeting-worker")
            worker.start()
            try:
                yield
            finally:
                stop.set()
                await asyncio.to_thread(worker.join)

    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhook")
    async def webhook(request: Request) -> JSONResponse:
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_WEBHOOK_BYTES:
                return JSONResponse(
                    {"status": "invalid", "message": "Webhook body is too large"}, status_code=413
                )
            raw.extend(chunk)
        body = bytes(raw)
        if config.webhook_secret and not verify_signature(
            config.webhook_secret,
            body,
            request.headers.get("X-MeetStream-Signature"),
            request.headers.get("X-MeetStream-Timestamp"),
        ):
            return JSONResponse({"status": "invalid", "message": "Invalid signature"}, status_code=401)
        try:
            event = Webhook.model_validate_json(body)
        except ValidationError:
            return JSONResponse({"status": "invalid", "message": "Invalid webhook payload"}, status_code=400)
        if event.name != EVENT or event.bot_id is None:
            return JSONResponse({"status": "ignored"})
        try:
            accepted, state = await asyncio.to_thread(store.enqueue, event.bot_id)
        except OperationalError:
            LOG.error("Could not reserve a MeetStream webhook delivery")
            return JSONResponse(
                {"status": "unavailable", "message": "Could not reserve delivery"}, status_code=503
            )
        return JSONResponse(
            {"status": "accepted" if accepted else "duplicate", "run_state": state},
            status_code=202 if accepted else 200,
        )

    return app
