#!/bin/bash
# Amusement — launch the API server.
# Usage: ./run.sh   (runs on 127.0.0.1:8000; expose publicly with the tunnel below)
cd "$(dirname "$0")"
exec ./venv/bin/python arena_api.py
