# Zoom Chat Knowledge Hub

Local-first, read-only MVP for indexing Zoom Team Chat channels and messages.

## Current milestone

- Import the existing Zoom OAuth token and protect it with Windows DPAPI.
- Refresh the authorized user's channel catalog.
- Classify selected channels as readable, encrypted, mixed, unknown, or inaccessible.
- Incrementally sync selected channels into SQLite with a five-minute overlap window.
- Detect direct mentions when the current Zoom user/member ID is configured.
- Use OpenAI Responses API Structured Outputs to turn multi-message windows into
  traceable conversation topics with problems, conclusions, open questions, and actions.
- Keep recent conversation topics for a configurable 7/14/30-day maturity period.
- Automatically archive mature and historical conversations into source-language knowledge.
- Use multilingual embeddings, temporary query translation, lexical matching, and structured
  identifiers for hybrid knowledge search.
- Automatically create, update, relate, or flag conflicts while preserving immutable versions
  and original Zoom message evidence.
- Browse status, channels, recent conversation topics, knowledge, and mentions in a local web UI.

Topic extraction sends only selected, readable message windows to the configured
OpenAI API; encrypted placeholders are excluded and Responses calls use `store=False`.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The first run imports the existing token from `..\work\.zoom_tokens.json` when present and stores a DPAPI-protected copy under `data\zoom-token.bin`.

## Run

```powershell
.\run.ps1
```

Open <http://127.0.0.1:8765>.

The application binds to localhost only. It never sends Zoom chat messages, marks
messages as read, or downloads attachment contents. Channel selection and mention
workflow status are local-only changes.

## First-use workflow

1. Open **Channels & Readability**, then click **Refresh from Zoom**.
2. Choose the new-channel initial sync range: 30, 90 (default), or 180 days.
3. Select the channels you want to index.
4. Click **Test Selected Channels** to classify readable, encrypted, and mixed channels.
5. Open **Settings** and provide your Zoom member ID for accurate `@me` matching.
6. Click **Sync Selected Channels**. Future runs continue from each channel's successful watermark with a
   five-minute overlap and database deduplication.

Changing the initial range never deletes or automatically backfills existing data.
Previously initialized channels show an explicit backfill action when their recorded
coverage is shorter than the selected default.

The tool stores message bodies locally in SQLite. Protect the Windows account and
the `data` directory according to your organization's data-retention policy.

## OpenAI topic extraction

Configuration priority:

1. `OPENAI_API_KEY`, `OPENAI_MODEL`, and `OPENAI_BASE_URL`
2. JSON path in `OPENAI_CONFIG_FILE`
3. `data\openai_config.json`
4. `C:\Works\access-redmine\openai_config.json`

The API key is never copied into SQLite or returned by the UI. Topic calls use the
Responses API with `store=False`. Open **Conversation Topics** and click **Extract New Topics**.
Unchanged message windows are skipped by input hash. The extraction range always follows the
Knowledge maturity setting; there is no separate day selector. Mature topics are archived
automatically, and the one-time historical import is available from **Knowledge Base**.

The web interface is English. Open **Settings** to select the AI model tier, preferred query
language, and knowledge maturity period. Knowledge itself always remains in the original
conversation language. The default tier is `mid` (`gpt-5.6-luna`).
Model tiers are intentionally mapped in `zoom_kb/config.py`:

- `low`: `gpt-5-mini`
- `mid`: `gpt-5.6-luna`
- `high`: `gpt-5.6-terra`

AI summaries never replace source evidence. Conversation topics and knowledge items
retain links to the original Zoom Chat messages, which can be expanded verbatim in
the UI. Zoom threads are grouped with their root messages even when replies arrive
days later.

## Test

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

