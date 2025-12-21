#!/bin/bash
set -e

echo "Starting FreeTAKServer..."

# Attiva virtualenv
source /opt/venv/bin/activate

cd /opt/FreeTAKServer

# Avvio ufficiale FreeTAKServer
exec python -m FreeTAKServer
