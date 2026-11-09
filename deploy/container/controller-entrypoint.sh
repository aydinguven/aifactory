#!/bin/sh
set -eu

FORWARDED_ALLOW_IPS="$(python -m app.controller_proxy)"
export FORWARDED_ALLOW_IPS
echo "Trusting forwarded headers from container proxy IPs: ${FORWARDED_ALLOW_IPS}"

echo "Applying AI Factory database migrations..."
python -m app.migrations upgrade

echo "Starting AI Factory controller..."
exec python -m uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers 1 \
    --proxy-headers \
    --forwarded-allow-ips "${FORWARDED_ALLOW_IPS}" \
    --timeout-graceful-shutdown 10

