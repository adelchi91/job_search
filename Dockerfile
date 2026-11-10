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

COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra llm --extra apply --extra boards

# /data is the Railway volume: allowlist.yaml and users/<name>/ live there
# JOB_HUNTER_REQUIRE_AUTH: never serve the open single-user mode on a public URL
ENV DATA_DIR=/data JOB_HUNTER_HEADLESS=1 HOST=0.0.0.0 JOB_HUNTER_REQUIRE_AUTH=1
VOLUME ["/data"]
EXPOSE 8000
CMD ["sh", "-c", "job-hunter --data \"$DATA_DIR\" serve --host 0.0.0.0"]
