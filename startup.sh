#!/usr/bin/env bash
set -euo pipefail

cd /home/site/wwwroot

echo "== Python =="
python -V || true

# Create a clean venv if missing or broken
if [ ! -x "antenv/bin/python" ]; then
  echo "== Creating venv =="
  rm -rf antenv
  python -m venv antenv
fi

echo "== Installing requirements =="
./antenv/bin/python -m pip install --upgrade pip
./antenv/bin/pip install -r requirements.txt

echo "== Starting gunicorn =="
exec ./antenv/bin/gunicorn \
  -k uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:${PORT:-8000} \
  --workers 2 \
  --timeout 600 \
  main:app
