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
# Il 2026-09-23 alle 23:20 UTC il caso inverso: il parser ha perso la sua
# connessione a RabbitMQ ma il processo è rimasto VIVO e fermo (on_message
# inghiotte ogni eccezione con `except BaseException`). Unit verde, processo
# presente, zero CoT per 7 ore finché non è stato riavviato a mano. Per questo
# il controllo 3 non si limita ad avvisare: se lo smistamento è fermo riavvia
# il parser (riavviarlo non scollega gli EUD, che stanno su eud_handler).
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
PARSER_RESTART_FILE=/var/lib/ots-health/cot-parser-restart
UNITS="rabbitmq-server opentakserver opentakserver-cot-parser opentakserver-eud-handler"

[ -f "$ENV_FILE" ] && . "$ENV_FILE"

# Nome del database: si legge da dove sta la verità, cioè la configurazione di
# OpenTAKServer, invece di indovinare «opentakserver» (su questo server è
# diverso, e il controllo sui CoT restava muto finché non lo si scopriva a
# mano). Si estrae SOLO l'ultimo pezzo della URI: la riga contiene anche la
# password del DB e non deve finire da nessuna parte.
OTS_CONFIG=${OTS_CONFIG:-/home/ots/ots/config.yml}
if [ -z "${OTS_DB_NAME:-}" ] && [ -r "$OTS_CONFIG" ]; then
    OTS_DB_NAME=$(sed -n 's/^[[:space:]]*SQLALCHEMY_DATABASE_URI[[:space:]]*:[[:space:]]*//p' "$OTS_CONFIG" \
        | head -n1 | tr -d '"'"'"' ' | sed 's/?.*$//; s#.*/##')
fi
OTS_DB_NAME=${OTS_DB_NAME:-opentakserver}
COT_STALE_MINUTES=${COT_STALE_MINUTES:-10}
# Intervallo minimo fra due riavvii automatici del parser per «smistamento
# fermo»: se il riavvio non basta (il guasto è altrove, es. eud_handler) non
# lo si martella ogni 5 minuti.
PARSER_RESTART_COOLDOWN_MINUTES=${PARSER_RESTART_COOLDOWN_MINUTES:-30}
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

# Fotografia del parser appeso, PRIMA di riavviarlo: il riavvio cancella
# l'unica prova di dove si era fermato. Stack Python di ogni processo (se c'è
# py-spy: `pip install py-spy` nel venv), socket TCP aperti del processo (la
# connessione a RabbitMQ :5672 c'è ancora?) e consumer della coda cot_parser.
# Stampa il percorso del file scritto.
snapshot_stalled_parser() {
    local dir=/var/log/ots-health f pid spy
    mkdir -p "$dir" 2>/dev/null || return 1
    f="$dir/cot-parser-stall-$(date -u +%Y%m%dT%H%M%SZ).txt"
    spy=$(command -v py-spy 2>/dev/null)
    [ -z "$spy" ] && [ -x /home/ots/.opentakserver_venv/bin/py-spy ] && spy=/home/ots/.opentakserver_venv/bin/py-spy
    {
        for pid in $(pgrep -f "[.]opentakserver_venv/bin/cot_parser"); do
            echo "=== PID $pid ($(ps -o etime= -p "$pid" 2>/dev/null | tr -d ' '))"
            ss -Htnp 2>/dev/null | grep "pid=$pid," || echo "(nessun socket TCP)"
            if [ -n "$spy" ]; then
                timeout 20 "$spy" dump --pid "$pid" 2>&1
            else
                echo "(py-spy non installato: niente stack)"
            fi
        done
        echo "=== consumer della coda cot_parser"
        timeout 30 rabbitmqctl -q list_consumers queue_name channel_pid ack_required 2>&1 | grep cot_parser
    } > "$f" 2>&1
    echo "$f"
}

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
        if [ "$COT_AGE" = "-1" ]; then
            # La query ha risposto: e' la TABELLA a essere vuota (MAX(timestamp)
            # NULL -> il COALESCE rende -1). Non e' un errore dello script, ed e'
            # un'informazione diversa -- puo' voler dire che il job di retention
            # ha appena ripulito tutto, oppure che non e' mai arrivato un CoT.
            COT_CHECK="tabella cot vuota"
            if [ "${EUD_CONNECTIONS:-0}" -gt 0 ]; then
                add_problem "$EUD_CONNECTIONS EUD collegati ma la tabella cot e' vuota: nessun CoT e' mai stato scritto"
            fi
        elif [ -z "$COT_AGE" ] || ! [ "$COT_AGE" -ge 0 ] 2>/dev/null; then
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
            STALE_MSG="$EUD_CONNECTIONS EUD collegati ma nessun CoT scritto da $((COT_AGE / 60)) minuti: smistamento fermo"
            LAST_RESTART=0
            [ -f "$PARSER_RESTART_FILE" ] && LAST_RESTART=$(cat "$PARSER_RESTART_FILE" 2>/dev/null)
            case "$LAST_RESTART" in ''|*[!0-9]*) LAST_RESTART=0 ;; esac
            SINCE_RESTART=$(( $(date +%s) - LAST_RESTART ))
            if [ "$DRY" -eq 1 ]; then
                add_problem "$STALE_MSG (dry-run, parser non riavviato)"
            elif ! systemctl is-enabled --quiet opentakserver-cot-parser 2>/dev/null; then
                add_problem "$STALE_MSG"
            elif [ "$SINCE_RESTART" -lt $((PARSER_RESTART_COOLDOWN_MINUTES * 60)) ]; then
                add_problem "$STALE_MSG; il riavvio del parser di $((SINCE_RESTART / 60)) minuti fa non è bastato"
            else
                mkdir -p "$(dirname "$PARSER_RESTART_FILE")" 2>/dev/null
                date +%s > "$PARSER_RESTART_FILE"
                SNAPSHOT=$(snapshot_stalled_parser)
                if systemctl restart opentakserver-cot-parser 2>/dev/null; then
                    add_problem "$STALE_MSG; cot_parser riavviato (diagnosi in ${SNAPSHOT:-?})"
                else
                    add_problem "$STALE_MSG; riavvio di cot_parser FALLITO"
                fi
            fi
        else
            COT_CHECK="ultimo CoT ${COT_AGE}s fa"
        fi
    fi
fi

# --- 4) Notifica solo sui cambi di stato --------------------------------
# Il timer gira ogni 5 minuti: senza questo, un guasto persistente
# manderebbe 288 messaggi al giorno e si smetterebbe di leggerli.
# Lo stato si confronta SENZA i numeri: «da 14 minuti» e «da 19 minuti» sono
# lo stesso guasto. Confrontando il testo intero (com'era fino al 2026-09-24)
# ogni giro sembrava un guasto nuovo e partiva un messaggio ogni 5 minuti.
mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null
PREVIOUS=""
[ -f "$STATE_FILE" ] && PREVIOUS=$(cat "$STATE_FILE" 2>/dev/null)
STATE_KEY=$(printf '%s' "$PROBLEMS" | sed 's/[0-9][0-9]*/N/g')

if [ -n "$PROBLEMS" ]; then
    if [ "$STATE_KEY" != "$PREVIOUS" ]; then
        notify "⚠️ $PROBLEMS"
    fi
    [ "$DRY" -eq 0 ] && echo "$STATE_KEY" > "$STATE_FILE"
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
