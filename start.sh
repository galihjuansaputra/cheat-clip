#!/usr/bin/env bash

echo "========================================================"
echo "   Starting CHEAT CLIP..."
echo "========================================================"
echo ""
echo "Frontend: http://localhost:5173"
echo "Backend:  http://localhost:8000"
echo "Press Ctrl+C anytime to stop."
echo ""

# Launch web browser after a 3-second delay in background
(sleep 3 && (open http://localhost:5173 2>/dev/null || xdg-open http://localhost:5173 2>/dev/null || sensible-browser http://localhost:5173 2>/dev/null)) &

# Start both frontend and backend concurrently
npm run dev
