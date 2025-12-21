#!/bin/bash
set -e

echo "Starting FreeTAKServer..."

cd /opt/FreeTAKServer

# Init DB / migrations se previste
python3 -m FreeTAKServer.controllers.services.FTS

# fallback (non dovrebbe arrivarci)
exec "$@"