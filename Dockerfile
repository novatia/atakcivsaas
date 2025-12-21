FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Sistema
RUN apt update && apt install -y \
    python3 \
    python3-pip \
    git \
    netcat \
    iproute2 \
    openssl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Codice FreeTAKServer
WORKDIR /opt
RUN git clone https://github.com/FreeTAKTeam/FreeTAKServer.git

WORKDIR /opt/FreeTAKServer
RUN pip3 install --upgrade pip
RUN pip3 install -r requirements.txt

# Config
COPY config/FreeTAKServer.ini /opt/FreeTAKServer/

# Entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8087 8089 19023

ENTRYPOINT ["/entrypoint.sh"]
