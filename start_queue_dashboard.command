#!/bin/zsh
cd "${0:A:h}" || exit 1

VENV_DIR=".venv"
PYTHON="$VENV_DIR/bin/python"

echo "Tell Me Why — Queue Dashboard"
echo "============================="
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is not installed. Install it with: brew install python"
  echo
  read "?Press Return to close this window."
  exit 1
fi

if [[ ! -x "$PYTHON" ]] || ! "$PYTHON" -c 'import sys' >/dev/null 2>&1; then
  echo "Repairing the local Python environment…"
  if ! python3 -m venv --clear "$VENV_DIR"; then
    echo "Could not create $VENV_DIR."
    echo
    read "?Press Return to close this window."
    exit 1
  fi
fi

if ! "$PYTHON" -c 'import queue_web' >/dev/null 2>&1; then
  echo "Installing dashboard dependencies…"
  if ! "$PYTHON" -m pip install -r requirements.txt; then
    echo "Dependency installation failed. Check your internet connection and try again."
    echo
    read "?Press Return to close this window."
    exit 1
  fi
fi

if ! "$PYTHON" -c 'import queue_web' >/dev/null 2>&1; then
  echo "The dashboard still cannot start after installing its dependencies."
  echo "Run this command in Terminal for details:"
  echo "  cd '${0:A:h}' && $PYTHON -c 'import queue_web'"
  echo
  read "?Press Return to close this window."
  exit 1
fi

echo "Opening the dashboard…"
exec "$PYTHON" queue_web.py
