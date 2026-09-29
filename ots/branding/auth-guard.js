/*
 * auth-guard.js — Rimanda al login chi apre la web UI di OpenTAKServer senza sessione.
 *
 * La UI upstream (brian7704/OpenTAKServer-UI) mostra comunque dashboard e pagine
 * interne a chi non è loggato: i dati restano protetti dalle API, ma la pagina si
 * apre vuota. Questo script, iniettato in index.html da apply-branding.sh:
 *
 *  1. al caricamento di una pagina non pubblica nasconde la UI, chiede /api/me e,
 *     se non c'è sessione va a /login (errori di rete o server giù: mostra la
 *     pagina com'è, per non chiudere fuori nessuno per un guasto);
 *  2. durante la navigazione, se una chiamata /api/ risulta non autenticata (sessione scaduta
 *     o logout da un'altra scheda) va a /login.
 *
 * Pagine pubbliche: quelle che la UI stessa definisce fuori dal layout
 * (/login, /reset, /404). La UI dei plugin è servita da /api/plugins/... e non
 * carica questo file.
 */
(function () {
    "use strict";

    var PUBLIC_PATHS = ["/login", "/reset", "/404"];
    // Chiamate che rispondono 401 per motivi legittimi (credenziali errate, 2FA...)
    var IGNORED_API = ["/api/login", "/api/ldap_login", "/api/logout", "/api/reset", "/api/tf-", "/api/register"];

    function isPublic(path) {
        path = (path || "/").replace(/\/+$/, "") || "/";
        for (var i = 0; i < PUBLIC_PATHS.length; i++) {
            if (path === PUBLIC_PATHS[i] || path.indexOf(PUBLIC_PATHS[i] + "/") === 0) return true;
        }
        return false;
    }

    function toLogin() {
        if (isPublic(location.pathname)) return;
        try { localStorage.removeItem("loggedIn"); } catch (e) { /* storage bloccato */ }
        location.replace("/login");
    }

    function apiPath(url) {
        try { return new URL(url, location.href).pathname; } catch (e) { return ""; }
    }

    function watched(url) {
        var p = apiPath(url);
        if (p.indexOf("/api/") !== 0) return false;
        for (var i = 0; i < IGNORED_API.length; i++) {
            if (p.indexOf(IGNORED_API[i]) === 0) return false;
        }
        return true;
    }

    // Non autenticato: 401 se la richiesta è JSON, altrimenti Flask-Security
    // risponde 302 verso /api/login e XHR/fetch seguono il redirect in silenzio.
    function unauthenticated(status, finalUrl, url) {
        if (status === 401) return true;
        return !!finalUrl && apiPath(finalUrl) === "/api/login" && apiPath(url) !== "/api/login";
    }

    // --- 1. Controllo al caricamento -------------------------------------------
    if (!isPublic(location.pathname)) {
        var root = document.documentElement;
        root.style.visibility = "hidden";
        var show = function () { root.style.visibility = ""; };
        // Rete lenta o bloccata: dopo 5 s la pagina compare comunque.
        var failsafe = setTimeout(show, 5000);
        var xhr = new XMLHttpRequest();
        xhr.open("GET", "/api/me", true);
        // Con JSON Flask-Security risponde 401 invece del redirect a /api/login.
        xhr.setRequestHeader("Accept", "application/json");
        xhr.setRequestHeader("Content-Type", "application/json");
        xhr.onload = function () {
            clearTimeout(failsafe);
            if (unauthenticated(xhr.status, xhr.responseURL, "/api/me")) toLogin(); else show();
        };
        xhr.onerror = xhr.ontimeout = function () { clearTimeout(failsafe); show(); };
        xhr.send();
    }

    // --- 2. Sessione scaduta durante la navigazione ----------------------------
    // La UI usa axios (XMLHttpRequest); intercettiamo anche fetch per sicurezza.
    var origOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method, url) {
        if (watched(url)) {
            this.addEventListener("load", function () {
                if (unauthenticated(this.status, this.responseURL, url)) toLogin();
            });
        }
        return origOpen.apply(this, arguments);
    };

    if (window.fetch) {
        var origFetch = window.fetch;
        window.fetch = function (input) {
            var url = typeof input === "string" ? input : (input && input.url) || "";
            return origFetch.apply(this, arguments).then(function (resp) {
                if (watched(url) && unauthenticated(resp.status, resp.url, url)) toLogin();
                return resp;
            });
        };
    }
})();
