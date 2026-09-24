#!/usr/bin/env bash
#
# install-milsim-companion-plugin.sh — Installa o aggiorna OTS-MilSim-Companion-Plugin
# nel venv di OpenTAKServer a partire da questo repo. Disinstalla da solo i
# pacchetti precedenti (OTS-EventCalendar-Plugin, OTS-GameMode-Plugin,
# OTS-SkyFi-Plugin, ora tutti fusi qui): i dati restano perché le tabelle DB
# non cambiano nome e le chiavi di config.yml restano le stesse.
#
# Uso (da root sul server, dentro il clone del repo):
#   ./install-milsim-companion-plugin.sh            # (ri)installa il plugin + restart + verifica
#   ./install-milsim-companion-plugin.sh --check    # mostra solo la versione installata
#   ./install-milsim-companion-plugin.sh --pull     # prima fa git pull del repo, poi installa
#
# Percorsi/nomi sovrascrivibili via variabili d'ambiente, es:
#   OTS_USER=ots OTS_SERVICE=opentakserver ./install-milsim-companion-plugin.sh

set -euo pipefail

# ------------------------- Configurazione -------------------------
OTS_USER="${OTS_USER:-ots}"
OTS_VENV="${OTS_VENV:-/home/${OTS_USER}/.opentakserver_venv}"
OTS_DATA="${OTS_DATA:-/home/${OTS_USER}/ots}"
OTS_SERVICE="${OTS_SERVICE:-opentakserver}"

PLUGIN_DISTRO="OTS-MilSim-Companion-Plugin"
LEGACY_DISTROS=("OTS-EventCalendar-Plugin" "OTS-GameMode-Plugin" "OTS-SkyFi-Plugin")
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="${PLUGIN_DIR:-${SCRIPT_DIR}/../plugins/${PLUGIN_DISTRO}}"

PIP="${OTS_VENV}/bin/pip"

# ------------------------- Utility -------------------------
log()  { echo -e "\e[1;32m[PLUGIN-INSTALL]\e[0m $*"; }
warn() { echo -e "\e[1;33m[PLUGIN-INSTALL]\e[0m $*" >&2; }
die()  { echo -e "\e[1;31m[PLUGIN-INSTALL]\e[0m $*" >&2; exit 1; }
step() { echo -e "\n\e[1;36m==>\e[0m \e[1m$*\e[0m"; }

installed_version() {
    # "|| true": alla prima installazione pip show fallisce e con set -e/pipefail
    # farebbe uscire lo script in silenzio
    "${PIP}" show "${PLUGIN_DISTRO}" 2>/dev/null | awk '/^Version:/{print $2}' || true
}

# ------------------------- Preflight -------------------------
step "Preflight"
[[ $EUID -eq 0 ]] || die "Esegui come root (serve per systemctl)."
[[ -x "${PIP}" ]] || die "Venv non trovato: ${OTS_VENV}"
[[ -f "${PLUGIN_DIR}/pyproject.toml" ]] || die "Plugin non trovato in: ${PLUGIN_DIR}"
systemctl cat "${OTS_SERVICE}" >/dev/null 2>&1 || die "Servizio systemd '${OTS_SERVICE}' non trovato."

log "Utente OTS:      ${OTS_USER}"
log "Venv:            ${OTS_VENV}"
log "Sorgente plugin: $(cd "${PLUGIN_DIR}" && pwd)"
log "Servizio:        ${OTS_SERVICE}"

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

# ------------------------- Git pull (opzionale) -------------------------
if [[ "${1:-}" == "--pull" ]]; then
    step "Aggiornamento repo (git pull)"
    # Il clone sul server è solo di deploy: scarta eventuali modifiche locali
    # (es. chmod, edit al volo) che bloccherebbero la pull
    BRANCH="$(git -C "${SCRIPT_DIR}" rev-parse --abbrev-ref HEAD)"
    if ! git -C "${SCRIPT_DIR}" diff --quiet; then
        warn "Modifiche locali nel repo: le scarto (git reset --hard)."
    fi
    git -C "${SCRIPT_DIR}" fetch origin
    git -C "${SCRIPT_DIR}" reset --hard "origin/${BRANCH}"
    log "Repo a: $(git -C "${SCRIPT_DIR}" log -1 --format='%h %s')"
fi

# ------------------------- Rimozione pacchetti precedenti -------------------------
# Il plugin unifica OTS-EventCalendar-Plugin e OTS-GameMode-Plugin: se sono
# ancora installati vanno tolti, altrimenti OTS caricherebbe rotte duplicate.
step "Rimozione plugin precedenti (se presenti)"
for legacy in "${LEGACY_DISTROS[@]}"; do
    if [[ -n "$("${PIP}" show "${legacy}" 2>/dev/null || true)" ]]; then
        log "Trovato ${legacy}: lo disinstallo."
        sudo -u "${OTS_USER}" "${PIP}" uninstall --yes "${legacy}"
    fi
done

