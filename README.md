# CrewAI Meeting Recap

Turn a MeetStream post-call transcript into a cited meeting recap and a follow-up draft with CrewAI and Gemini. Decisions, owners, deadlines, risks, and open questions stay linked to the source transcript. The app writes both files locally for human review.

[Quickstart](QUICKSTART.md) · [Live webhook setup](QUICKSTART.md#run-with-a-live-meeting) · [Design decisions](DECISIONS.md) · [MeetStream API and reference projects](RECON.md)

## Try it locally

On macOS or Linux, install Git, `make`, and [uv](https://docs.astral.sh/uv/getting-started/installation/). Run these commands from the repository root.

```sh
make start
make replay
make stop
```

The first start installs Python 3.12 and the locked dependencies as needed. The demo uses a fictional transcript and recorded model responses, so running it after installation needs zero API keys and zero external requests. The server listens on `127.0.0.1:3000` by default.

On a fresh output directory, `make replay` submits the fixture, waits for the worker, and submits it again. The replay verifies the output contents and confirms that the second delivery leaves both files unchanged.

```text
First delivery 202 {"status": "accepted", "run_state": "queued"}
Replay 200 {"status": "duplicate", "run_state": "processed"}
Verified 2 decisions, 3 actions, 1 risk, 1 open question, 9 citations and 7 draft references.
```

| Output | Contents |
| --- | --- |
| `out/<bot_id>/<run_id>/recap.json` | UTC generation time, decisions, action items, risks, and questions with source citations |
| `out/<bot_id>/<run_id>/followup_draft.md` | An editable draft with the same generation time and references to recap items |

Use the [live setup](QUICKSTART.md#run-with-a-live-meeting) to receive signed workspace webhooks and process transcripts from real Google Meet, Zoom, or Microsoft Teams calls. A live Google Meet run was verified from the original signed delivery through both artifacts. The duplicate replay preserved both file hashes.

## How it works

```text
MeetStream POST /webhook
        |
        v
Verify signature -> Reserve in SQLite -> HTTP 202
                           |
                           v
                   Fetch and pin transcript
                           |
                           v
              Scribe -> Analyst -> Writer
                           |
                           v
                 Publish both files together
```

- **Scribe** extracts decisions and action items. Owners and deadlines stay null when the transcript leaves them unspecified.
- **Analyst** extracts risks and open questions. The worker checks that each source citation points to an exact speaker and start time in the transcript.
- **Writer** turns the validated recap into a draft and references its item IDs. There are zero message delivery integrations.

### Delivery and recovery

| Response | Meaning |
| --- | --- |
| `202 accepted` | SQLite reserved the delivery. A local worker processes it in the background. |
| `200 duplicate` | The delivery is already queued, running, or processed. The response includes `run_state`. |
| `200 ignored` | The subscription delivered another event type. |
| `400 / 401 / 413 invalid` | The envelope, signature, or body size failed validation. |
| `503 unavailable` | The app could not reserve the delivery in SQLite. |

MeetStream's documented notification identifies the bot. The worker pins the transcript ID after looking it up and uses that ID for the output folder. When the ID is unavailable, the worker uses `transcription.processed` as the run identifier and requests the original transcript by bot ID. One receipt per bot is retained, so a later transcription of the same bot shares that delivery identity.

A failed run can be requeued by resending the delivery. Gemini HTTP 503 responses get up to two retries per model call. If the process restarts during a run, the worker resumes it from SQLite and recognizes a pair of files that was already published. Keep a single worker on a local filesystem and preserve the ledger alongside its output.

Webhook bodies are limited to 64 KiB. Transcript limits are 2 MiB, 3,000 segments, and 100,000 characters of normalized text. Live mode sends transcript text to Gemini. Source citations confirm where the evidence came from. Review the recap and draft for transcription or interpretation errors before using them.

## Development

```sh
uv sync --locked --python 3.12
make test
```

`make test` runs the offline suite, Ruff lint and format checks, mypy, and an actual HTTP replay against an isolated local server. CI runs the same checks on Linux and macOS. The optional [Gemini smoke test](QUICKSTART.md#check-gemini-with-the-fictional-transcript) uses the bundled transcript with a configured Gemini key.

| Path | Purpose |
| --- | --- |
| [`meeting_crew/`](meeting_crew/) | HTTP receiver, transcript sources, schema validation, CrewAI workflow, and SQLite worker |
| [`scripts/`](scripts/) | Start, stop, and fixture replay commands |
| [`fixtures/`](fixtures/) | Fictional transcript, webhook, and recorded model responses |
| [`tests/`](tests/) | Evidence validation, webhook signing, concurrent deliveries, retry, and provider transport checks |

Runtime logs are in `.run/server.log`. See the [quickstart troubleshooting table](QUICKSTART.md#troubleshooting) for setup and processing failures.

## References

The webhook and transcript flow follows [MeetStream Labs](https://github.com/meetstream-ai/labs). The runtime and local command setup draw on the [Universal Agent Adapter](https://github.com/meetstream-ai/universal-agent-adapter), and the evidence review follows the approach used in [Trace](https://github.com/Arjun220905/trace-engineering-decision-agent). [MeetStream docs](https://docs.meetstream.ai) · [CrewAI docs](https://docs.crewai.com)
