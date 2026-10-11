# Advantage Player — bot and dashboard image (Linux → uvloop).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY config.yaml ./
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 1000 ap && mkdir -p /data && chown ap /data
USER ap

ENTRYPOINT ["python", "-m", "advantage_player"]
CMD ["run"]
