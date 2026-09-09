#!/usr/bin/env bash
#
# apply-branding.sh — Applica il logo personalizzato alla web UI di OpenTAKServer.
#
# Sostituisce il logo OTS (assets/ots-logo-<hash>.png nella root nginx) con
# ots/branding/logo.png e, se presenti in ots/branding/, anche le favicon.
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

# Trova la root della UI dal config nginx (stessa logica di update-ots.sh)
UI_ROOT="${UI_ROOT:-$(grep -rhoP '^\s*root\s+\K[^;]+' /etc/nginx/sites-enabled/ 2>/dev/null | sort -u | head -1 || true)}"
[[ -n "${UI_ROOT}" && -d "${UI_ROOT}" ]] || die "Root della UI non trovata nei config nginx. Imposta UI_ROOT e riprova."
log "UI servita da nginx in: ${UI_ROOT}"

# ------------------------- Logo principale -------------------------
# Vite emette il logo come assets/ots-logo-<hash>.png: sovrascriviamo il file
# mantenendo il nome, così i riferimenti nel JS continuano a funzionare.
FOUND=0
while IFS= read -r -d '' target; do
    cp "${BRANDING_DIR}/logo.png" "${target}"
    log "Logo sostituito: ${target}"
    FOUND=1
done < <(find "${UI_ROOT}/assets" -maxdepth 1 -name 'ots-logo-*.png' -print0 2>/dev/null)

[[ ${FOUND} -eq 1 ]] || warn "Nessun assets/ots-logo-*.png trovato in ${UI_ROOT}: build della UI diversa dal previsto?"

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

log "Fatto. Se nel browser vedi ancora il vecchio logo, forza il refresh (Ctrl+F5):"
log "il nome file non cambia, quindi la cache puo' tenere la versione precedente."
