#!/bin/bash
set -e

sed -i 's/\r$//' /entrypoint.sh

echo "Starting FreeTAKServer..."

# Attiva virtualenv
source /opt/venv/bin/activate

cd /opt/FreeTAKServer

# Avvio ufficiale FreeTAKServer
exec /opt/FreeTAKServer/docker-run.sh