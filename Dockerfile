FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8899
ENV HOST=0.0.0.0
ENV DOWNLOAD_DIR=/tmp/reclip-downloads
ENV CLEAN_DOWNLOAD_DIR_ON_START=1
CMD ["python", "app.py"]
