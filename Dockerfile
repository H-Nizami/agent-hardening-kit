# Official Python 3.12.16 slim-bookworm multi-platform index, verified 2026-10-29.
FROM python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANGSMITH_TRACING=false \
    HOME=/tmp
WORKDIR /app

# Build and test tools are included intentionally in this local lab image.
COPY requirements-dev.lock /app/requirements-dev.lock
RUN python -m pip install --no-cache-dir --require-hashes -r requirements-dev.lock
COPY pyproject.toml README.md LICENSE /app/
COPY src /app/src
RUN python -m pip install --no-cache-dir --no-build-isolation --no-deps .

USER 10001:10001
ENTRYPOINT ["python", "-m", "agent_hardening_kit"]
CMD ["worker", "--address", "temporal:7233", "--db", "/data/agent.sqlite", "--queue", "ahk"]
