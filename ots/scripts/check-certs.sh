#!/usr/bin/env bash
# check-certs.sh — rinnovo automatico Let's Encrypt + sorveglianza scadenze certificati
# con notifica Telegram in caso di problemi.
#
# Uso:   check-certs.sh          esegue rinnovo e controlli (pensato per il timer systemd)
#        check-certs.sh --test   invia solo un messaggio di prova su Telegram
#
# Configurazione (NON committata nel repo): /etc/ots-notify.env con
#   TELEGRAM_TOKEN=<token del bot>
#   TELEGRAM_CHAT_ID=<chat id>
#   WARN_DAYS=14   (opzionale, soglia giorni per l'avviso)
#
# Va eseguito come root (certbot + reload nginx).

set -u

ENV_FILE=/etc/ots-notify.env
LE_CERT=/etc/letsencrypt/live/tacticalscout.3utilities.com/fullchain.pem
OTS_CERT=/home/ots/ots/ca/certs/opentakserver/opentakserver.pem

[ -f "$ENV_FILE" ] && . "$ENV_FILE"
WARN_DAYS=${WARN_DAYS:-14}
HOST=$(hostname -s)

notify() {
    local msg="[$HOST certs] $1"
    if [ -n "${TELEGRAM_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
        curl -s --max-time 20 "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
            -d "chat_id=${TELEGRAM_CHAT_ID}" \
            --data-urlencode "text=${msg}" >/dev/null || true
    fi
    logger -t ots-cert-check "$1" 2>/dev/null || true
    echo "$msg"
}

if [ "${1:-}" = "--test" ]; then
    notify "Test notifica: il monitoraggio certificati funziona."
    exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "Questo script va eseguito come root (sudo)." >&2
    exit 1
fi

# --- 1) Rinnovo Let's Encrypt (certbot rinnova solo se mancano <30 giorni) ---
if command -v certbot >/dev/null 2>&1; then
    RENEW_OUT=$(certbot renew --non-interactive --deploy-hook "systemctl reload nginx" 2>&1)
    RENEW_RC=$?
    if [ "$RENEW_RC" -ne 0 ]; then
        notify "ERRORE rinnovo certbot (exit $RENEW_RC): $(echo "$RENEW_OUT" | tail -n 5)"
    elif echo "$RENEW_OUT" | grep -q "Congratulations"; then
        notify "Certificato Let's Encrypt rinnovato con successo, nginx ricaricato."
    fi
else
    notify "ATTENZIONE: certbot non installato, nessun rinnovo possibile."
fi

# --- 2) Controllo scadenze ---
check_cert() {
    local name="$1" file="$2"
    if [ ! -f "$file" ]; then
        notify "ATTENZIONE: certificato $name non trovato ($file)"
        return
    fi
    local end epoch_end epoch_now days
    end=$(openssl x509 -in "$file" -noout -enddate | cut -d= -f2)
    epoch_end=$(date -d "$end" +%s)
    epoch_now=$(date +%s)
    days=$(( (epoch_end - epoch_now) / 86400 ))
    if [ "$days" -lt 0 ]; then
        notify "CRITICO: certificato $name SCADUTO da $((-days)) giorni!"
    elif [ "$days" -lt "$WARN_DAYS" ]; then
        notify "ATTENZIONE: certificato $name scade tra $days giorni e non risulta rinnovato."
    fi
}

check_cert "Let's Encrypt (web 443)" "$LE_CERT"
check_cert "OpenTAKServer (8443/8089)" "$OTS_CERT"

exit 0
