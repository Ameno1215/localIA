#!/usr/bin/env bash
# Lance le panneau de contrôle web : http://localhost:7000  (Ctrl+C pour arrêter)
cd "$(dirname "$0")/.."
exec python3 panneau/server.py
