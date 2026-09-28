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
# il controllo 4 non si limita ad avvisare: se lo smistamento è fermo riavvia
# il parser (riavviarlo non scollega gli EUD, che stanno su eud_handler).
#
# Il 2026-09-28 l'errore opposto: 12 «EUD collegati» che erano connessioni
# morte da tre giorni. Il controllo 3 ora le chiude (o, col fork n3 che le
# chiude da solo, si limita a contarle) e il 4 conta solo quelle che ricevono
# dati. Dal fork n3 anche il parser non esce più con 0 (issue #4): il suo padre
# rilancia i figli, e i riavvii qui restano per il caso «vivo ma appeso».
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
#   EUD_IDLE_MINUTES=15           (opzionale: oltre, una connessione 8089 è morta)
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
# Una connessione sulla 8089 che non riceve un byte da più di così è morta:
# ATAK manda la propria posizione ogni pochi secondi e un ping ogni minuto.
EUD_IDLE_MINUTES=${EUD_IDLE_MINUTES:-15}
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
PROBLEM_KEYS=""
# $1 = testo per il messaggio; $2 (opzionale) = chiave che identifica il
# guasto per il confronto di stato. Serve quando lo stesso guasto si descrive
# in modi diversi da un giro all'altro («parser riavviato» / «il riavvio non è
# bastato»): senza chiave ogni cambio di frase sembrava un guasto nuovo e
# partivano 4 messaggi l'ora per tre giorni (2026-09-25 → 28).
add_problem() {
    PROBLEMS="${PROBLEMS}${PROBLEMS:+; }$1"
    PROBLEM_KEYS="${PROBLEM_KEYS}${PROBLEM_KEYS:+; }${2:-$1}"
}

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

# --- 3) Connessioni morte sulla 8089 ------------------------------------
# Un telefono che perde la rete o chiude ATAK senza chiudere il socket lascia
# la connessione ESTABLISHED per sempre: eud_handler non ha un timeout di
# inattività, non scrive nulla verso chi tace, e senza keepalive il kernel non
# se ne accorge. Il 2026-09-28 c'erano 12 connessioni «collegate» che non
# ricevevano un byte da 75-84 ore: la sentinella le contava come EUD online,
# gridava «smistamento fermo» e riavviava il parser ogni 30 minuti per niente.
#
# Stampa «peer lastrcv_ms» per ogni connessione (lastrcv -1 = non leggibile,
# trattata come viva per prudenza).
list_8089() {
    ss -Htni state established '( sport = :8089 )' 2>/dev/null | awk '
        { for (i = 1; i <= NF; i++) {
            if ($i ~ /:8089$/ && i < NF) { if (peer != "") print peer, -1; peer = $(i + 1); i++ }
            else if ($i ~ /^lastrcv:/ && peer != "") { split($i, a, ":"); print peer, a[2]; peer = "" }
        } }
        END { if (peer != "") print peer, -1 }'
}
count_8089() {  # stampa «vive morte»
    list_8089 | awk -v max=$((EUD_IDLE_MINUTES * 60000)) '
        { if ($2 >= 0 && $2 > max) dead++; else live++ }
        END { print live + 0, dead + 0 }'
}

# Dal fork novatia/OpenTAKServer branch n3 (issue #3) e' eud_handler stesso a
# chiudere chi tace da OTS_EUD_IDLE_TIMEOUT secondi, con keepalive TCP: la
# chiave in defaultconfig.py dice se il fix e' installato. In quel caso qui si
# conta soltanto, e una connessione morta rimasta e' il segno che il fix non
# sta funzionando (va nel journal, non la si chiude a mano).
OTS_VENV=${OTS_VENV:-/home/ots/.opentakserver_venv}
SERVER_CLOSES_IDLE=0
for f in "$OTS_VENV"/lib/python3*/site-packages/opentakserver/defaultconfig.py; do
    grep -q OTS_EUD_IDLE_TIMEOUT "$f" 2>/dev/null && SERVER_CLOSES_IDLE=1
done

