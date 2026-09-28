#!/usr/bin/env bash
#
# update-ots.sh — Aggiorna OpenTAKServer (backend pip + opzionalmente la web UI)
#
# Uso (da root sul server):
#   ./update-ots.sh              # ferma opentakserver+cot-parser+eud-handler, backup, upgrade, riavvia, verifica
#   ./update-ots.sh --check      # mostra solo versione/commit installato vs ultimo disponibile
#   ./update-ots.sh --ui         # aggiorna anche la web UI servita da nginx
#   ./update-ots.sh --no-backup  # salta il backup dei dati e la rotazione: i
#                                # backup esistenti restano tutti intatti
# I flag si combinano (es. --no-backup --ui).
#
# Durante l'aggiornamento la sentinella (ots-health-check.timer) viene fermata
# e riattivata alla fine, anche se lo script fallisce: altrimenti potrebbe
# riavviare un servizio mentre pip sta sostituendo il pacchetto, o mandare su
# Telegram falsi allarmi per le unit ferme di proposito.
#
# Le tre unit girano sullo stesso venv (vedi ots/systemd/): vengono fermate
# tutte PRIMA di backup+upgrade, non solo riavviate dopo — altrimenti
# rischiano di leggere pacchetti a metà sostituiti mentre sono ancora attive.
#
# Percorsi/nomi sovrascrivibili via variabili d'ambiente, es:
#   OTS_USER=ots OTS_SERVICE=opentakserver ./update-ots.sh
#
# Sorgente del backend: di default installiamo dal NOSTRO fork
# (github.com/novatia/OpenTAKServer, branch n3-1.7.13), non da PyPI — PyPI è il
# pacchetto upstream vanilla. n3-1.7.13 = release upstream 1.7.13 + i nostri fix,
# un commit per issue del fork (github.com/novatia/OpenTAKServer/issues).
# NON il branch n3: quello sta sopra il master upstream non rilasciato, la cui
# migrazione 640de7aafac2 richiede PostGIS (type "geography" does not exist,
# server giù il 2026-09-29). Si passa a una base più nuova solo dopo una release
# upstream e con PostGIS installato. Fix inclusi:
#   - create_channel() Meshtastic con campi LoRa opzionali
#   - #1 binding RabbitMQ di tutti gli EUD sciolti alla disconnessione di uno
#   - #2 close_connection() che si interrompeva lasciando socket/AMQP aperti
#   - #3 timeout di inattività (OTS_EUD_IDLE_TIMEOUT, default 900 s) + keepalive
#   - #4 cot_parser supervisore: rilancia i figli invece di uscire con 0
#   - #5 creator_uid dei data package sempre NULL
#   - #6 delete_old_data con retention 0 = cancella tutto → ora non cancella
# Per tornare a PyPI upstream (perdendo i fix): OTS_GIT_SOURCE="" ./update-ots.sh
OTS_GIT_SOURCE="${OTS_GIT_SOURCE:-git+https://github.com/novatia/OpenTAKServer.git@n3-1.7.13}"

set -euo pipefail

# ------------------------- Configurazione -------------------------
OTS_USER="${OTS_USER:-ots}"
OTS_VENV="${OTS_VENV:-/home/${OTS_USER}/.opentakserver_venv}"
OTS_DATA="${OTS_DATA:-/home/${OTS_USER}/ots}"
OTS_SERVICE="${OTS_SERVICE:-opentakserver}"
# Unit dedicate introdotte perché il processo principale non le spawna da solo
# (vedi ots/systemd/): girano nello stesso venv, quindi vanno fermate PRIMA di
# aggiornare i pacchetti (non solo riavviate dopo) e rialzate con lui.
OTS_EXTRA_SERVICES="${OTS_EXTRA_SERVICES:-opentakserver-cot-parser opentakserver-eud-handler}"
BACKUP_DIR="${BACKUP_DIR:-/root/ots-backups}"
KEEP_BACKUPS="${KEEP_BACKUPS:-5}"
UI_REPO="${UI_REPO:-brian7704/OpenTAKServer-UI}"

WATCHDOG_TIMER="${WATCHDOG_TIMER:-ots-health-check.timer}"
WATCHDOG_SERVICE="${WATCHDOG_TIMER%.timer}.service"

