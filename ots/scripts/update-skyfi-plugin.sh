#!/usr/bin/env bash
#
# update-skyfi-plugin.sh — Installa o aggiorna OTS-SkyFi-Plugin nel venv di
# OpenTAKServer, compilando la web UI che il repo upstream NON distribuisce.
#
# Contesto (verificato 2026-09-14): il repo brian7704/OTS-SkyFi-Plugin dichiara
# in pyproject.toml di includere ots_skyfi_plugin/ui/**, ma quella cartella non
# esiste: il submodule "ui" è definito in .gitmodules senza gitlink committato,
# quindi né pip né git lo scaricano. Questo script clona separatamente il
# template UI (brian7704/OTS-UI-Plugin-Template), lo compila con vite e mette
# il build in ots_skyfi_plugin/ui prima della pip install. NOTA: finché
# l'autore non pubblica una UI vera, la pagina è il placeholder del template;
# le API del plugin (/orders, /config, data package) funzionano comunque.
#
# Uso (da root sul server):
#   ./update-skyfi-plugin.sh            # clone/pull + build UI + install + restart + verifica
#   ./update-skyfi-plugin.sh --check    # mostra solo la versione installata
#   ./update-skyfi-plugin.sh --no-ui    # salta il build della UI (solo backend)
#
# Percorsi/nomi sovrascrivibili via variabili d'ambiente, es:
#   OTS_USER=ots OTS_SERVICE=opentakserver ./update-skyfi-plugin.sh

set -euo pipefail

# ------------------------- Configurazione -------------------------
OTS_USER="${OTS_USER:-ots}"
OTS_VENV="${OTS_VENV:-/home/${OTS_USER}/.opentakserver_venv}"
OTS_DATA="${OTS_DATA:-/home/${OTS_USER}/ots}"
OTS_SERVICE="${OTS_SERVICE:-opentakserver}"

PLUGIN_DISTRO="OTS-SkyFi-Plugin"
PLUGIN_REPO="${PLUGIN_REPO:-https://github.com/brian7704/OTS-SkyFi-Plugin.git}"
UI_REPO="${UI_REPO:-https://github.com/brian7704/OTS-UI-Plugin-Template.git}"
# I sorgenti restano sul server per le pull successive
SRC_DIR="${SRC_DIR:-/home/${OTS_USER}/plugin-src}"
PLUGIN_CLONE="${SRC_DIR}/OTS-SkyFi-Plugin"
UI_CLONE="${SRC_DIR}/OTS-UI-Plugin-Template"

PIP="${OTS_VENV}/bin/pip"

# ------------------------- Utility -------------------------
log()  { echo -e "\e[1;32m[SKYFI-UPDATE]\e[0m $*"; }
warn() { echo -e "\e[1;33m[SKYFI-UPDATE]\e[0m $*" >&2; }
die()  { echo -e "\e[1;31m[SKYFI-UPDATE]\e[0m $*" >&2; exit 1; }
step() { echo -e "\n\e[1;36m==>\e[0m \e[1m$*\e[0m"; }

installed_version() {
    # "|| true": alla prima installazione pip show fallisce e con set -e/pipefail
    # farebbe uscire lo script in silenzio
    "${PIP}" show "${PLUGIN_DISTRO}" 2>/dev/null | awk '/^Version:/{print $2}' || true
}

# Clona il repo se manca, altrimenti lo riallinea a origin scartando modifiche
# locali (il clone sul server è solo di deploy)
sync_repo() {
    local url="$1" dest="$2"
    if [[ -d "${dest}/.git" ]]; then
        local branch
        branch="$(git -C "${dest}" rev-parse --abbrev-ref HEAD)"
        if ! git -C "${dest}" diff --quiet; then
            warn "Modifiche locali in ${dest}: le scarto (git reset --hard)."
        fi
        git -C "${dest}" fetch origin
        git -C "${dest}" reset --hard "origin/${branch}"
    else
        git clone "${url}" "${dest}"
    fi
    log "$(basename "${dest}") a: $(git -C "${dest}" log -1 --format='%h %s')"
}

# ------------------------- Preflight -------------------------
step "Preflight"
[[ $EUID -eq 0 ]] || die "Esegui come root (serve per systemctl)."
[[ -x "${PIP}" ]] || die "Venv non trovato: ${OTS_VENV}"
systemctl cat "${OTS_SERVICE}" >/dev/null 2>&1 || die "Servizio systemd '${OTS_SERVICE}' non trovato."
command -v git >/dev/null 2>&1 || die "git non installato."

BUILD_UI=1
[[ "${1:-}" == "--no-ui" ]] && BUILD_UI=0
if (( BUILD_UI )); then
    command -v npm >/dev/null 2>&1 || die "npm non installato: serve per compilare la UI (apt install nodejs npm, o rilancia con --no-ui)."
fi

log "Utente OTS: ${OTS_USER}"
log "Venv:       ${OTS_VENV}"
log "Sorgenti:   ${SRC_DIR}"
log "Servizio:   ${OTS_SERVICE}"

OTS_VERSION="$("${PIP}" show opentakserver 2>/dev/null | awk '/^Version:/{print $2}' || true)"
log "OpenTAKServer installato: ${OTS_VERSION:-NON TROVATO}"

CURRENT="$(installed_version)"
if [[ -n "${CURRENT}" ]]; then
    log "Versione installata di ${PLUGIN_DISTRO}: ${CURRENT}"
else
    log "${PLUGIN_DISTRO} non risulta installato: prima installazione."
fi

if [[ "${1:-}" == "--check" ]]; then
    log "Modalità --check: nessuna modifica effettuata."
    exit 0
