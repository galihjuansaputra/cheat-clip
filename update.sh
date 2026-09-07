#!/usr/bin/env bash
set -e

echo "========================================================"
echo "   CHEAT CLIP - Quick Update (Mac/Linux)"
echo "========================================================"
echo ""

if ! command -v git &> /dev/null; then
    echo "[ERROR] Git is not installed!"
    echo "Please install Git from https://git-scm.com/downloads"
    exit 1
fi

echo "[1/3] Pulling latest updates from GitHub..."
git pull || echo "[WARNING] Git pull had warnings."
echo ""

echo "[2/3] Checking and updating frontend packages..."
npm install
echo ""

echo "[3/3] Checking and updating Python backend packages..."
if command -v python3 &> /dev/null; then
    python3 -m pip install -r backend/requirements.txt
else
    python -m pip install -r backend/requirements.txt
fi

echo ""
echo "========================================================"
echo "   Cheat Clip is now up to date!"
echo "========================================================"
echo "You can now run ./start.sh to launch the app."
echo ""