PIP="${OTS_VENV}/bin/pip"
PY="${OTS_VENV}/bin/python3"

# ------------------------- Argomenti -------------------------
MODE_CHECK=0
MODE_UI=0
NO_BACKUP=0
for arg in "$@"; do
    case "${arg}" in
        --check)     MODE_CHECK=1 ;;
        --ui)        MODE_UI=1 ;;
        --no-backup) NO_BACKUP=1 ;;
        *) echo "Argomento sconosciuto: ${arg} (validi: --check --ui --no-backup)" >&2; exit 2 ;;
    esac
done

# ------------------------- Utility -------------------------
log()  { echo -e "\e[1;32m[OTS-UPDATE]\e[0m $*"; }
warn() { echo -e "\e[1;33m[OTS-UPDATE]\e[0m $*" >&2; }
die()  { echo -e "\e[1;31m[OTS-UPDATE]\e[0m $*" >&2; exit 1; }

installed_version() {
    # "|| true": se il pacchetto non è installato pip show fallisce e con
    # set -e/pipefail lo script uscirebbe in silenzio prima del messaggio di errore
    "${PIP}" show opentakserver 2>/dev/null | awk '/^Version:/{print $2}' || true
}

# Il venv vede anche /usr/lib/python3/dist-packages (include-system-site-packages),
# dove apt (python3-zope.interface 6.1, serve a certbot: non si toglie) mette
# zope.interface-6.1-nspkg.pth. Quel .pth gira a ogni avvio di Python e registra
# in sys.modules lo zope DI SISTEMA; zope.event del venv diventa introvabile e
# OTS muore con «No module named 'zope.event'» (2026-09-29, dopo che pip ha
# portato zope.interface 8.6 / zope.event 6.2, senza più __init__.py).
# Il .pth usa sys.modules.setdefault: se zope c'è già, ci aggiunge solo la sua
# cartella. I .pth del venv girano prima di quelli di sistema, quindi:
#   - zope/__init__.py con extend_path nel venv (pacchetto regolare del venv),
#   - 000-zope-venv-first.pth nel venv che lo importa per primo.
# pip non tocca nessuno dei due: non sono nel RECORD di alcun pacchetto.
fix_zope_namespace() {
    local sp
    sp="$("${PY}" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
    [[ -d "${sp}/zope" ]] || return 0
    if [[ ! -f "${sp}/zope/__init__.py" ]]; then
        warn "zope nel venv senza __init__.py: aggiungo extend_path (conflitto con lo zope di sistema)."
        cat <<'EOF' | sudo -u "${OTS_USER}" tee "${sp}/zope/__init__.py" >/dev/null
# Aggiunto da update-ots.sh: vedi fix_zope_namespace() nello script.
__path__ = __import__("pkgutil").extend_path(__path__, __name__)
EOF
    fi
    if [[ ! -f "${sp}/000-zope-venv-first.pth" ]]; then
        warn "Aggiungo 000-zope-venv-first.pth: lo zope del venv va importato prima del .pth di sistema."
        echo "import zope" | sudo -u "${OTS_USER}" tee "${sp}/000-zope-venv-first.pth" >/dev/null
    fi
}

check_imports() {
    fix_zope_namespace
    local out
    if ! out="$(sudo -u "${OTS_USER}" "${PY}" -c 'import opentakserver.app' 2>&1)"; then
        echo "${out}" | tail -n 5 >&2
        die "Import di opentakserver fallito dopo l'aggiornamento (vedi sopra): unit lasciate ripartire dalla trap, ma il server non funzionerà finché l'errore non è risolto."
    fi
    log "Import di opentakserver: OK."
}

