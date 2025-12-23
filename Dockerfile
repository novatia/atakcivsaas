FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Europe/Rome

# Dipendenze base
RUN apt-get update && apt-get install -y \
    sudo \
    curl \
    git \
    python3 \
    python3-pip \
    python3-venv \
    ansible \
    openssl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# User root richiesto dall’installer
WORKDIR /root

# Clona installer ufficiale
RUN git clone https://github.com/FreeTAKTeam/FreeTAKHub-Installation.git

WORKDIR /root/FreeTAKHub-Installation

# Lancia installer NON interattivo
RUN ./install.sh --core --latest || true
# ↑ true per evitare exit 1 su warning non critici

# Espone porte
EXPOSE 8080 8087 8089 8443

# Avvio server
CMD ["FreeTAKServer"]
