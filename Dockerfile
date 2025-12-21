FROM ubuntu:24.04

# ----------------------------
# Environment
# ----------------------------
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV VENV_PATH=/opt/venv
ENV FTS_ENV=production
ENV FTS_CONFIG=/opt/FreeTAKServer/FreeTAKServer.ini
ENV PATH="/opt/venv/bin:$PATH"

# ----------------------------
# System dependencies
# ----------------------------
RUN apt update && apt install -y \
    python3 \
    python3-venv \
    python3-pip \
    git \
    netcat-openbsd \
    iproute2 \
    openssl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ----------------------------
# Create virtualenv (PEP 668 safe)
# ----------------------------
RUN python3 -m venv ${VENV_PATH} \
 && ${VENV_PATH}/bin/pip install --upgrade pip
 
# ----------------------------
# FreeTAKServer source
# ----------------------------
WORKDIR /opt

# Pin version for reproducible builds
ARG FTS_VERSION=master
RUN git clone --branch ${FTS_VERSION} --depth 1 \
    https://github.com/FreeTAKTeam/FreeTAKServer.git

# ----------------------------
# Python dependencies
# ----------------------------
WORKDIR /opt/FreeTAKServer

# Install FreeTAKServer runtime dependencies
# (avoids installing test-only requirements)
RUN pip3 install --no-cache-dir .

# ----------------------------
# Configuration
# ----------------------------
COPY config/FreeTAKServer.ini /opt/FreeTAKServer/FreeTAKServer.ini

# ----------------------------
# Entrypoint
# ----------------------------
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# ----------------------------
# Ports
# ----------------------------
# 8087  -> CoT
# 8089  -> REST / API
# 19023 -> Federation / misc
EXPOSE 8087 8089 19023

# ----------------------------
# Runtime user (optional but recommended)
# ----------------------------
RUN useradd -m freetak
USER freetak

# ----------------------------
# Start
# ----------------------------
ENTRYPOINT ["/entrypoint.sh"]