# Sentinella: la si ferma prima di toccare i servizi e la si riattiva all'uscita
# (trap EXIT, quindi anche dopo un die) solo se era attiva prima. Se un giro è
# in corso (oneshot) si aspetta che finisca invece di interromperlo a metà di
# un riavvio.
WATCHDOG_WAS_ACTIVE=0
# 1 fra lo stop delle unit e il loro riavvio: se lo script muore lì in mezzo
# (pip install fallito, rete giù, set -e) le si rialza, invece di lasciare il
# server spento.
SERVICES_STOPPED=0
TMP=""
cleanup() {
    [[ -n "${TMP}" ]] && rm -rf "${TMP}"
    if (( SERVICES_STOPPED )); then
        warn "Uscita a metà aggiornamento: riavvio ${ALL_SERVICES} con quello che è installato ora."
        for svc in ${ALL_SERVICES}; do systemctl start "${svc}" || warn "${svc} non riparte"; done
    fi
    if (( WATCHDOG_WAS_ACTIVE )); then
        if systemctl start "${WATCHDOG_TIMER}"; then
            log "Sentinella ${WATCHDOG_TIMER} riattivata."
        else
            warn "Riattivazione di ${WATCHDOG_TIMER} FALLITA: systemctl start ${WATCHDOG_TIMER}"
        fi
    fi
}
trap cleanup EXIT

pause_watchdog() {
    systemctl is-active --quiet "${WATCHDOG_TIMER}" 2>/dev/null || return 0
    WATCHDOG_WAS_ACTIVE=1
    systemctl stop "${WATCHDOG_TIMER}"
    local waited=0
    while systemctl is-active --quiet "${WATCHDOG_SERVICE}" 2>/dev/null && (( waited < 120 )); do
        (( waited == 0 )) && log "Attendo la fine del giro in corso della sentinella ..."
        sleep 2; waited=$((waited + 2))
    done
    log "Sentinella ${WATCHDOG_TIMER} in pausa fino alla fine dell'aggiornamento."
}

# Ultimo commit (short hash) del branch del fork da cui installiamo — non un
# numero di versione: un fork installato via git non ne ha uno affidabile da
# confrontare (pyproject.toml può restare invariato tra un commit e l'altro).
latest_git_commit() {
    local url="${OTS_GIT_SOURCE#git+}"
    url="${url%@*}"
    local branch="${OTS_GIT_SOURCE##*@}"
    git ls-remote "${url}" "refs/heads/${branch}" 2>/dev/null | cut -c1-7 || echo "?"
}

# Base del branch del fork: deve essere un tag di RELEASE upstream (X.Y.Z) con
# sopra solo i nostri commit. Il 2026-09-29 il branch installato stava sul
# master upstream non rilasciato (versione 0.0.0.postNNNN, nessun tag): una
# migrazione che richiede PostGIS ha fermato il server. Il controllo gira PRIMA
# di fermare qualsiasi servizio. Stampa «tag commit_sopra»; vuoto = nessun tag.
MAX_FORK_COMMITS="${MAX_FORK_COMMITS:-30}"
fork_base() {
    local url="${OTS_GIT_SOURCE#git+}"
    url="${url%@*}"
    local branch="${OTS_GIT_SOURCE##*@}"
    local dir desc=""
    dir="$(mktemp -d)"
    # Solo storia e tag, niente file: bastano per git describe
    if git clone --quiet --filter=blob:none --no-checkout --branch "${branch}" "${url}" "${dir}" 2>/dev/null; then
        desc="$(git -C "${dir}" describe --tags --long --match '[0-9]*.[0-9]*.[0-9]*' 2>/dev/null || true)"
    fi
    rm -rf "${dir}"
    # 1.7.13-7-gb6d2143 -> «1.7.13 7»
    if [[ "${desc}" =~ ^([0-9]+\.[0-9]+\.[0-9]+)-([0-9]+)-g[0-9a-f]+$ ]]; then
        echo "${BASH_REMATCH[1]} ${BASH_REMATCH[2]}"
    fi
}

check_fork_base() {
    local base tag ahead
    base="$(fork_base || true)"
    if [[ -z "${base}" ]]; then
        die "Il branch ${OTS_GIT_SOURCE##*@} non parte da un tag di release (X.Y.Z) o il tag non è sul fork: pip lo installerebbe come 0.0.0. Crea il branch da un tag upstream e pubblica il tag sul fork (git push origin refs/tags/<tag>). Per forzare: ALLOW_UNRELEASED_BASE=1 $0"
    fi
    read -r tag ahead <<< "${base}"
    if (( ahead > MAX_FORK_COMMITS )); then
        die "Il branch ${OTS_GIT_SOURCE##*@} ha ${ahead} commit sopra la release ${tag} (massimo ${MAX_FORK_COMMITS}): probabilmente contiene il master upstream non rilasciato, con migrazioni DB non verificate. Crea il branch dal tag ${tag} e riporta solo i nostri commit (cherry-pick). Per forzare: ALLOW_UNRELEASED_BASE=1 $0"
    fi
    log "Base del fork: release ${tag} + ${ahead} commit nostri."
}

