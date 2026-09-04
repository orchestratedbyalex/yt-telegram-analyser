FROM node:20-slim

# Install Python (3.12 ships with bookworm-slim) + pip + a couple of utilities
# codex CLI may need bubblewrap to honor sandboxing; install it to silence
# the "vendored bubblewrap" warning Codex emits on each run.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv ca-certificates bubblewrap curl \
    && rm -rf /var/lib/apt/lists/*

# Install OpenAI Codex CLI globally
# Pinned: unpinned installs silently stay on whatever the cached layer had.
# Bump deliberately; re-run the health check in the wiki afterwards.
RUN npm install -g @openai/codex@0.152.1

# Run as the non-root `node` user (uid 1000) that node:20-slim ships with.
# The codex auth volume is mounted at /home/node/.codex and must be writable
# by uid 1000 — see README "Codex login".

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY main.py .
COPY prompts ./prompts/

# codex needs HOME to be /home/node so it finds .codex/
ENV HOME=/home/node
RUN mkdir -p /home/node/.codex && chown -R node:node /home/node /app

USER node

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
