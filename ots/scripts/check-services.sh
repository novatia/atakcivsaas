#!/usr/bin/env bash
# check-services.sh — sentinella dei servizi OpenTAKServer, con riavvio
# automatico di quelli caduti e notifica Telegram.
#
# Perché esiste. Il 2026-09-22 unattended-upgrades ha riavviato RabbitMQ per
# 19 secondi. OTS non sopravvive a un broker assente: `CoTController.run()`
# finisce con `start_consuming()` senza try e senza riconnessione, quindi il
# figlio muore; il padre è fermo in `os.waitpid()`, la waitpid ritorna e il
# padre esce con **codice 0**. Per systemd è un'uscita riuscita: con
# `Restart=on-failure` il parser resta morto in silenzio. È rimasto giù un'ora
# e mezza senza un errore, e siccome `route_cot()` vive lì dentro, gli EUD
# hanno smesso di vedersi fra loro mentre tutto il resto sembrava a posto.
#
# Le unit del repo ora usano tutte Restart=always, quindi il caso si risolve
# da solo in 5 secondi. Questo script è il secondo strato: se qualcosa resta
# giù comunque (riavvio in loop, dipendenza rotta, disco pieno) qualcuno deve
# essere avvisato invece di scoprirlo in campo.
#
# Uso:   check-services.sh          controlla, ripara e notifica (per il timer)
#        check-services.sh --test   invia un messaggio di prova su Telegram
#        check-services.sh --dry    controlla e riferisce senza riavviare nulla
#
# Configurazione (NON committata): /etc/ots-notify.env con
#   TELEGRAM_TOKEN=<token del bot>
#   TELEGRAM_CHAT_ID=<chat id>
#   OTS_DB_NAME=opentakserver     (opzionale)
#   COT_STALE_MINUTES=10          (opzionale)
#
# Va eseguito come root (systemctl restart).

set -u

ENV_FILE=/etc/ots-notify.env
STATE_FILE=/var/lib/ots-health/state
UNITS="rabbitmq-server opentakserver opentakserver-cot-parser opentakserver-eud-handler"

[ -f "$ENV_FILE" ] && . "$ENV_FILE"
OTS_DB_NAME=${OTS_DB_NAME:-opentakserver}
COT_STALE_MINUTES=${COT_STALE_MINUTES:-10}
HOST=$(hostname -s 2>/dev/null || hostname)

