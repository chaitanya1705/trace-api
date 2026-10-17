# TRACE API + worker in one container (supervised by app.run_all). Works on Railway, Render, Fly or any Docker host.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app
COPY migrations ./migrations
COPY docs/openapi.json ./docs/openapi.json

RUN useradd --create-home --uid 10001 trace && chown -R trace /srv
USER trace

# The host injects PORT; the platform's health check should use /health/ready (or /health/live for a cheap probe).
ENV PORT=8000
EXPOSE 8000
CMD ["python", "-m", "app.run_all"]
