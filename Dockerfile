# Distributed AI Orchestrator — server image
#
# Runs the FastAPI orchestrator. Ollama runs in its own container
# (see docker-compose.yml) or on the host.
#
# Runtime data (config.json, ledger.json, events.db,
# capability-shadow-health.db, output/, projects/)
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
COPY execution/ execution/
# Server startup imports the trusted-alpha preflight, and operators need the
# matching backup/restore tools in the immutable image. Copy only runtime
# scripts, not benchmark result fixtures or local bytecode.
COPY scripts/__init__.py scripts/preflight.py scripts/backup.py scripts/restore.py scripts/node_enrollment_admin.py scripts/

# All state files are resolved relative to the working directory
WORKDIR /data
ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

EXPOSE 8000
# The coordinator intentionally supports one process per /data directory. Do
# not add Uvicorn workers; the kernel lock also rejects accidental fan-out.
CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
