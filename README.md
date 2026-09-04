# yt-telegram-analyser

Send a YouTube link to a Telegram bot, get back an AI summary. Reply `/verify`
to have a second model fact-check that summary against the web, or `/roadmap`
to turn the video into a learning plan.

Built and maintained by [orchestratedbyalex](https://github.com/orchestratedbyalex).

```
Telegram ──► n8n workflow ──► yt-transcript ──► YouTube captions
                 │                  └──► SearchAPI.io (fallback)
                 ├──► Fabric (/chat) ──► Gemini / OpenAI / Anthropic …
                 └──► yt-research ──► OpenAI Codex CLI (web search)
```

## What you get

| Command | What it does | Backed by |
|---|---|---|
| `/summary <url>` (default, a bare URL works) | Video summary | Fabric `youtube_summary` |
| `/wisdom <url>` | Key insights, quotes, ideas | Fabric `extract_wisdom` |
| `/chapters <url>` | Timestamped chapter markers, rendered as `youtu.be/<id>?t=` deep links | Fabric `create_video_chapters` |
| `/claims <url>` | Fact-check the claims made in the video | Fabric `analyze_claims` |
| `/song <url>` | Song meaning analysis | Fabric `extract_song_meaning` |
| `/verify` (reply, or tap the 🔍 button) | Independent web fact-check of the last analysis | Codex CLI |
| `/roadmap` (reply, or tap the 🗺️ button) | Learning roadmap built from the video plus web sources | Codex CLI |
| `/commands` | Help text | – |

Add a language flag after the command for non-English videos: `/summary -es <url>`.
Each reply ends with a `/verify · /roadmap · id:<sid>` footer. Session context
lives in n8n workflow static data for 7 days.

## Components

| Directory | What it is |
|---|---|
| `n8n/YoutubeAnalyser.json` | The n8n workflow (27 nodes). Import it into your n8n instance. |
| `yt-transcript/` | FastAPI sidecar. Fetches captions with `youtube-transcript-api`, falls back to SearchAPI.io when YouTube bot-walls you. Internal only, port 8000. |
| `yt-research/` | FastAPI sidecar. Wraps `codex exec --json` for `/verify` and `/roadmap`, streams progress back into the Telegram message. Internal only, port 8000. |
| `fabric-config/` | Where [Fabric](https://github.com/danielmiessler/fabric) keeps its `.env` and downloaded patterns. Bind-mounted into the Fabric container. |
| `docker-compose.yml` | Runs the three sidecars, optionally n8n as well. |

Nothing here is exposed to the internet except n8n (which Telegram must reach).

## Prerequisites, step by step

Work through these in order. Every account and download you need is listed
with where to get it.

### 0. A host with Docker

- Any Linux box (or a Mac) with **Docker Engine 24+ and the Compose v2 plugin**.
  Install from <https://docs.docker.com/engine/install/>.
- Roughly 2 GB free RAM for the four containers, 2 GB disk for images.
- **A public HTTPS URL for n8n.** Telegram delivers updates by webhook and will
  only talk to HTTPS. Options:
  - a domain plus a reverse proxy with TLS (Traefik, Caddy, nginx + certbot), or
  - a tunnel for testing: [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
    or [ngrok](https://ngrok.com/download) pointing at `localhost:5678`.

### 1. Telegram bot token (required)

1. Open Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot`.
2. Pick a display name and a username ending in `bot`.
3. Copy the token BotFather prints. It looks like `123456789:AAF…`. You will
   paste it twice: into `.env` and into an n8n credential.
4. Optional but nice: `/setcommands` on BotFather and paste

   ```
   summary - Video summary (default)
   wisdom - Key insights, quotes and ideas
   chapters - Timestamped chapter markers
   claims - Fact-check claims in the video
   song - Song meaning analysis
   verify - Web fact-check of the last analysis (reply)
   roadmap - Learning roadmap (reply or URL)
   commands - Show help
   ```

### 2. An LLM API key for Fabric (required)

Fabric does the summarising. It supports many vendors; this project was built
and tested with **Google Gemini 2.5 Pro**, which handles long transcripts well
and has a free tier.

- Gemini: create a key at <https://aistudio.google.com/apikey>.
- Or OpenAI (<https://platform.openai.com/api-keys>), Anthropic
  (<https://console.anthropic.com/>), or any other vendor Fabric lists in
  `fabric --listvendors`.

You do not install Fabric yourself. It runs from the published container image
`ghcr.io/ksylvan/fabric` (pinned in `docker-compose.yml`).

### 3. SearchAPI.io key (optional, recommended)

YouTube periodically blocks caption fetches from datacenter IPs with a
"confirm you're not a bot" wall. When that happens the transcript sidecar falls
back to SearchAPI.io's `youtube_transcripts` engine.

- Sign up at <https://www.searchapi.io/> and copy the API key from the dashboard.
- The free tier is **100 requests per month**. Only fallback calls count.
- Without a key, blocked videos return a `429` and the bot tells you the
  transcript could not be fetched.

Alternative or complement: export a Netscape-format `youtube-cookies.txt` from
a logged-in browser session (for example with the
[Get cookies.txt LOCALLY](https://github.com/kairi003/Get-cookies.txt-LOCALLY)
extension), put it next to `docker-compose.yml`, and uncomment the volume line
in the `yt-transcript` service. Cookies expire after a few weeks, so the API
key is the more durable option.

### 4. OpenAI Codex login (optional, needed for `/verify` and `/roadmap`)

The research commands run the [OpenAI Codex CLI](https://github.com/openai/codex)
inside the `yt-research` container. It is installed by the Dockerfile
(`npm install -g @openai/codex`, version pinned). What you need to bring is an
**OpenAI login with Codex access**, either:

- a ChatGPT Plus/Pro/Team account (Codex is included), or
- an OpenAI API key with access to the model you set in `CODEX_MODEL`.

You log in once after the first `docker compose up` (step 6). The credentials
persist in the `codex-auth` Docker volume.

If you skip this, everything else works. `/verify` and `/roadmap` will answer
with an error message.

### 5. n8n (required)

n8n runs the Telegram bot and glues everything together.

- **Already have n8n?** Any recent 1.x works. It must be able to reach the
  sidecars by service name on the `yt-analyser` Docker network (see step 6c).
- **Don't have n8n?** `docker compose --profile n8n up` starts one for you on
  port 5678. Put it behind your HTTPS reverse proxy or tunnel from step 0 and
  set `N8N_HOST`, `N8N_PROTOCOL` and `N8N_WEBHOOK_URL` in `.env` to the public
  URL. n8n docs: <https://docs.n8n.io/hosting/installation/docker/>.

## Installation

### 6. Configure and start the sidecars

```bash
git clone https://github.com/orchestratedbyalex/yt-telegram-analyser.git
cd yt-telegram-analyser

# a) secrets for the sidecars
cp .env.example .env
#    edit .env: TELEGRAM_BOT_TOKEN, SEARCHAPI_KEY (optional), n8n URL if bundled

# b) Fabric vendor + key
cp fabric-config/.env.example fabric-config/.env
#    edit fabric-config/.env: DEFAULT_VENDOR, DEFAULT_MODEL, the matching *_API_KEY

# c) start (add --profile n8n if you want the bundled n8n)
docker compose config -q            # validates the file
docker compose up -d --build

# d) download Fabric's pattern library into fabric-config/patterns (once)
docker compose exec fabric fabric --updatepatterns

# e) log Codex in (once; skip if you don't want /verify and /roadmap)
docker compose exec yt-research codex login --device-auth
#    …follow the printed URL and code. Or, with an API key:
#    docker compose exec -e OPENAI_API_KEY=sk-… yt-research \
#      sh -c 'printenv OPENAI_API_KEY | codex login --with-api-key'
docker compose exec yt-research codex login status
```

If you run your own n8n outside this compose file, attach it to the network so
`http://yt-transcript:8000`, `http://fabric:8080` and `http://yt-research:8000`
resolve from inside n8n:

```bash
docker network connect yt-analyser <your-n8n-container>
```

Smoke test the sidecars from the host:

```bash
docker compose exec yt-transcript python -c "import urllib.request,json;print(urllib.request.urlopen('http://localhost:8000/health').read())"
docker compose exec fabric fabric --listpatterns | head
docker compose exec yt-research codex exec --skip-git-repo-check --model gpt-5.4 "reply OK"
```

### 7. Import the workflow into n8n

1. Open n8n, **Workflows → Add workflow → ⋯ → Import from File**, choose
   `n8n/YoutubeAnalyser.json`.
2. Create the Telegram credential: **Credentials → Add → Telegram API**, name
   it anything, paste the bot token from step 1. Then open the imported
   workflow and select that credential on every Telegram node (there are nine;
   n8n usually offers to apply it to all).
3. **Activate** the workflow (toggle top right). n8n registers the Telegram
   webhook at your public URL. If activation fails with a webhook error, your
   `WEBHOOK_URL` is not reachable over HTTPS from the internet.
4. Send your bot a YouTube link. You should see "Processing…" within a second
   and a summary after 20 to 60 seconds.

The workflow calls the sidecars at fixed internal URLs. If you rename services
in `docker-compose.yml`, update the three **HTTP Request** nodes.

## Configuration reference

| Variable | File | Default | Notes |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | `.env` | – | Same token as the n8n credential. yt-research uses it to edit the "researching…" message live. |
| `SEARCHAPI_KEY` | `.env` | empty | Transcript fallback. |
| `CODEX_MODEL` | `.env` | `gpt-5.4` | Must be a model your Codex login may use. |
| `CODEX_REASONING_EFFORT` | `.env` | `medium` | `none` is faster but noticeably sloppier at fact-checking. |
| `CODEX_TIMEOUT_S` | `.env` | `600` | The n8n "HTTP: Call yt-research" node timeout is 660 s. Keep it above this. |
| `DEFAULT_VENDOR`, `DEFAULT_MODEL`, `*_API_KEY` | `fabric-config/.env` | Gemini / gemini-2.5-pro | Fabric vendor and model. |

The cost footer under each summary is computed in **Code: Chunk for
Telegram** with Gemini 2.5 Pro list prices hardcoded. If you use another model,
edit the two per-million-token rates in that node.

Research prompts live in `yt-research/prompts/` and are baked into the image.
They cap Codex at 8 web searches per run so a `/verify` finishes in about two
minutes. Edit them and `docker compose up -d --build yt-research`.

## Running the tests

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r yt-transcript/requirements.txt -r yt-research/requirements.txt pytest httpx
(cd yt-transcript && pytest -q)
(cd yt-research && pytest -q)
```

No network access needed. The yt-research tests parse recorded Codex output
from `tests/fixtures/`.

## Known limits

- **YouTube bot walls.** Direct caption fetches from cloud IPs get blocked in
  waves. The SearchAPI fallback exists for that reason. Residential IPs rarely
  see it.
- **SearchAPI free tier** is 100 requests per month.
- **Telegram message size.** Replies are chunked at 4096 characters. The
  inline Verify/Roadmap buttons go on the last chunk.
- **Codex CLI drift.** The CLI version is pinned in `yt-research/Dockerfile`.
  When bumping it, re-run the `reply OK` smoke test above: a `400` naming the
  model means the model is no longer offered to your account.
- **Cost.** One summary is one Fabric call with the full transcript as input.
  One `/verify` is one Codex run of roughly 30 to 60k tokens, dominated by
  fetched web pages.

## How the workflow is structured

1. **Trigger: Telegram** receives `message` and `callback_query` updates.
2. **Code: Extract URL** parses the command, language flag and URL, or routes
   `/verify` and `/roadmap` (typed or via button) to the research path.
3. **HTTP: Fetch Transcript** → `yt-transcript`.
4. **HTTP: Fabric Chat** posts the transcript and pattern name to Fabric's
   `/chat`; **Code: Parse SSE** reads the streamed reply and the usage event
   that feeds the cost footer.
5. **Code: Chunk for Telegram** stores the session for later `/verify` and
   `/roadmap`, appends the token/cost footer, and splits the reply into
   Telegram-sized chunks that the reply nodes send.
6. Research path: **Code: Resolve Session** looks up the cached transcript and
   analysis by session id, **Telegram: Ack Research** posts the "researching…"
   message that `yt-research` then edits in place, **HTTP: Call yt-research**
   runs Codex, **Code: Format Result** turns the Markdown into Telegram HTML.

## License

GNU Affero General Public License v3.0. See `LICENSE`.

Fabric (MIT), youtube-transcript-api (MIT), OpenAI Codex CLI (Apache 2.0) and
n8n (Sustainable Use License) are separate projects under their own licenses.
