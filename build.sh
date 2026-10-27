#!/usr/bin/env bash
# Render build command helper
set -e
pip install -r requirements.txt
playwright install chromium
playwright install-deps chromium || true