latest_version() {
    curl -fsSL https://pypi.org/pypi/OpenTAKServer/json 2>/dev/null \
        | "${PY}" -c 'import sys,json; print(json.load(sys.stdin)["info"]["version"])' 2>/dev/null \
        || echo "?"
}

# ------------------------- Preflight -------------------------
[[ $EUID -eq 0 ]] || die "Esegui come root (serve per systemctl e backup)."
[[ -x "${PIP}" ]] || die "Venv non trovato: ${OTS_VENV}"
[[ -d "${OTS_DATA}" ]] || die "Directory dati non trovata: ${OTS_DATA}"
systemctl cat "${OTS_SERVICE}" >/dev/null 2>&1 || die "Servizio systemd '${OTS_SERVICE}' non trovato."
for svc in ${OTS_EXTRA_SERVICES}; do
    systemctl cat "${svc}" >/dev/null 2>&1 || die "Servizio systemd '${svc}' non trovato (OTS_EXTRA_SERVICES)."
done

ALL_SERVICES="${OTS_SERVICE} ${OTS_EXTRA_SERVICES}"

CURRENT="$(installed_version)"
[[ -n "${CURRENT}" ]] || die "opentakserver non risulta installato in ${OTS_VENV}"

if [[ -n "${OTS_GIT_SOURCE}" ]]; then
    # Da fork: niente numero di versione da PyPI da confrontare, guardiamo
    # invece l'hash del commit in cima al branch. A differenza di PyPI non
    # possiamo sapere in anticipo se coincide con quello già installato (pip
    # non registra da quale commit git proviene un pacchetto), quindi qui
    # LATEST è solo informativo: l'aggiornamento viene sempre eseguito.
    LATEST="$(latest_git_commit)"
    log "Versione installata: ${CURRENT}   Sorgente: ${OTS_GIT_SOURCE}   Ultimo commit: ${LATEST}"
    if [[ "${ALLOW_UNRELEASED_BASE:-0}" == "1" ]]; then
        warn "ALLOW_UNRELEASED_BASE=1: salto il controllo della base del fork."
    else
        check_fork_base
    fi

    if (( MODE_CHECK )); then
        log "Installazione da fork: esegui senza --check per reinstallare sempre l'ultimo commit del branch."
        exit 0
    fi
else
    LATEST="$(latest_version)"
    log "Versione installata: ${CURRENT}   Ultima su PyPI: ${LATEST}"

    if (( MODE_CHECK )); then
        if [[ "${CURRENT}" == "${LATEST}" ]]; then
            log "Sei già all'ultima versione."
        else
            log "Aggiornamento disponibile: ${CURRENT} -> ${LATEST}. Esegui senza --check per applicarlo."
        fi
        exit 0
    fi

    if [[ "${CURRENT}" == "${LATEST}" ]]; then
        log "Già all'ultima versione (${CURRENT}). Nessun aggiornamento backend necessario."
        (( MODE_UI )) || exit 0
    fi
fi

# ------------------------- Backup -------------------------
mkdir -p "${BACKUP_DIR}"
STAMP="$(date +%F_%H%M%S)"
BACKUP_FILE="${BACKUP_DIR}/ots-data-${CURRENT}-${STAMP}.tar.gz"

# ------------------------- Upgrade backend -------------------------
if [[ "${CURRENT}" != "${LATEST}" || ${MODE_UI} -eq 1 ]]; then
    pause_watchdog
fi

