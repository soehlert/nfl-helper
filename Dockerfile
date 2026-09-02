FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NFL_HELPER_DB_PATH=/app/data/nfl_helper.db

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
COPY frontend/ ./frontend/
RUN uv sync --frozen --no-dev

RUN mkdir -p /app/data

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "nfl_helper.main:app", "--host", "0.0.0.0", "--port", "8000"]
