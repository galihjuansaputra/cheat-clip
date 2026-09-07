#!/usr/bin/env bash
set -e

echo "========================================================"
echo "   CHEAT CLIP - Automatic Installer (Mac/Linux)"
echo "========================================================"
echo ""

# 1. Check Node.js
if ! command -v node &> /dev/null; then
    echo "[ERROR] Node.js is not installed!"
    echo "Please download and install Node.js (LTS version) from:"
    echo "  https://nodejs.org/"
    exit 1
fi
echo "[OK] Node.js is installed ($(node -v))."

# 2. Check Python
if command -v python3 &> /dev/null; then
    PYTHON_CMD="python3"
elif command -v python &> /dev/null; then
    PYTHON_CMD="python"
else
    echo "[ERROR] Python is not installed!"
    echo "Please download and install Python from:"
    echo "  https://www.python.org/downloads/"
    exit 1
fi
echo "[OK] Python is installed ($($PYTHON_CMD --version))."
echo ""

# 3. Install frontend dependencies
echo "[1/3] Installing frontend dependencies (npm install)..."
npm install
echo ""

# 4. Install backend dependencies
echo "[2/3] Installing Python dependencies (pip install)..."
$PYTHON_CMD -m pip install --upgrade pip
$PYTHON_CMD -m pip install -r backend/requirements.txt
echo ""

# 5. Check environment configuration
echo "[3/3] Checking environment configuration..."
if [ ! -f "backend/.env" ] && [ -f "backend/.env.template" ]; then
    cp backend/.env.template backend/.env
    echo "[OK] Created backend/.env from template."
fi

echo ""
echo "========================================================"
echo "   Setup Complete! You are ready to go!"
echo "========================================================"
echo ""
echo "To start Cheat Clip:"
echo "   ./start.sh   OR   npm run dev"
echo ""