if [[ "${CURRENT}" != "${LATEST}" ]]; then
    # Tutte e tre le unit girano sullo stesso venv: aggiornare i pacchetti
    # mentre sono ancora in esecuzione può farle leggere file a metà scritti
    # o lasciarle con moduli vecchi in memoria mentre gli altri processi hanno
    # già i nuovi — le fermiamo PRIMA del backup/upgrade, non solo dopo.
    log "Fermo ${ALL_SERVICES} prima dell'aggiornamento ..."
    SERVICES_STOPPED=1
    for svc in ${ALL_SERVICES}; do
        systemctl stop "${svc}"
    done

    if (( NO_BACKUP )); then
        warn "--no-backup: nessun backup dei dati, i backup esistenti in ${BACKUP_DIR} restano tutti."
        BACKUP_FILE=""
    else
        log "Backup di ${OTS_DATA} in ${BACKUP_FILE} ..."
        # Con i servizi fermi non c'è più scrittura concorrente sui log: l'unico
        # motivo per cui tar potrebbe ancora vedere un file cambiare a metà lettura
        # è un processo esterno (logrotate, ecc.), quindi teniamo comunque la
        # tolleranza sull'exit code 1 ("file changed as we read it").
        set +e
        tar czf "${BACKUP_FILE}" --warning=no-file-changed \
            -C "$(dirname "${OTS_DATA}")" "$(basename "${OTS_DATA}")"
        TAR_RC=$?
        set -e
        if (( TAR_RC > 1 )); then
            for svc in ${ALL_SERVICES}; do systemctl start "${svc}" || true; done
            SERVICES_STOPPED=0
            die "Backup fallito (tar exit ${TAR_RC}). Servizi rimessi su, nessun aggiornamento applicato."
        fi
        log "Backup completato ($(du -h "${BACKUP_FILE}" | cut -f1))."

        # Rotazione: tieni solo gli ultimi KEEP_BACKUPS
        ls -1t "${BACKUP_DIR}"/ots-data-*.tar.gz 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)) | while read -r old; do
            warn "Rimuovo backup vecchio: ${old}"
            rm -f "${old}"
        done
    fi

    log "Aggiorno opentakserver come utente ${OTS_USER} ..."
    if [[ -n "${OTS_GIT_SOURCE}" ]]; then
        # --force-reinstall: senza, pip può considerare l'installazione già
        # soddisfatta e non ripescare un nuovo commit sullo stesso branch.
        sudo -u "${OTS_USER}" "${PIP}" install --upgrade --force-reinstall "${OTS_GIT_SOURCE}"
    else
        sudo -u "${OTS_USER}" "${PIP}" install --upgrade opentakserver
    fi

    NEW_VERSION="$(installed_version)"
    log "Installata versione: ${NEW_VERSION}"

    # Import di base PRIMA di riavviare: un modulo rotto altrimenti si scopre
    # solo dal ciclo di riavvii delle unit (Restart=always le fa sembrare attive).
    check_imports

    log "Riavvio ${ALL_SERVICES} ..."
    for svc in ${ALL_SERVICES}; do
        systemctl start "${svc}" || true
    done
    SERVICES_STOPPED=0
    sleep 5

    FAILED_SERVICES=""
    for svc in ${ALL_SERVICES}; do
        if systemctl is-active --quiet "${svc}"; then
            log "${svc}: attivo."
        else
            warn "${svc}: NON attivo dopo l'avvio. Ultime righe di log:"
            journalctl -u "${svc}" --no-pager -n 30 || true
            FAILED_SERVICES="${FAILED_SERVICES} ${svc}"
        fi
    done
    if [[ -n "${FAILED_SERVICES}" ]]; then
        tail -n 30 "${OTS_DATA}/logs/opentakserver.log" 2>/dev/null || true
        die "Aggiornamento fallito, servizi non partiti:${FAILED_SERVICES}.${BACKUP_FILE:+ Backup disponibile: ${BACKUP_FILE}}"
    fi

    # cot_parser dal fork n3 è un supervisore: padre + un figlio per
    # OTS_COT_PARSER_PROCESSES. Solo il padre = il figlio muore in loop.
    PARSER_PROCS="$(pgrep -fc 'bin/cot_parser' || true)"
    if (( ${PARSER_PROCS:-0} >= 2 )); then
        log "cot_parser: ${PARSER_PROCS} processi (padre + figli)."
    else
        warn "cot_parser: ${PARSER_PROCS:-0} processi, atteso padre + almeno un figlio. Log: ${OTS_DATA}/logs/cot_parser.log"
    fi

    # Verifica porte (8087 spesso volutamente disabilitata: solo avviso)
    sleep 3
    for port in 8080 8089 8443; do
        if ss -tln "( sport = :${port} )" | grep -q LISTEN; then
            log "Porta ${port}: OK"
        else
            warn "Porta ${port}: NON in ascolto — controlla i log!"
        fi
    done

    log "Ultime righe del log applicativo:"
    tail -n 15 "${OTS_DATA}/logs/opentakserver.log" 2>/dev/null || warn "Log applicativo non trovato."
