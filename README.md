# Personal AI Agent — Coder PoC

English | [日本語](README.ja-JP.md)

A proof of concept of an **always-on personal AI agent that runs in a dedicated workspace per user**. [Coder](https://coder.com/) provisions one Docker container per user, running a FastAPI app. Each user enters **their own Azure OpenAI API key** as a Coder workspace parameter and talks to a Realtime model (default `gpt-realtime-2.1-mini`) through both **web chat** and **voice chat**. Conversation history (text and voice) and scheduled jobs are stored in SQLite on a persistent volume, and text and voice share the same context.

> [!WARNING]
> This is a PoC, **not** a production-ready enterprise system. Read [Production gaps and security](#production-gaps-and-security) before using it beyond evaluation.

## Features

| Feature | Status |
| --- | --- |
| Web chat (server-side WebSocket to the Realtime model, text output) | ✅ Implemented |
| Voice chat (browser ⇔ Azure over WebRTC; the backend relays the SDP) | ✅ Implemented |
| Per-user API key (Coder workspace parameter, masked input) | ✅ Implemented |
| Shared history across text, voice and job results (recent history is included in the instructions) | ✅ Implemented |
| Daily scheduled jobs at a given hour/minute (APScheduler cron) | ✅ Implemented |
| Always-on (tini, restart policy, health check, embedded Coder agent) | ✅ Implemented (disable auto-stop in the template settings) |
| SQLite persistence (`/data/agent.db`) | ✅ Implemented |
| Creating schedules from natural language | ⏳ Planned |
| Web search (Realtime function tool → Responses API `web_search`, with sources), in text, voice and jobs | ✅ Implemented |
| Agent Skills import, export and management (list, enable/disable, edit SKILL.md, create, delete), used by the agent | ✅ Implemented |
| MCP tools (sending email, …) | ⏳ Planned |

With `WEB_SEARCH_DEPLOYMENT` set, the model calls a `web_search` tool for questions that need current information. The backend uses the same API key to have a chat deployment that supports the Responses API web search tool (e.g. `gpt-6.1-terra`) search the web, and returns a summary with sources to the model. In voice chat, the browser runs the tool calls it receives through `/api/tools/call`.

## Chats and rendering

- **Chats:** listed in the left pane (at the bottom on narrow screens). "+ New chat" starts one, clicking opens a past chat, 🗑 deletes it. Titles come from the first message. Only that chat's history goes into the model's context. Voice transcripts are saved to the chat where the call started. Scheduled job results go to "⏰ 定期ジョブの結果".
- **Migration:** history from before chats existed becomes one chat named "これまでの会話" on first start.
- **Rich rendering:** agent replies render as Markdown (tables, headings, links) using [marked](https://marked.js.org/) and [DOMPurify](https://github.com/cure53/DOMPurify) from cdnjs, falling back to plain text if they cannot load.
- **Infographics:** ask for a visual in text chat and the model returns a self-contained HTML/SVG ```` ```html ```` block, which the page draws. It is rendered in a sandboxed iframe (no scripts, forms, popups or navigation) with a CSP that blocks every external load. Voice replies describe the content instead.

## Agent Skills

Skills in the [Agent Skills](https://agentskills.io) format (a folder containing `SKILL.md`) are managed in the "Skills" section of the web UI.

- **Storage:** `/data/skills/<name>/` (persistent volume). Enabled/disabled state is in `/data/skills/.state.json`.
- **Import:** a skill folder as `.zip` / `.skill` (several skills per zip are fine), or a bare `SKILL.md`. A skill with the same name is replaced only when "overwrite" is checked.
- **Export:** one skill or all skills as a zip (`<name>/SKILL.md ...`), usable as-is by other Agent Skills tools.
- **Default skills:** `weekly-weather` (weekly forecast drawn as an infographic, written for this project) and Anthropic's [skill-creator](https://github.com/anthropics/skills/tree/main/skills/skill-creator) (Apache-2.0, unmodified) is bundled and copied into `/data/skills` on first start. Once seeded it is never re-added, even if the user disables, edits or deletes it. This environment cannot run its scripts, sub-agents or eval loop, so the agent skips those steps and saves with the save tools. The upstream commit is recorded in [app/default_skills/README.md](app/default_skills/README.md).
- **Reload:** the "⟳ Reload" button refreshes the list from storage (for edits made in a terminal, another tab or a voice session).
- **Create and edit from chat:** ask "make a skill for ..." and the agent writes SKILL.md and saves it with `save_skill`; bundled references are written with `write_skill_file`. To change an existing skill it reads it with `load_skill` first. The agent cannot delete skills (delete them in the UI).
- **Use by the agent:** small skills (SKILL.md body up to 8,000 characters, 16,000 in total) are inlined into the instructions so their steps and output format always apply (a light model often skips load_skill). For larger skills only `name` and `description` go into the instructions. When a request matches, the model calls `load_skill` (SKILL.md plus the file list) and `read_skill_file` (bundled files). Works in text, voice and scheduled jobs.
- **Limits:** scripts inside skills are **never executed** (read-only). Import rejects parent-directory or absolute paths, symlinks and oversized archives (over 20MB, over 50MB extracted, over 300 files, or any file over 5MB). `name` must be lowercase letters, digits and hyphens (up to 64 characters); `description` is required (up to 1024 characters).

## Architecture

```
Browser ──HTTPS──► Coder (Entra ID auth, app proxy, share=owner)
   │                   │
   │                   ▼
   │         Workspace container (one per user, restart=unless-stopped)
   │         ┌──────────────────────────────────────────────────────────┐
   │         │ tini ─┬─ Coder agent (background, auto-restart)           │
   │         │       └─ FastAPI :8000                                    │
   │         │            ├─ Web chat ──WebSocket(api-key)──► Azure OpenAI Realtime
   │         │            ├─ Voice: SDP relay ─client_secrets→calls──► 〃
   │         │            ├─ APScheduler (jobs) ──WebSocket──► 〃
   │         │            └─ SQLite (/data volume)                     │
   │         └──────────────────────────────────────────────────────────┘
   └───────── voice media (WebRTC, UDP/TCP) ───────────────────────────► Azure OpenAI Realtime
```

- **The API key never reaches the browser.** For voice, the browser sends its SDP offer to the backend. The backend uses the user's key to mint a short-lived client secret (`/openai/v1/realtime/client_secrets`), exchanges SDP with Azure's `/openai/v1/realtime/calls` using that secret, and returns only the SDP answer.
- Voice transcripts (user speech and agent replies) are taken from data-channel events and saved to history via `/api/transcripts`.

## Repository layout

```
.
├── app/main.py           # FastAPI: API, Realtime (WebSocket / WebRTC relay), tools, scheduler, SQLite
├── app/skills.py         # Agent Skills storage, import/export, validation, default-skill seeding, model tools
├── app/default_skills/   # Default skills (Anthropic skill-creator, Apache-2.0)
├── app/static/index.html # Web UI (chat, voice, skills, scheduled jobs; skills and jobs in a left pane on wide screens)
├── entrypoint.sh         # Starts the Coder agent (optional) in the background, runs uvicorn in the foreground
├── Dockerfile            # python:3.12-slim + tini + curl, non-root (UID 10001), HEALTHCHECK
├── requirements.txt      # fastapi, uvicorn, httpx, websockets, apscheduler, pydantic, PyYAML
└── terraform/main.tf     # Coder template (Docker provider)
```

## API

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/` | Web UI |
| `GET` | `/health` | Health check (`{"ok": true}`) |
| `GET` | `/api/status` | Configuration state (whether a key is set, deployment, endpoint host; never the key) |
| `POST` | `/api/chat` | Send `{"message": "...", "conversation_id": "..."}`, get `{"answer": "...", "conversation_id": "..."}` (omit `conversation_id` to start a new chat) |
| `GET` / `POST` | `/api/conversations` | List chats (most recent first) / create one |
| `PATCH` / `DELETE` | `/api/conversations/{id}` | Rename with `{"title": "..."}` / delete the chat and its messages |
| `POST` | `/api/realtime/sdp?conversation_id=...` | Send an SDP offer (`application/sdp`), get the SDP answer |
| `POST` | `/api/tools/call` | Run a tool call from the voice session: `{"name": "web_search", "arguments": "{\"query\": \"...\"}"}` |
| `POST` | `/api/transcripts` | Save a voice transcript: `{"role": "user"\|"assistant", "content": "...", "conversation_id": "..."}` |
| `GET` | `/api/skills` | List skills (name, description, enabled, file count, errors) |
| `POST` | `/api/skills` | Create from `{"content": "<SKILL.md>"}` |
| `POST` | `/api/skills/import?filename=x.zip&overwrite=false` | Import the file in the body (.zip / .skill / SKILL.md) |
| `GET` | `/api/skills/export` | Download all skills as a zip |
| `GET` / `PUT` / `PATCH` / `DELETE` | `/api/skills/{name}` | Get / update SKILL.md / `{"enabled": bool}` / delete |
| `GET` | `/api/skills/{name}/export` | Download one skill as a zip |
| `GET` | `/api/skills/{name}/files/{path}` | Content of a bundled file |
| `GET` | `/api/skills/template?name=...` | SKILL.md template for a new skill |
| `GET` | `/api/history?conversation_id=...` | Last 200 messages of that chat (`channel` is `text` / `voice` / `job`) |
| `POST` | `/api/jobs` | Register a daily job: `{"hour": 7, "minute": 0, "prompt": "..."}` |
| `GET` | `/api/jobs` | List jobs |
| `DELETE` | `/api/jobs/{id}` | Delete a job |

## Configuration (environment variables)

Under Coder, the main values come from workspace parameters (see [Coder template](#coder-template)).

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `AZURE_OPENAI_ENDPOINT` | Yes | — | e.g. `https://YOUR-RESOURCE.openai.azure.com` (a `/openai/v1` suffix is fine) |
| `AZURE_OPENAI_API_KEY` | Yes | — | The user's own API key |
| `AZURE_OPENAI_REALTIME_DEPLOYMENT` | No | `gpt-realtime-2.1-mini` | Realtime deployment name (the old `AZURE_OPENAI_DEPLOYMENT` is also read) |
| `REALTIME_VOICE` | No | `marin` | Voice |
| `REALTIME_TRANSCRIBE_MODEL` | No | `whisper-1` | Model that transcribes user speech; empty disables transcription |
| `REALTIME_TRANSCRIBE_LANGUAGE` | No | `ja` | Transcription language |
| `AZURE_OPENAI_REALTIME_WEBRTC_URL` | No | `{endpoint}/openai/v1/realtime/calls?webrtc_filter=on` | SDP exchange URL (override for a regional URL) |
| `WEB_SEARCH_DEPLOYMENT` | No | empty (no search) | Chat deployment used for web search (supports the Responses API `web_search` tool, e.g. `gpt-6.1-terra`) |
| `AGENT_INSTRUCTIONS` | No | Built-in Japanese assistant prompt | System instructions |
| `CONTEXT_MESSAGES` | No | `20` | Number of recent messages included in the instructions |
| `APP_TIMEZONE` | No | `Asia/Tokyo` | Time zone for schedules and timestamps |
| `DATA_DIR` | No | `/data` | SQLite location |
| `CODER_AGENT_INIT_SCRIPT` | No | — | Set by Coder; when present the entrypoint starts the Coder agent |

## Azure setup

1. Deploy a Realtime model in your Azure OpenAI (or Foundry) resource. The default deployment name is `gpt-realtime-2.1-mini`; if you use another name, set it in the "Realtime deployment" parameter. Check that the model is offered in your region.
2. To give each user their own key, create a resource per user (or per team) and hand out its key and endpoint.
3. Check that a transcription model such as `whisper-1` is available. If it is not, change `REALTIME_TRANSCRIBE_MODEL` or set it to empty; with it empty, user speech is not saved to history (agent replies still are).

## Build

```sh
docker build -t personal-ai-agent:poc .
```

## Local test (without Coder)

```sh
docker volume create personal-agent-test
cat > .env <<'EOF'
AZURE_OPENAI_ENDPOINT=https://YOUR-RESOURCE.openai.azure.com
AZURE_OPENAI_API_KEY=YOUR-KEY
AZURE_OPENAI_REALTIME_DEPLOYMENT=gpt-realtime-2.1-mini
EOF
docker run --rm -p 127.0.0.1:8000:8000 -v personal-agent-test:/data --env-file .env personal-ai-agent:poc
```

Open http://localhost:8000. Browsers treat `localhost` as a secure context, so the microphone works. `.env` is already in `.gitignore`.

## Coder template

1. Build the image and push it to a registry the workspace Docker daemon can pull from.
2. Push `terraform/` as a new template:

   ```sh
   coder templates push personal-agent -d terraform \
     --variable image=REGISTRY/personal-ai-agent:poc \
     --variable azure_openai_endpoint=https://YOUR-RESOURCE.openai.azure.com
   ```

   | Template variable | Default | Description |
   | --- | --- | --- |
   | `image` | `personal-ai-agent:poc` | Image |
   | `azure_openai_endpoint` | empty | Default endpoint (users can override) |
   | `realtime_deployment` | `gpt-realtime-2.1-mini` | Default deployment name (users can override) |
   | `web_search_deployment` | empty | Default web search deployment (users can override; empty = no search) |
   | `app_subdomain` | `false` | `true` serves the app on a subdomain (needs a wildcard access URL) |

3. **Always-on.** Disable template auto-stop:

   ```sh
   coder templates edit personal-agent --default-ttl 0s
   # Premium: also stop users overriding auto-stop, and disable the weekly autostop requirement, dormancy and dormant auto-deletion
   coder templates edit personal-agent --allow-user-autostop=false --autostop-requirement-weekdays none --dormancy-threshold 0s --dormancy-auto-deletion 0s
   ```

   Flag names can change between Coder versions; check `coder templates edit --help`. The container uses `restart=unless-stopped` with tini as PID 1. If the app crashes, Docker restarts it, and the container comes back after a Docker daemon or host restart. The Coder agent is restarted 5 seconds after it exits.
4. **What users do.** When creating a workspace, users are asked for the parameters below. They can change them later under Settings, which rebuilds the workspace.
   - **Azure OpenAI API key (personal)**: required, masked input
   - **Azure OpenAI endpoint**: defaults to the template variable
   - **Realtime deployment**, **web search deployment** and **voice**
5. Open the "Personal AI Agent" app in the workspace. It is `share = "owner"`, so only the owner can reach it. Voice needs HTTPS (the Coder access URL).
6. Network requirements:
   - The container can reach Azure over HTTPS and WSS.
   - The browser can reach Azure's WebRTC media (UDP, or the TCP/443 fallback).
   - The container can reach the Coder access URL (to download and connect the agent). An access URL of `localhost` is rewritten to `host.docker.internal`.

## Production gaps and security

- **Where the API key lives.** The personal key is stored as a Coder workspace build parameter in the Coder database and Terraform state, and is passed to the container as an environment variable (visible in `docker inspect`). The workspace owner and Coder admins can see it. For production, consider fetching it from Key Vault, Coder external auth, or a per-user token broker.
- **Voice credentials.** Not even the short-lived client secret goes to the browser. The backend mints it and performs the SDP exchange itself.
- **Realtime API compatibility.** This targets the Azure OpenAI Realtime GA interface (`/openai/v1/realtime`, `client_secrets`, `calls`, `api-key` header). It has been tested against a mock server, not yet against real Azure with `gpt-realtime-2.1-mini`. Verify web chat, voice and transcription in your environment before rollout.
- **Network exposure.** Make sure users cannot reach container port 8000 directly. The app itself has no authentication and relies on Coder's authenticated proxy (`share = "owner"`).
- **Cost and quotas.** Although the agent is always on, Azure only bills during conversations and job runs. Voice chat bills continuously while connected, so set quotas or alerts per resource.
- **Scheduler.** APScheduler runs in-process, so jobs do not run while the workspace is stopped (the always-on settings above are assumed). Keep one SQLite database and one scheduler process per workspace.
- **Volume.** The data volume has `prevent_destroy`, so deleting a workspace needs manual handling.
- Web browsing and email are not implemented. Before enabling tools, add an MCP tool registry, approval flows, egress limits, logging and prompt-injection defenses.
