# Quickstart

## Run the fixture demo

Use macOS or Linux with Git, `make`, and [uv](https://docs.astral.sh/uv/getting-started/installation/). Sign in to GitHub with an account that has access to the repository.

```sh
git clone https://github.com/Arjun220905/crewai-meeting-crew.git
cd crewai-meeting-crew
make start
make replay
make stop
```

`make start` launches a background server and prints its port and log path. The first start needs internet access to install dependencies. The bundled demo runs with the default settings and needs zero credentials.

Look for `202 accepted` on the first delivery and `200 duplicate` after the worker finishes. Open the output folder printed by `make replay`. It contains `recap.json` and `followup_draft.md`. The script checks the fixture's decisions and owners, transcript citations, draft reference IDs, and unchanged file contents after replay.

If you've already run the demo, a repeat should return `duplicate`. To see a new first delivery, point start and replay at a fresh directory inside `out/`. Stop any already-running server first.

```sh
OUTPUT_DIR=out/fresh-demo make start
OUTPUT_DIR=out/fresh-demo make replay
make stop
```

## Check Gemini with the fictional transcript

Set `GEMINI_API_KEY` in your local `.env` or export it in your shell, then run `make test-live`.

This runs the three roles using the fictional meeting and the live Gemini API. The default model is `gemini-3.5-flash-lite`. The test skips if the API key is missing. The app accepts `GOOGLE_API_KEY` as a fallback. [Gemini API keys](https://aistudio.google.com/docs/api-key)

## Run with a live meeting

Use a test workspace and a meeting whose participants have agreed to recording and transcript processing. You need a MeetStream API key, a Gemini API key, and a public HTTPS endpoint pointed at the local listener.

1. Stop the demo with `make stop`. Copy [.env.example](.env.example) to `.env` on first setup, then set the values below. Keep an existing `.env` and edit the needed lines if you've already configured the project.

   ```dotenv
   TRANSCRIPT_SOURCE=meetstream
   LLM_MODE=gemini
   OUTPUT_DIR=out/live
   MEETSTREAM_API_KEY=
   GEMINI_API_KEY=
   MEETSTREAM_WEBHOOK_SECRET=
   ```

2. Point an HTTPS tunnel or reverse proxy at `127.0.0.1:3000`. For example, with ngrok configured, run `ngrok http 3000` in another terminal. In MeetStream, open **Configure → Webhooks → Add Webhook**. Set the URL to your HTTPS host followed by `/webhook`, and subscribe to `transcription.processed`. Save the generated signing secret to `MEETSTREAM_WEBHOOK_SECRET` in `.env`. [Local tunnel setup](https://docs.meetstream.ai/guides/webhooks/local-webhook-server) · [Workspace webhook setup](https://docs.meetstream.ai/guides/webhooks/workspace-webhooks)
3. Run `make start`. Visit `/health` through the public URL and confirm it returns `{"status":"ok"}`. Start a MeetStream bot in the test call with a **post-call** transcription provider. End the meeting after a short discussion. [Post-call transcription setup](https://docs.meetstream.ai/guides/transcription-recordings/post-call-transcription)
4. In the webhook delivery log, confirm the delivery reached this endpoint. Check `.run/server.log` for a processed run, then open both files under `out/live/<bot_id>/<run_id>/`. Compare the recap and draft with the relevant transcript lines. Resend the delivery for this endpoint to verify the duplicate response and confirm both files remain unchanged.
5. Run `make stop`. Close the temporary tunnel and disable its test webhook when finished.

The app receives the signed workspace notification, reads the transcript from MeetStream, and sends the text to Gemini. It saves the recap and draft to disk. Keep `.env`, live logs, and generated files local.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `make start` fails | Check `.run/server.log`. If port 3000 is busy, set `PORT=3001` in `.env` and use that port for the tunnel too. |
| Signature rejected | Copy the signing secret for this workspace endpoint into `.env` and restart. Confirm the delivery is a signed workspace event and the clock is correct. |
| Event accepted but files are missing | HTTP 202 means the worker queued it. Check the log for transcript or provider errors. Fix the cause and resend the event to this endpoint. |
| Meeting finishes without a transcription event | Confirm the bot used a post-call transcription provider and check transcript status in the MeetStream dashboard. |
| Gemini smoke skipped | Save `GEMINI_API_KEY` or `GOOGLE_API_KEY` and repeat `make test-live`. |
| A fixture replay reports a duplicate | The ledger already has the run. Use a fresh directory under `out/` to exercise the first delivery again. |