fi

# ------------------------- Upgrade UI (opzionale) -------------------------
if (( MODE_UI )); then
    log "Aggiornamento web UI da ${UI_REPO} ..."

    # Trova la root della UI: override manuale via UI_ROOT, altrimenti cerca
    # nei config nginx (tutta /etc/nginx) una root che contenga un index.html
    if [[ -z "${UI_ROOT:-}" ]]; then
        while read -r candidate; do
            if [[ -f "${candidate}/index.html" ]]; then
                UI_ROOT="${candidate}"
                break
            fi
        done < <(grep -rhoP '^\s*root\s+\K[^;]+' /etc/nginx/ 2>/dev/null | tr -d '"' | sort -u)
    fi
    [[ -n "${UI_ROOT:-}" && -d "${UI_ROOT}" ]] || die "Root della UI non trovata. Individuala con: grep -rn 'root' /etc/nginx/ | grep -v '#'  e rilancia con: UI_ROOT=/percorso/ui $0 --ui"
    log "UI servita da nginx in: ${UI_ROOT}"

    RELEASE_JSON="$(curl -fsSL "https://api.github.com/repos/${UI_REPO}/releases/latest")" \
        || die "Impossibile interrogare le release GitHub di ${UI_REPO}."
    UI_TAG="$(echo "${RELEASE_JSON}" | "${PY}" -c 'import sys,json; print(json.load(sys.stdin)["tag_name"])')"
    UI_ZIP_URL="$(echo "${RELEASE_JSON}" | "${PY}" -c '
import sys, json
r = json.load(sys.stdin)
for a in r.get("assets", []):
    if a["name"].endswith(".zip"):
        print(a["browser_download_url"]); break
')"
    [[ -n "${UI_ZIP_URL}" ]] || die "La release ${UI_TAG} non ha un asset .zip precompilato: aggiorna la UI manualmente (build npm dal sorgente)."

    log "Scarico UI ${UI_TAG} ..."
    TMP="$(mktemp -d)"
    curl -fsSL -o "${TMP}/ui.zip" "${UI_ZIP_URL}"

    UI_BACKUP="${BACKUP_DIR}/ots-ui-${STAMP}.tar.gz"
    log "Backup UI attuale in ${UI_BACKUP} ..."
    tar czf "${UI_BACKUP}" -C "$(dirname "${UI_ROOT}")" "$(basename "${UI_ROOT}")"

    log "Installo la nuova UI in ${UI_ROOT} ..."
    unzip -q "${TMP}/ui.zip" -d "${TMP}/ui"
    # Se lo zip contiene una singola directory radice, usa il suo contenuto
    SRC="${TMP}/ui"
    if [[ "$(find "${TMP}/ui" -mindepth 1 -maxdepth 1 | wc -l)" -eq 1 && -d "$(find "${TMP}/ui" -mindepth 1 -maxdepth 1)" ]]; then
        SRC="$(find "${TMP}/ui" -mindepth 1 -maxdepth 1)"
    fi
    rm -rf "${UI_ROOT:?}"/*
    cp -a "${SRC}"/. "${UI_ROOT}/"

    nginx -t && systemctl reload nginx
    log "UI aggiornata a ${UI_TAG}. Backup precedente: ${UI_BACKUP}"

    # Riapplica il branding personalizzato (logo/favicon), che l'update della UI sovrascrive
    BRANDING_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/apply-branding.sh"
    if [[ -x "${BRANDING_SCRIPT}" ]]; then
        log "Riapplico il branding personalizzato..."
        "${BRANDING_SCRIPT}" || warn "apply-branding.sh fallito: rilancialo a mano."
    fi
fi

log "Fatto."
if [[ -n "${BACKUP_FILE}" && -f "${BACKUP_FILE}" ]]; then
    log "In caso di problemi, ripristina i dati con:"
    log "  systemctl stop ${ALL_SERVICES} && tar xzf ${BACKUP_FILE} -C $(dirname "${OTS_DATA}") && systemctl start ${ALL_SERVICES}"
fi
