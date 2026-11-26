import hashlib
import hmac
import json

import httpx

from meeting_crew.contracts import Draft, Recap, Webhook
from meeting_crew.server import Settings
from meeting_crew.source import fetch_fixture
from meeting_crew.store import Store


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def main() -> None:
    config = Settings.from_env()
    require(
        config.source == "fixture" and config.model_mode == "recorded",
        "Replay requires fixture and recorded modes.",
    )
    fixture = (config.fixture_dir / "webhook.json").read_bytes()
    event = Webhook.model_validate_json(fixture)
    if event.bot_id is None:
        raise SystemExit("Fixture is missing its bot ID")
    bot_id = event.bot_id
    store = Store(config.output_dir)
    run_id, segments = fetch_fixture(config.fixture_dir, bot_id, None)
    folder = store.destination(bot_id, run_id)
    recap_path = folder / "recap.json"
    draft_path = folder / "followup_draft.md"

    headers = {"Content-Type": "application/json"}
    if config.webhook_secret:
        signature = hmac.new(config.webhook_secret.encode(), fixture, hashlib.sha256).hexdigest()
        headers["X-MeetStream-Signature"] = f"sha256={signature}"

    with httpx.Client(base_url=f"http://127.0.0.1:{config.port}", trust_env=False, timeout=5) as client:
        first = client.post("/webhook", headers=headers, content=fixture)
        print("First delivery", first.status_code, json.dumps(first.json()), flush=True)
        require(first.json()["status"] in ("accepted", "duplicate"), "The fixture was not accepted.")
        require(
            store.wait(bot_id, "processed", timeout=15),
            f"Expected processed. Ledger state {store.status(bot_id)}",
        )
        before = recap_path.read_bytes(), draft_path.read_bytes()
        second = client.post("/webhook", headers=headers, content=fixture)
        print("Replay", second.status_code, json.dumps(second.json()), flush=True)
        require(
            second.status_code == 200 and second.json() == {"status": "duplicate", "run_state": "processed"},
            "Expected the replay to report a processed duplicate.",
        )
        require(before == (recap_path.read_bytes(), draft_path.read_bytes()), "Replay changed the artifacts.")

    recap = Recap.model_validate_json(before[0])
    require((recap.bot_id, recap.run_id) == (bot_id, run_id), "Recap identity does not match the fixture.")
    require(
        {file.name for file in folder.iterdir()} == {"recap.json", "followup_draft.md"},
        "Expected exactly two files in the run directory.",
    )
    require(len(recap.scribe.decisions) >= 2, "Expected at least two decisions.")
    owners = {action.owner for action in recap.scribe.action_items}
    require(
        {"José Rivera", "Amina Shah", None} <= owners, "Expected the assigned and unassigned fixture actions."
    )
    require(
        bool(recap.analyst.risks and recap.analyst.open_questions), "Expected a risk and an open question."
    )
    locations = {(segment.speaker, segment.timestamp) for segment in segments}
    citations = [*recap.scribe.items, *(ref for item in recap.analyst.items for ref in item.evidence)]
    require(all((ref.speaker, ref.timestamp) in locations for ref in citations), "Found an invalid citation.")

    markdown = before[1].decode()
    require(
        markdown.startswith("# DRAFT") and recap.generated_at.isoformat() in markdown,
        "Draft is missing its review marker or generation time.",
    )
    _, _, content = markdown.partition("\n**Subject** — ")
    subject, _, content = content.partition("\n\n")
    body, _, references = content.rpartition("\n\n**Referenced recap items** — ")
    draft = Draft(subject=subject, body=body, referenced_item_ids=references.strip().split(", "))
    item_ids = {item.id for item in recap.scribe.items} | {item.id for item in recap.analyst.items}
    require(set(draft.referenced_item_ids) <= item_ids, "Draft references an unknown recap item.")
    print(
        f"Verified {len(recap.scribe.decisions)} decisions, {len(recap.scribe.action_items)} actions, "
        f"{len(recap.analyst.risks)} risk, {len(recap.analyst.open_questions)} open question, "
        f"{len(citations)} citations and {len(draft.referenced_item_ids)} draft references."
    )
    print(f"Artifacts: {folder}")


if __name__ == "__main__":
    main()