# ------------------------- Install / Update -------------------------
# pip installa da directory locale ricostruendo sempre il pacchetto,
# quindi lo stesso comando fa sia install che update.
# ------------------------- Dipendenze di sistema -------------------------
# GDAL (gdalinfo, gdalwarp, gdal_translate, gdaladdo) serve alla «Mappa
# offline HD» del tab SkyFi (3.16.0+). Senza, il resto del plugin funziona e
# il bottone risponde con l'istruzione per installarlo.
step "Dipendenze di sistema (GDAL)"
if command -v gdalwarp >/dev/null 2>&1 && command -v gdaladdo >/dev/null 2>&1; then
    log "GDAL presente: $(gdalinfo --version 2>/dev/null || echo '?')"
elif command -v apt-get >/dev/null 2>&1; then
    log "GDAL assente: installo gdal-bin (serve alla mappa offline HD di SkyFi)."
    # Senza update l'indice può essere vecchio (404 sui .deb) o non avere
    # ancora universe: l'errore di apt resta visibile, niente >/dev/null
    apt-get update -qq || warn "apt-get update fallito: provo comunque l'installazione."
    if DEBIAN_FRONTEND=noninteractive apt-get install -y -q gdal-bin; then
        log "Installato: $(gdalinfo --version 2>/dev/null || echo '?')"
    else
        warn "Installazione di gdal-bin fallita: la mappa offline HD non funzionerà finché non lo installi (apt install gdal-bin)."
    fi
else
    warn "GDAL assente e apt-get non disponibile: installa i comandi GDAL a mano per la mappa offline HD."
fi

step "Installazione plugin nel venv (utente ${OTS_USER})"
sudo -u "${OTS_USER}" "${PIP}" install --upgrade "${PLUGIN_DIR}"

NEW_VERSION="$(installed_version)"
[[ -n "${NEW_VERSION}" ]] || die "pip install terminato ma ${PLUGIN_DISTRO} non risulta installato!"
if [[ -n "${CURRENT}" && "${CURRENT}" != "${NEW_VERSION}" ]]; then
    log "Aggiornato: ${CURRENT} -> ${NEW_VERSION}"
else
    log "Installata versione: ${NEW_VERSION}"
fi

# ------------------------- Restart servizio -------------------------
step "Riavvio ${OTS_SERVICE}"

# Il riavvio SCOLLEGA TUTTI GLI EUD: opentakserver-eud-handler.service ha
# PartOf=opentakserver.service, quindi systemd lo riavvia insieme al servizio
# principale e ogni connessione TCP sulla 8089 cade. Gli ATAK si riconnettono
# da soli, ma non all'istante: un telefono in background può metterci minuti,
# e nel frattempo smette anche di rilanciare i tag Meshtastic. Mai installare
# a partita in corso.
CONNECTED_EUDS=$(ss -Htn state established "( sport = :8089 )" 2>/dev/null | wc -l)
if [ "${CONNECTED_EUDS:-0}" -gt 0 ]; then
    warn "Ci sono ${CONNECTED_EUDS} EUD collegati sulla 8089: il riavvio li scollegherà tutti."
    if [ -t 0 ] && [ "${ASSUME_YES:-0}" != "1" ]; then
        read -r -p "Procedere comunque? [s/N] " REPLY
        case "${REPLY}" in
            s|S|y|Y) ;;
            *) die "Annullato. Rilancia a partita finita, o con ASSUME_YES=1 per non chiedere." ;;
        esac
    else
        warn "Nessun terminale interattivo (o ASSUME_YES=1): procedo."
    fi
fi

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
    tail -n 200 "${OTS_DATA}/logs/opentakserver.log" | grep -i "MilSim" | tail -n 5 || true
else
    warn "Nessuna conferma di caricamento entro 60s. Controlla manualmente con:"
    warn "  grep -i MilSim ${OTS_DATA}/logs/opentakserver.log | tail"
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

# Monitor Meshtastic (3.13.0+): il consumer si lega all'exchange firehose di
# RabbitMQ. Se non si connette, la tab Meshtastic resta vuota senza dirlo
# a voce alta: meglio accorgersene qui.
if grep -qi "MilSim mesh: firehose connesso a RabbitMQ" "${OTS_DATA}/logs/opentakserver.log" 2>/dev/null; then
    log "Monitor Meshtastic: connesso al firehose"
else
    warn "Monitor Meshtastic: nessuna conferma di connessione al firehose."
    warn "  grep -i 'MilSim mesh' ${OTS_DATA}/logs/opentakserver.log | tail"
    warn "  (se il monitor e' disabilitato in config.yml e' normale: OTS_MILSIM_MESH_ENABLED)"
fi

step "Fatto"
log "UI del plugin: https://<server>/api/plugins/ots_milsim_companion_plugin/ui"
log "Monitor Meshtastic: tab «Meshtastic»; mappature canale->gruppo: tab «Canali Meshtastic»"
log "Per disinstallare: sudo -u ${OTS_USER} ${PIP} uninstall --yes ${PLUGIN_DISTRO} && systemctl restart ${OTS_SERVICE}"
