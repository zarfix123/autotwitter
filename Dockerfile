# X Growth Engine — single long-lived process (bot + scheduler + admin).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    XGROWTH_DB_PATH=/app/data/xgrowth.db

WORKDIR /app

# Install dependencies first (cached layer); requirements.txt is also what
# pyproject reads for the package's dynamic dependencies.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Install the package itself (src layout -> `xgrowth` importable, no PYTHONPATH).
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps . && mkdir -p /app/data

# Run as a non-root user.
RUN useradd -m -u 10001 appuser && chown -R appuser /app
USER appuser

# Liveness via the admin /health endpoint (bound to 127.0.0.1 inside the container).
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3).status==200 else 1)"

CMD ["python", "-m", "xgrowth.app"]
