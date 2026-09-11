FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg aria2 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home appuser
USER appuser

EXPOSE 8899
ENV HOST=0.0.0.0 \
    DOWNLOAD_DIR=/tmp/reclip-downloads \
    CLEAN_DOWNLOAD_DIR_ON_START=1 \
    PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT','8899') + '/healthz')"

# Single worker: job state lives in process memory. Scale with --threads, not --workers.
CMD ["sh", "-c", "exec gunicorn --workers 1 --threads 8 --timeout 300 --bind 0.0.0.0:${PORT:-8899} app:app"]
