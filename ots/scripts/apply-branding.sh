#!/usr/bin/env bash
#
# apply-branding.sh — Applica il logo personalizzato alla web UI di OpenTAKServer.
#
# Sostituisce il logo OTS (assets/ots-logo-<hash>.png nella root nginx) con
# ots/branding/logo.png e, se presenti in ots/branding/, anche le favicon.
# Inietta inoltre auth-guard.js, che rimanda al login chi apre la UI senza sessione.
#
# Va rilanciato dopo ogni aggiornamento della UI (update-ots.sh --ui lo fa da solo).
#
# Uso (da root sul server):
#   ./apply-branding.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRANDING_DIR="${BRANDING_DIR:-${SCRIPT_DIR}/../branding}"

log()  { echo -e "\e[1;32m[BRANDING]\e[0m $*"; }
warn() { echo -e "\e[1;33m[BRANDING]\e[0m $*" >&2; }
die()  { echo -e "\e[1;31m[BRANDING]\e[0m $*" >&2; exit 1; }

[[ -f "${BRANDING_DIR}/logo.png" ]] || die "Logo non trovato: ${BRANDING_DIR}/logo.png"

# Trova la root della UI: override manuale via UI_ROOT, altrimenti cerca
# nei config nginx (tutta /etc/nginx) una root che contenga un index.html
# (stessa logica di update-ots.sh)
if [[ -z "${UI_ROOT:-}" ]]; then
    while read -r candidate; do
        if [[ -f "${candidate}/index.html" ]]; then
            UI_ROOT="${candidate}"
            break
        fi
    done < <(grep -rhoP '^\s*root\s+\K[^;]+' /etc/nginx/ 2>/dev/null | tr -d '"' | sort -u)
fi
[[ -n "${UI_ROOT:-}" && -d "${UI_ROOT}" ]] || die "Root della UI non trovata. Individuala con: grep -rn 'root' /etc/nginx/ | grep -v '#'  e rilancia con: UI_ROOT=/percorso/ui $0"
log "UI servita da nginx in: ${UI_ROOT}"

# ------------------------- Logo principale -------------------------
# Vite emette il logo come assets/ots-logo-<hash>.png: sovrascriviamo il file
# mantenendo il nome, così i riferimenti nel JS continuano a funzionare.
FOUND=0
while IFS= read -r -d '' target; do
    cp "${BRANDING_DIR}/logo.png" "${target}"
    log "Logo sostituito: ${target}"
    FOUND=1
done < <(find "${UI_ROOT}" -name 'ots-logo-*.png' -print0 2>/dev/null)

[[ ${FOUND} -eq 1 ]] || warn "Nessun ots-logo-*.png trovato sotto ${UI_ROOT}: build della UI diversa dal previsto?"

# ------------------------- Favicon (opzionali) -------------------------
# Se metti questi file in ots/branding/, vengono copiati con lo stesso nome nella root della UI.
FAVICONS=(favicon.ico favicon-16x16.png favicon-32x32.png apple-touch-icon.png
          android-chrome-192x192.png android-chrome-512x512.png mstile-150x150.png safari-pinned-tab.svg)
for name in "${FAVICONS[@]}"; do
    if [[ -f "${BRANDING_DIR}/${name}" ]]; then
        cp "${BRANDING_DIR}/${name}" "${UI_ROOT}/${name}"
        log "Favicon sostituita: ${name}"
    fi
done

# ------------------------- Guardia di login -------------------------
# La UI upstream apre dashboard e pagine interne anche senza login: auth-guard.js
# rimanda a /login chi non ha sessione (tranne /login, /reset, /404).
# Il ?v=<hash> cambia a ogni versione dello script e scavalca la cache del browser.
GUARD="${BRANDING_DIR}/auth-guard.js"
INDEX="${UI_ROOT}/index.html"
if [[ -f "${GUARD}" && -f "${INDEX}" ]]; then
    cp "${GUARD}" "${UI_ROOT}/auth-guard.js"
    GUARD_VER="$(sha256sum "${GUARD}" | cut -c1-12)"
    GUARD_TAG="<script src=\"/auth-guard.js?v=${GUARD_VER}\"></script>"
    # Toglie un'eventuale versione precedente e mette la nuova subito dopo <head>,
    # prima del bundle della UI (script classico = eseguito prima dei module).
    sed -i -E '/<script src="\/auth-guard\.js[^"]*"><\/script>/d' "${INDEX}"
    sed -i "0,/<head>/s|<head>|<head>\n    ${GUARD_TAG}|" "${INDEX}"
    if grep -qF "${GUARD_TAG}" "${INDEX}"; then
        log "Guardia di login attiva (auth-guard.js ${GUARD_VER})"
    else
        warn "Guardia di login NON iniettata: index.html senza <head>? Controlla ${INDEX}"
    fi
elif [[ ! -f "${GUARD}" ]]; then
    warn "auth-guard.js non trovato in ${BRANDING_DIR}: le pagine della UI restano apribili senza login."
fi

log "Fatto. Se nel browser vedi ancora il vecchio logo, forza il refresh (Ctrl+F5):"
log "il nome file non cambia, quindi la cache puo' tenere la versione precedente."
