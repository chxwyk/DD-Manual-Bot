FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dash_bot ./dash_bot

RUN mkdir -p /app/data/transcripts \
    && useradd --create-home --uid 10001 botuser \
    && chown -R botuser:botuser /app

USER botuser

CMD ["python", "-m", "dash_bot"]
