# Distributed AI Orchestrator — server image
#
# Runs the FastAPI orchestrator. Ollama runs in its own container
# (see docker-compose.yml) or on the host.
#
# Runtime data (config.json, ledger.json, events.db, output/, projects/)
# lives in /data — mount a volume there to persist it.

FROM python:3.14-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY templates/ templates/
# Package directories need copying explicitly — `COPY *.py` only takes the
# top level, and a missing prompts/ breaks `import prompts` at startup.
COPY prompts/ prompts/

# All state files are resolved relative to the working directory
WORKDIR /data
ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
