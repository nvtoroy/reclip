#!/bin/bash
set -e
cd "$(dirname "$0")"

# Check prerequisites
missing=""

if ! command -v python3 &> /dev/null; then
    missing="$missing python3"
fi

# yt-dlp is intentionally not checked here — it's pinned in requirements.txt and
# installed into venv/, which activation puts ahead of any system copy on PATH.
# A brew-installed yt-dlp is usually older and would only shadow the pin.
if ! command -v ffmpeg &> /dev/null; then
    missing="$missing ffmpeg"
fi

if [ -n "$missing" ]; then
    echo "Missing required tools:$missing"
    echo ""
    if command -v brew &> /dev/null; then
        echo "Install with:  brew install$missing"
    elif command -v apt &> /dev/null; then
        echo "Install with:  sudo apt install$missing"
    else
        echo "Please install:$missing"
    fi
    exit 1
fi

# Set up venv and install Python deps
if [ ! -d "venv" ]; then
    echo "Setting up virtual environment..."
    python3 -m venv venv
fi
source venv/bin/activate

# Run every time, not just on first setup — the yt-dlp pin moves often and an
# existing venv would otherwise stay on an old, broken-extractor version.
# It's a no-op once the pins are already satisfied.
pip install -q -r requirements.txt

PORT="${PORT:-8899}"
export PORT

echo ""
echo "  ReClip is running at http://localhost:$PORT"
echo ""
python3 app.py