fi

# ------------------------- Sorgenti -------------------------
step "Aggiornamento sorgenti (plugin + template UI)"
mkdir -p "${SRC_DIR}"
sync_repo "${PLUGIN_REPO}" "${PLUGIN_CLONE}"
if (( BUILD_UI )); then
    sync_repo "${UI_REPO}" "${UI_CLONE}"
fi
# pip install e npm girano come ${OTS_USER}: deve poter leggere e scrivere
chown -R "${OTS_USER}:${OTS_USER}" "${SRC_DIR}"

# ------------------------- Build UI -------------------------
UI_DEST="${PLUGIN_CLONE}/ots_skyfi_plugin/ui"
if (( BUILD_UI )); then
    step "Build della UI (vite)"
    log "npm install in ${UI_CLONE} ..."
    sudo -u "${OTS_USER}" -H bash -c "cd '${UI_CLONE}' && npm install --no-audit --no-fund"

    # Il build script del template è "tsc && vite build --base ./" con outDir
    # ../ots_plugin_template/ui: qui forziamo la destinazione dentro il clone
    # del plugin e saltiamo tsc (typecheck non necessario per il deploy)
    log "vite build -> ${UI_DEST}"
    sudo -u "${OTS_USER}" -H bash -c "cd '${UI_CLONE}' && npx vite build --base ./ --outDir '${UI_DEST}' --emptyOutDir"

    [[ -f "${UI_DEST}/index.html" ]] || die "Build UI terminato ma manca ${UI_DEST}/index.html"
    log "UI compilata ($(find "${UI_DEST}" -type f | wc -l) file)."
else
    warn "Build UI saltato (--no-ui): la pagina /ui resterà vuota se ${UI_DEST} non esiste già."
fi

# ------------------------- Install / Update -------------------------
# pip installa da directory locale ricostruendo sempre il pacchetto,
# quindi lo stesso comando fa sia install che update.
step "Installazione plugin nel venv (utente ${OTS_USER})"
sudo -u "${OTS_USER}" "${PIP}" install --upgrade "${PLUGIN_CLONE}"

NEW_VERSION="$(installed_version)"
[[ -n "${NEW_VERSION}" ]] || die "pip install terminato ma ${PLUGIN_DISTRO} non risulta installato!"
if [[ -n "${CURRENT}" && "${CURRENT}" != "${NEW_VERSION}" ]]; then
    log "Aggiornato: ${CURRENT} -> ${NEW_VERSION}"
else
    log "Installata versione: ${NEW_VERSION}"
fi

# Verifica che la UI sia finita davvero dentro il pacchetto installato
INSTALLED_UI="$("${PIP}" show -f "${PLUGIN_DISTRO}" 2>/dev/null | grep -c 'ui/index.html' || true)"
if (( BUILD_UI )) && [[ "${INSTALLED_UI}" == "0" ]]; then
    warn "ui/index.html NON risulta nel pacchetto installato: la UI non verrà servita."
fi

# ------------------------- Restart servizio -------------------------
step "Riavvio ${OTS_SERVICE}"
systemctl restart "${OTS_SERVICE}"
log "Attendo l'avvio del servizio..."
sleep 5

if ! systemctl is-active --quiet "${OTS_SERVICE}"; then
    warn "Il servizio NON è attivo dopo il riavvio. Ultime righe di log:"
    journalctl -u "${OTS_SERVICE}" --no-pager -n 30 || true
    tail -n 30 "${OTS_DATA}/logs/opentakserver.log" 2>/dev/null || true
    die "Installazione fallita. Per rimuovere il plugin: sudo -u ${OTS_USER} ${PIP} uninstall --yes ${PLUGIN_DISTRO}"
fi
log "Servizio attivo."

# ------------------------- Verifica caricamento plugin -------------------------
step "Verifica caricamento plugin"
# All'avvio OTS fa prima le migrazioni DB e carica i plugin dopo:
# facciamo polling del log fino a 60 secondi invece di un'attesa fissa.
LOADED=""
for _ in $(seq 1 12); do
    if tail -n 200 "${OTS_DATA}/logs/opentakserver.log" 2>/dev/null | grep -qi "Successfully Loaded ${PLUGIN_DISTRO}"; then
        LOADED=1
        break
    fi
    echo -n "."
    sleep 5
done
echo ""

if [[ -n "${LOADED}" ]]; then
    log "Plugin caricato correttamente:"
    tail -n 200 "${OTS_DATA}/logs/opentakserver.log" | grep -i "SkyFi" | tail -n 5 || true
else
    warn "Nessuna conferma di caricamento entro 60s. Controlla manualmente con:"
    warn "  grep -i SkyFi ${OTS_DATA}/logs/opentakserver.log | tail"
    warn "Ultime righe del log applicativo:"
    tail -n 30 "${OTS_DATA}/logs/opentakserver.log" 2>/dev/null || warn "Log applicativo non trovato."
fi

# Verifica porte come update-ots.sh
for port in 8080 8443; do
    if ss -tln "( sport = :${port} )" | grep -q LISTEN; then
        log "Porta ${port}: OK"
    else
        warn "Porta ${port}: NON in ascolto — controlla i log!"
    fi
done

step "Fatto"
log "UI del plugin (solo utenti administrator): https://<server>/api/plugins/skyfi/ui"
log "Senza account/API key SkyFi configurati il log mostra 'Failed to get orders: Not Found' — innocuo."
log "Per disinstallare: sudo -u ${OTS_USER} ${PIP} uninstall --yes ${PLUGIN_DISTRO} && systemctl restart ${OTS_SERVICE}"
