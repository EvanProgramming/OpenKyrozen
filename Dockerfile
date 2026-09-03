# OpenKyrozen Docker Image
# Build:  docker build -t openkyrozen .
# Run:    docker run -p 8000:8000 -v kyrozen-data:/data openkyrozen

FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir fastapi uvicorn

# Copy application code
COPY . .

# Keep the SQLite source of truth and its derived index under one explicit,
# non-root-writable data directory. A named Docker volume mounted at /data is
# therefore sufficient to survive container replacement.
ENV KYROZEN_DB_PATH=/data/openkyrozen.sqlite3 \
    KYROZEN_VECTOR_PATH=/data/chroma_index

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin kyrozen \
    && install -d -o kyrozen -g kyrozen /data \
    && chown -R kyrozen:kyrozen /app

USER kyrozen

VOLUME ["/data"]

# Expose web server port
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=3)"

# Default: run web server
CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8000"]
