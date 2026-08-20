FROM docker.io/tiangolo/uvicorn-gunicorn-fastapi:python3.11-slim

# Install dependencies
RUN apt-get update
RUN apt-get --yes install curl \
    build-essential \
    libffi-dev \
    libpq-dev \
    git \
    redis
#RUN rm -rf /var/lib/apt/lists/*

# Install Poetry
RUN curl -sSL https://install.python-poetry.org | python3 -
RUN ~/.local/share/pypoetry/venv/bin/poetry config virtualenvs.create false

WORKDIR /app

COPY pyproject.toml poetry.lock ./
COPY rctab ./rctab

RUN ~/.local/share/pypoetry/venv/bin/poetry install --only main

COPY alembic ./alembic
COPY scripts/prestart.sh ./
COPY alembic.ini ./
COPY redis.conf ./

ENV APP_MODULE="rctab:app"
