#!/usr/bin/env bash
#
# update-ots.sh — Aggiorna OpenTAKServer (backend pip + opzionalmente la web UI)
#
# Uso (da root sul server):
#   ./update-ots.sh              # backup + upgrade backend + restart + verifica
#   ./update-ots.sh --check      # mostra solo versione installata vs ultima su PyPI
#   ./update-ots.sh --ui         # aggiorna anche la web UI servita da nginx
#
# Percorsi/nomi sovrascrivibili via variabili d'ambiente, es:
#   OTS_USER=ots OTS_SERVICE=opentakserver ./update-ots.sh

set -euo pipefail

# ------------------------- Configurazione -------------------------
OTS_USER="${OTS_USER:-ots}"
OTS_VENV="${OTS_VENV:-/home/${OTS_USER}/.opentakserver_venv}"
OTS_DATA="${OTS_DATA:-/home/${OTS_USER}/ots}"
OTS_SERVICE="${OTS_SERVICE:-opentakserver}"
BACKUP_DIR="${BACKUP_DIR:-/root/ots-backups}"
KEEP_BACKUPS="${KEEP_BACKUPS:-5}"
UI_REPO="${UI_REPO:-brian7704/OpenTAKServer-UI}"

PIP="${OTS_VENV}/bin/pip"
PY="${OTS_VENV}/bin/python3"

# ------------------------- Utility -------------------------
log()  { echo -e "\e[1;32m[OTS-UPDATE]\e[0m $*"; }
warn() { echo -e "\e[1;33m[OTS-UPDATE]\e[0m $*" >&2; }
die()  { echo -e "\e[1;31m[OTS-UPDATE]\e[0m $*" >&2; exit 1; }

installed_version() {
    # "|| true": se il pacchetto non è installato pip show fallisce e con
    # set -e/pipefail lo script uscirebbe in silenzio prima del messaggio di errore
    "${PIP}" show opentakserver 2>/dev/null | awk '/^Version:/{print $2}' || true
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

CURRENT="$(installed_version)"
[[ -n "${CURRENT}" ]] || die "opentakserver non risulta installato in ${OTS_VENV}"
LATEST="$(latest_version)"

log "Versione installata: ${CURRENT}   Ultima su PyPI: ${LATEST}"

if [[ "${1:-}" == "--check" ]]; then
    if [[ "${CURRENT}" == "${LATEST}" ]]; then
        log "Sei già all'ultima versione."
    else
        log "Aggiornamento disponibile: ${CURRENT} -> ${LATEST}. Esegui senza --check per applicarlo."
    fi
    exit 0
fi

if [[ "${CURRENT}" == "${LATEST}" ]]; then
    log "Già all'ultima versione (${CURRENT}). Nessun aggiornamento backend necessario."
    [[ "${1:-}" == "--ui" ]] || exit 0
fi

# ------------------------- Backup -------------------------
mkdir -p "${BACKUP_DIR}"
STAMP="$(date +%F_%H%M%S)"
BACKUP_FILE="${BACKUP_DIR}/ots-data-${CURRENT}-${STAMP}.tar.gz"

# ------------------------- Upgrade backend -------------------------
if [[ "${CURRENT}" != "${LATEST}" ]]; then
    log "Backup di ${OTS_DATA} in ${BACKUP_FILE} ..."
    # I log vengono scritti mentre tar li legge: exit code 1 ("file changed as
    # we read it") è accettabile, solo >1 è un errore reale.
    set +e
    tar czf "${BACKUP_FILE}" --warning=no-file-changed \
        -C "$(dirname "${OTS_DATA}")" "$(basename "${OTS_DATA}")"
    TAR_RC=$?
    set -e
    if (( TAR_RC > 1 )); then
        die "Backup fallito (tar exit ${TAR_RC})."
    fi
    log "Backup completato ($(du -h "${BACKUP_FILE}" | cut -f1))."

    # Rotazione: tieni solo gli ultimi KEEP_BACKUPS
    ls -1t "${BACKUP_DIR}"/ots-data-*.tar.gz 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)) | while read -r old; do
        warn "Rimuovo backup vecchio: ${old}"
        rm -f "${old}"
    done

    log "Aggiorno opentakserver come utente ${OTS_USER} ..."
    sudo -u "${OTS_USER}" "${PIP}" install --upgrade opentakserver

    NEW_VERSION="$(installed_version)"
    log "Installata versione: ${NEW_VERSION}"

    log "Riavvio ${OTS_SERVICE} ..."
    systemctl restart "${OTS_SERVICE}"
    sleep 5

    if ! systemctl is-active --quiet "${OTS_SERVICE}"; then
        warn "Il servizio NON è attivo dopo il riavvio. Ultime righe di log:"
        journalctl -u "${OTS_SERVICE}" --no-pager -n 30 || true
        tail -n 30 "${OTS_DATA}/logs/opentakserver.log" 2>/dev/null || true
        die "Aggiornamento fallito. Backup disponibile: ${BACKUP_FILE}"
    fi
    log "Servizio attivo."

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
if [[ "${1:-}" == "--ui" ]]; then
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
    trap 'rm -rf "${TMP}"' EXIT
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
fi

log "Fatto."
if [[ -f "${BACKUP_FILE}" ]]; then
    log "In caso di problemi, ripristina i dati con:"
    log "  systemctl stop ${OTS_SERVICE} && tar xzf ${BACKUP_FILE} -C $(dirname "${OTS_DATA}") && systemctl start ${OTS_SERVICE}"
fi