notify() {
    local msg="[$HOST ots] $1"
    if [ -n "${TELEGRAM_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
        curl -s --max-time 20 "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
            -d "chat_id=${TELEGRAM_CHAT_ID}" \
            --data-urlencode "text=${msg}" >/dev/null || true
    fi
    logger -t ots-health "$1" 2>/dev/null || true
    echo "$msg"
}

if [ "${1:-}" = "--test" ]; then
    notify "Test notifica: la sentinella dei servizi funziona."
    exit 0
fi

DRY=0
[ "${1:-}" = "--dry" ] && DRY=1

if [ "$(id -u)" -ne 0 ] && [ "$DRY" -eq 0 ]; then
    echo "Questo script va eseguito come root (sudo)." >&2
    exit 1
fi

PROBLEMS=""
add_problem() { PROBLEMS="${PROBLEMS}${PROBLEMS:+; }$1"; }

# --- 1) Unit di sistema -------------------------------------------------
# Ordine importante: rabbitmq-server è il primo della lista, così se è lui a
# essere giù viene rimesso in piedi prima di riavviare chi ci si appoggia.
for unit in $UNITS; do
    if ! systemctl list-unit-files "${unit}.service" >/dev/null 2>&1; then
        continue
    fi
    if ! systemctl is-enabled --quiet "$unit" 2>/dev/null; then
        # Unit presente ma non abilitata: non è un guasto, è una scelta
        continue
    fi
    if systemctl is-active --quiet "$unit"; then
        continue
    fi
    if [ "$DRY" -eq 1 ]; then
        add_problem "$unit NON attivo (dry-run, non riavviato)"
        continue
    fi
    if systemctl restart "$unit" 2>/dev/null && sleep 3 && systemctl is-active --quiet "$unit"; then
        add_problem "$unit era giù, riavviato"
    else
        add_problem "$unit GIÙ e il riavvio è fallito"
    fi
done

# --- 2) Il cot_parser gira davvero? -------------------------------------
# L'unit può risultare attiva mentre il processo che fa il lavoro non c'è
# (il padre forka: se il figlio muore, muore tutto ed esce con 0).
if systemctl is-enabled --quiet opentakserver-cot-parser 2>/dev/null; then
    if ! pgrep -f "[.]opentakserver_venv/bin/cot_parser" >/dev/null 2>&1; then
        if [ "$DRY" -eq 0 ]; then
            systemctl restart opentakserver-cot-parser 2>/dev/null
            add_problem "processo cot_parser assente, unit riavviata"
        else
            add_problem "processo cot_parser assente (dry-run)"
        fi
    fi
fi

# --- 3) I CoT arrivano davvero al database? -----------------------------
# Il controllo più vicino alla realtà: EUD collegati sulla 8089 ma tabella
# `cot` ferma = lo smistamento è rotto anche se tutte le unit sono verdi.
# Senza EUD collegati la tabella è ferma per forza: niente allarme.
EUD_CONNECTIONS=$(ss -Htn state established '( sport = :8089 )' 2>/dev/null | wc -l)
COT_CHECK="saltato (nessun EUD collegato: la tabella è ferma per forza)"

if [ "${EUD_CONNECTIONS:-0}" -gt 0 ]; then
    if ! command -v psql >/dev/null 2>&1; then
        COT_CHECK="saltato (psql non disponibile: DB non Postgres?)"
    else
        # stderr catturato, non buttato: senza il messaggio di psql non si
        # distingue «database sbagliato» da «permessi» da «tabella assente»,
        # e si finisce a indovinare.
        COT_OUT=$(sudo -u postgres psql -tAc \
            "SELECT COALESCE(EXTRACT(EPOCH FROM (NOW() AT TIME ZONE 'UTC' - MAX(timestamp)))::bigint, -1) FROM cot;" \
            "$OTS_DB_NAME" 2>&1)
        COT_AGE=$(printf '%s' "$COT_OUT" | tr -d '[:space:]')
        if [ -z "$COT_AGE" ] || ! [ "$COT_AGE" -ge 0 ] 2>/dev/null; then
            # Un controllo che non riesce a girare va DETTO, non taciuto: è il
            # controllo più importante dei tre e senza di esso la sentinella
            # dichiara «tutto a posto» avendo guardato solo le unit.
            # Prudenza: se l'errore contenesse una URI con credenziali, via.
            REASON=$(printf '%s' "$COT_OUT" | head -n1 \
                | sed -E 's#://[^:/@]+:[^@]*@#://***:***@#g' | cut -c1-140)
            COT_CHECK="NON ESEGUITO sul DB «$OTS_DB_NAME» — ${REASON:-nessun output da psql} (regolabile con OTS_DB_NAME in $ENV_FILE)"
            add_problem "$COT_CHECK"
        elif [ "$COT_AGE" -gt $((COT_STALE_MINUTES * 60)) ]; then
            COT_CHECK="ultimo CoT $((COT_AGE / 60)) minuti fa"
            add_problem "$EUD_CONNECTIONS EUD collegati ma nessun CoT scritto da $((COT_AGE / 60)) minuti: smistamento fermo"
        else
            COT_CHECK="ultimo CoT ${COT_AGE}s fa"
        fi
    fi
fi

# --- 4) Notifica solo sui cambi di stato --------------------------------
# Il timer gira ogni 5 minuti: senza questo, un guasto persistente
# manderebbe 288 messaggi al giorno e si smetterebbe di leggerli.
mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null
PREVIOUS=""
[ -f "$STATE_FILE" ] && PREVIOUS=$(cat "$STATE_FILE" 2>/dev/null)

if [ -n "$PROBLEMS" ]; then
    if [ "$PROBLEMS" != "$PREVIOUS" ]; then
        notify "⚠️ $PROBLEMS"
    fi
    [ "$DRY" -eq 0 ] && echo "$PROBLEMS" > "$STATE_FILE"
    exit 1
fi

if [ -n "$PREVIOUS" ]; then
    notify "✅ Tutti i servizi OTS sono tornati a posto."
fi
[ "$DRY" -eq 0 ] && : > "$STATE_FILE"
# Si dichiara SEMPRE cosa è stato controllato davvero: «tutto a posto» senza
# dire quali controlli hanno girato è la stessa bugia per omissione che si
# vuole evitare.
echo "[$HOST ots] tutto a posto · unit: $(echo $UNITS | wc -w) verificate · EUD collegati: $EUD_CONNECTIONS · CoT: $COT_CHECK"
