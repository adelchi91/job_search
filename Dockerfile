# job-hunter web service + daily run, for Railway (or any Docker host).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app

# dependencies first, so code changes do not reinstall them
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra llm --extra apply --extra boards
RUN playwright install --with-deps chromium

# Claude Code CLI (pinned; the admin signs it in from Admin → Claude account).
# Its login lives on the volume (CLAUDE_CONFIG_DIR) so it survives redeploys.
ARG CLAUDE_CODE_VERSION=2.1.325
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://claude.ai/install.sh | bash -s "$CLAUDE_CODE_VERSION" \
    && install -m 755 "$(readlink -f /root/.local/bin/claude)" /usr/local/bin/claude \
    && rm -rf /root/.local/bin/claude /root/.local/share/claude /root/.claude* \
    && rm -rf /var/lib/apt/lists/* && claude --version

COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra llm --extra apply --extra boards

# /data is the Railway volume: allowlist.yaml and users/<name>/ live there
# JOB_HUNTER_REQUIRE_AUTH: never serve the open single-user mode on a public URL
# CLAUDE_CONFIG_DIR: the admin's Claude login, kept out of every user's folder (users/<name>/)
ENV DATA_DIR=/data JOB_HUNTER_HEADLESS=1 HOST=0.0.0.0 JOB_HUNTER_REQUIRE_AUTH=1 \
    CLAUDE_CONFIG_DIR=/data/.claude DISABLE_AUTOUPDATER=1
EXPOSE 8000
CMD ["sh", "-c", "mkdir -p \"$CLAUDE_CONFIG_DIR\" && chmod 700 \"$CLAUDE_CONFIG_DIR\" && exec job-hunter --data \"$DATA_DIR\" serve --host 0.0.0.0"]
