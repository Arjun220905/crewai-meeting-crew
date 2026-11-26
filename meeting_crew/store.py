import fcntl
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from shutil import rmtree
from sqlite3 import Connection, connect
from tempfile import TemporaryDirectory
from time import monotonic, sleep

from meeting_crew.contracts import Analyst, Draft, Recap, Scribe, run_key

# The dot distinguishes a provisional reservation from an API transcript ID.
UNRESOLVED = ".pending"


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root

    @contextmanager
    def _db(self) -> Iterator[Connection]:
        with closing(connect(self.root / ".runs.sqlite3", timeout=2)) as db, db:
            yield db

    @contextmanager
    def own_worker(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".worker.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Another worker owns this output directory") from exc
            with self._db() as db:
                db.execute("""
                    CREATE TABLE IF NOT EXISTS runs (
                        bot_id TEXT NOT NULL UNIQUE,
                        run_id TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'processed', 'failed')),
                        PRIMARY KEY (bot_id, run_id)
                    )
                """)
                db.execute("UPDATE runs SET state = 'queued' WHERE state = 'running'")
            # ponytail: one worker owns a private staging directory on a local filesystem.
            staging = self.root / ".staging"
            if staging.exists():
                rmtree(staging)
            staging.mkdir(mode=0o700)
            yield

    def enqueue(self, bot_id: str) -> tuple[bool, str]:
        with self._db() as db:
            row = db.execute(
                """
                INSERT INTO runs (bot_id, run_id, state) VALUES (?, ?, 'queued')
                ON CONFLICT (bot_id) DO UPDATE SET state = 'queued' WHERE state = 'failed'
                RETURNING state
            """,
                (bot_id, UNRESOLVED),
            ).fetchone()
            if row is not None:
                return True, "queued"
            row = db.execute("SELECT state FROM runs WHERE bot_id = ?", (bot_id,)).fetchone()
            if row is None:
                raise RuntimeError("Delivery reservation is missing")
            return False, str(row[0])

    def take(self) -> tuple[str, str | None] | None:
        with self._db() as db:
            row = db.execute("""
                UPDATE runs SET state = 'running'
                WHERE rowid = (SELECT rowid FROM runs WHERE state = 'queued' ORDER BY rowid LIMIT 1)
                RETURNING bot_id, run_id
            """).fetchone()
        if row is None:
            return None
        bot_id, run_id = map(str, row)
        return bot_id, None if run_id == UNRESOLVED else run_id

    def pin(self, bot_id: str, run_id: str) -> None:
        with self._db() as db:
            updated = db.execute(
                """
                UPDATE runs SET run_id = ? WHERE bot_id = ? AND state = 'running' AND run_id IN (?, ?)
            """,
                (run_id, bot_id, UNRESOLVED, run_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError("Could not reserve the transcript run")

    def finish(self, bot_id: str, *, succeeded: bool) -> None:
        with self._db() as db:
            db.execute(
                "UPDATE runs SET state = ? WHERE bot_id = ? AND state = 'running'",
                ("processed" if succeeded else "failed", bot_id),
            )

    def status(self, bot_id: str) -> tuple[str, str] | None:
        with self._db() as db:
            row = db.execute("SELECT run_id, state FROM runs WHERE bot_id = ?", (bot_id,)).fetchone()
        return (str(row[0]), str(row[1])) if row else None

    def wait(self, bot_id: str, state: str, *, timeout: float) -> bool:
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            status = self.status(bot_id)
            if status and status[1] == state:
                return True
            sleep(0.05)
        return False

    def destination(self, bot_id: str, run_id: str) -> Path:
        bot, run = run_key(bot_id, run_id)
        return self.root / bot / run

    def published(self, bot_id: str, run_id: str) -> bool:
        folder = self.destination(bot_id, run_id)
        return (folder / "recap.json").is_file() and (folder / "followup_draft.md").is_file()

    def publish(self, bot_id: str, run_id: str, scribe: Scribe, analyst: Analyst, draft: Draft) -> None:
        now = datetime.now(UTC)
        recap = Recap(bot_id=bot_id, run_id=run_id, generated_at=now, scribe=scribe, analyst=analyst)
        refs = ", ".join(draft.referenced_item_ids) or "None"
        markdown = (
            f"# DRAFT — for human review\n\n"
            f"Generated at {now.isoformat()}\n\n"
            f"**Subject** — {draft.subject}\n\n{draft.body}\n\n"
            f"**Referenced recap items** — {refs}\n"
        )
        destination = self.destination(bot_id, run_id)
        destination.parent.mkdir(mode=0o700, exist_ok=True)
        with TemporaryDirectory(dir=self.root / ".staging") as name:
            stage = Path(name)
            (stage / "recap.json").write_text(recap.model_dump_json(indent=2) + "\n", encoding="utf-8")
            (stage / "followup_draft.md").write_text(markdown, encoding="utf-8")
            stage.rename(destination)