read -r EUD_LIVE EUD_DEAD <<EOF
$(count_8089)
EOF
DEAD_CLEANUP=""
if [ "$EUD_DEAD" -gt 0 ]; then
    if [ "$SERVER_CLOSES_IDLE" -eq 1 ]; then
        DEAD_CLEANUP="$EUD_DEAD connessioni sulla 8089 silenti da oltre $EUD_IDLE_MINUTES minuti nonostante il timeout di eud_handler (OTS_EUD_IDLE_TIMEOUT): controllare config.yml e il log di eud_handler"
    elif [ "$DRY" -eq 1 ]; then
        DEAD_CLEANUP="$EUD_DEAD connessioni morte (dry-run, non chiuse)"
    else
        # Una per una con ss -K: le connessioni vive restano dove sono
        list_8089 | while read -r peer rcv; do
            [ "$rcv" -ge 0 ] 2>/dev/null && [ "$rcv" -gt $((EUD_IDLE_MINUTES * 60000)) ] || continue
            addr=${peer%:*}; port=${peer##*:}
            ss -K state established "( sport = :8089 and dst $addr and dport = :$port )" >/dev/null 2>&1
        done
        read -r EUD_LIVE STILL_DEAD <<EOF
$(count_8089)
EOF
        if [ "$STILL_DEAD" -gt 0 ] && [ "$EUD_LIVE" -eq 0 ]; then
            # ss -K non ha effetto se il kernel non ha CONFIG_INET_DIAG_DESTROY.
            # Con nessuna connessione viva riavviare eud_handler non scollega
            # nessuno, quindi è la via di riserva sicura.
            systemctl restart opentakserver-eud-handler 2>/dev/null
            DEAD_CLEANUP="$EUD_DEAD connessioni morte sulla 8089 (ss -K senza effetto): eud_handler riavviato, nessun EUD attivo"
        elif [ "$STILL_DEAD" -gt 0 ]; then
            DEAD_CLEANUP="$STILL_DEAD connessioni morte sulla 8089 non chiudibili con ss -K; lasciate aperte per non scollegare i $EUD_LIVE EUD attivi"
        else
            DEAD_CLEANUP="chiuse $EUD_DEAD connessioni morte sulla 8089 (silenti da oltre $EUD_IDLE_MINUTES minuti)"
        fi
    fi
    # Non è un guasto: va nel journal e nel riepilogo, non su Telegram.
    logger -t ots-health "$DEAD_CLEANUP" 2>/dev/null || true
fi

# --- 4) I CoT arrivano davvero al database? -----------------------------
# Il controllo più vicino alla realtà: EUD ATTIVI sulla 8089 (connessioni che
# hanno ricevuto dati negli ultimi EUD_IDLE_MINUTES) ma tabella `cot` ferma =
# lo smistamento è rotto anche se tutte le unit sono verdi. Senza EUD attivi
# la tabella è ferma per forza: niente allarme.
EUD_CONNECTIONS=$EUD_LIVE
COT_CHECK="saltato (nessun EUD attivo: la tabella è ferma per forza)"

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
                add_problem "$EUD_CONNECTIONS EUD attivi ma la tabella cot e' vuota: nessun CoT e' mai stato scritto"
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
            STALE_MSG="$EUD_CONNECTIONS EUD attivi (ricevono dati) ma nessun CoT scritto da $((COT_AGE / 60)) minuti: smistamento fermo"
            LAST_RESTART=0
            [ -f "$PARSER_RESTART_FILE" ] && LAST_RESTART=$(cat "$PARSER_RESTART_FILE" 2>/dev/null)
            case "$LAST_RESTART" in ''|*[!0-9]*) LAST_RESTART=0 ;; esac
            SINCE_RESTART=$(( $(date +%s) - LAST_RESTART ))
            if [ "$DRY" -eq 1 ]; then
                add_problem "$STALE_MSG (dry-run, parser non riavviato)" "cot-stale"
            elif ! systemctl is-enabled --quiet opentakserver-cot-parser 2>/dev/null; then
                add_problem "$STALE_MSG" "cot-stale"
            elif [ "$SINCE_RESTART" -lt $((PARSER_RESTART_COOLDOWN_MINUTES * 60)) ]; then
                add_problem "$STALE_MSG; il riavvio del parser di $((SINCE_RESTART / 60)) minuti fa non è bastato" "cot-stale"
            else
                mkdir -p "$(dirname "$PARSER_RESTART_FILE")" 2>/dev/null
                date +%s > "$PARSER_RESTART_FILE"
                SNAPSHOT=$(snapshot_stalled_parser)
                if systemctl restart opentakserver-cot-parser 2>/dev/null; then
                    add_problem "$STALE_MSG; cot_parser riavviato, riprovo ogni $PARSER_RESTART_COOLDOWN_MINUTES minuti senza altri avvisi (diagnosi in ${SNAPSHOT:-?})" "cot-stale"
                else
                    add_problem "$STALE_MSG; riavvio di cot_parser FALLITO" "cot-stale-restart-failed"
                fi
            fi
        else
            COT_CHECK="ultimo CoT ${COT_AGE}s fa"
        fi
    fi
fi

# --- 5) Notifica solo sui cambi di stato --------------------------------
# Il timer gira ogni 5 minuti: senza questo, un guasto persistente
# manderebbe 288 messaggi al giorno e si smetterebbe di leggerli.
# Lo stato si confronta SENZA i numeri: «da 14 minuti» e «da 19 minuti» sono
# lo stesso guasto. Confrontando il testo intero (com'era fino al 2026-09-24)
# ogni giro sembrava un guasto nuovo e partiva un messaggio ogni 5 minuti.
# Si confrontano le CHIAVI (v. add_problem), non le frasi.
mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null
PREVIOUS=""
[ -f "$STATE_FILE" ] && PREVIOUS=$(cat "$STATE_FILE" 2>/dev/null)
STATE_KEY=$(printf '%s' "$PROBLEM_KEYS" | sed 's/[0-9][0-9]*/N/g')

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
echo "[$HOST ots] tutto a posto · unit: $(echo $UNITS | wc -w) verificate · EUD attivi sulla 8089: $EUD_LIVE${DEAD_CLEANUP:+ ($DEAD_CLEANUP)} · CoT: $COT_CHECK"
