# OpenKyrozen Docker Image
# Build:  docker build -t openkyrozen .
# Run:    docker run -p 8000:8000 -e DEEPSEEK_API_KEY=sk-... openkyrozen

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

# Expose web server port
EXPOSE 8000

# Default: run web server
CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8000"]
