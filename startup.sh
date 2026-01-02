#!/usr/bin/env bash
set -euo pipefail

cd /home/site/wwwroot

echo "== Python =="
python3 --version

VENV="/home/site/antenv"

if [ ! -d "$VENV" ]; then
  echo "== Creating venv at $VENV =="
  python3 -m venv "$VENV"
fi

source "$VENV/bin/activate"

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

exec gunicorn -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:${PORT:-8000} --workers 2 --timeout 600 main:app
