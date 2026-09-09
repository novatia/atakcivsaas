#!/usr/bin/env bash
#
# install-eventcalendar-plugin.sh — Installa o aggiorna OTS-EventCalendar-Plugin
# nel venv di OpenTAKServer a partire da questo repo.
#
# Uso (da root sul server, dentro il clone del repo):
#   ./install-eventcalendar-plugin.sh            # (ri)installa il plugin + restart + verifica
#   ./install-eventcalendar-plugin.sh --check    # mostra solo la versione installata
#   ./install-eventcalendar-plugin.sh --pull     # prima fa git pull del repo, poi installa
#
# Percorsi/nomi sovrascrivibili via variabili d'ambiente, es:
#   OTS_USER=ots OTS_SERVICE=opentakserver ./install-eventcalendar-plugin.sh

set -euo pipefail

# ------------------------- Configurazione -------------------------
OTS_USER="${OTS_USER:-ots}"
OTS_VENV="${OTS_VENV:-/home/${OTS_USER}/.opentakserver_venv}"
OTS_DATA="${OTS_DATA:-/home/${OTS_USER}/ots}"
OTS_SERVICE="${OTS_SERVICE:-opentakserver}"

PLUGIN_DISTRO="OTS-EventCalendar-Plugin"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="${PLUGIN_DIR:-${SCRIPT_DIR}/../plugins/${PLUGIN_DISTRO}}"

PIP="${OTS_VENV}/bin/pip"

# ------------------------- Utility -------------------------
log()  { echo -e "\e[1;32m[PLUGIN-INSTALL]\e[0m $*"; }
warn() { echo -e "\e[1;33m[PLUGIN-INSTALL]\e[0m $*" >&2; }
die()  { echo -e "\e[1;31m[PLUGIN-INSTALL]\e[0m $*" >&2; exit 1; }

installed_version() {
    "${PIP}" show "${PLUGIN_DISTRO}" 2>/dev/null | awk '/^Version:/{print $2}'
}

# ------------------------- Preflight -------------------------
[[ $EUID -eq 0 ]] || die "Esegui come root (serve per systemctl)."
[[ -x "${PIP}" ]] || die "Venv non trovato: ${OTS_VENV}"
[[ -f "${PLUGIN_DIR}/pyproject.toml" ]] || die "Plugin non trovato in: ${PLUGIN_DIR}"
systemctl cat "${OTS_SERVICE}" >/dev/null 2>&1 || die "Servizio systemd '${OTS_SERVICE}' non trovato."

CURRENT="$(installed_version)"
if [[ -n "${CURRENT}" ]]; then
    log "Versione installata di ${PLUGIN_DISTRO}: ${CURRENT}"
else
    log "${PLUGIN_DISTRO} non risulta installato: procedo con la prima installazione."
fi

if [[ "${1:-}" == "--check" ]]; then
    exit 0
fi

if [[ "${1:-}" == "--pull" ]]; then
    log "git pull del repo..."
    git -C "${SCRIPT_DIR}" pull --ff-only
fi

# ------------------------- Install / Update -------------------------
# pip installa da directory locale ricostruendo sempre il pacchetto,
# quindi lo stesso comando fa sia install che update.
log "Installo/aggiorno ${PLUGIN_DISTRO} nel venv come utente ${OTS_USER} ..."
sudo -u "${OTS_USER}" "${PIP}" install --upgrade "${PLUGIN_DIR}"

NEW_VERSION="$(installed_version)"
log "Installata versione: ${NEW_VERSION:-sconosciuta}"

log "Riavvio ${OTS_SERVICE} ..."
systemctl restart "${OTS_SERVICE}"
sleep 5

if ! systemctl is-active --quiet "${OTS_SERVICE}"; then
    warn "Il servizio NON è attivo dopo il riavvio. Ultime righe di log:"
    journalctl -u "${OTS_SERVICE}" --no-pager -n 30 || true
    tail -n 30 "${OTS_DATA}/logs/opentakserver.log" 2>/dev/null || true
    die "Installazione fallita. Per rimuovere il plugin: sudo -u ${OTS_USER} ${PIP} uninstall --yes ${PLUGIN_DISTRO}"
fi
log "Servizio attivo."

# ------------------------- Verifica caricamento plugin -------------------------
sleep 3
if tail -n 100 "${OTS_DATA}/logs/opentakserver.log" 2>/dev/null | grep -qi "Successfully Loaded ${PLUGIN_DISTRO}"; then
    log "Plugin caricato correttamente."
else
    warn "Non trovo la conferma di caricamento nel log applicativo — controlla:"
    tail -n 30 "${OTS_DATA}/logs/opentakserver.log" 2>/dev/null || warn "Log applicativo non trovato."
fi

log "Fatto. UI del plugin: https://<server>/api/plugins/ots_eventcalendar_plugin/ui"
log "Per disinstallare: sudo -u ${OTS_USER} ${PIP} uninstall --yes ${PLUGIN_DISTRO} && systemctl restart ${OTS_SERVICE}"
