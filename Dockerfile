FROM python:3.12-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpcsclite-dev swig gcc libc6-dev && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --extra nfc

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpcsclite1 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=builder /app/.venv .venv
COPY pyproject.toml uv.lock .python-version ./
COPY server.py ./
COPY static/ ./static/
EXPOSE 8443
CMD ["uv", "run", "server.py"]
