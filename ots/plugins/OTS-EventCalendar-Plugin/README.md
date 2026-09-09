# OTS-EventCalendar-Plugin

Plugin per **OpenTAKServer** (>= 1.7) che aggiunge al server un **calendario eventi** con
gestione presenze, punteggi e gradi militari. Usa il login e gli utenti di OpenTAKServer
(la registrazione utente resta quella standard di OTS).

## Funzionalità

### Calendario eventi
- Ogni evento ha: **sede di gioco** (dall'anagrafica campi), **data/ora di inizio**,
  **data/ora di fine**, titolo e **descrizione**.
- Gli **amministratori** creano/modificano/eliminano gli eventi dalla UI.
- Import eventi da **Google Calendar** (URL iCal/ICS, deduplicato per UID: si può
  rilanciare per sincronizzare) o da **file CSV**
  (`title,description,field,start,end`, date ISO, separatore `,` o `;`).

### Presenze
- Ogni utente loggato dichiara per ogni evento: **Presente / Non presente / In dubbio**;
  il default è **Non configurato**.
- Gli operatori admin, sul campo, **confermano le presenze** effettive dalla scheda
  "Presenze": la conferma assegna i punti (revocabile, i punti vengono tolti).

### Punteggi e gradi
- Ogni presenza confermata incrementa lo **score** dell'utente
  (`OTS_EVENTCALENDAR_POINTS_PER_PRESENCE`, default 10, configurabile in `config.yml`).
- Sezione amministrativa **Gradi e badge**: gradi aderenti alla gerarchia dell'Esercito
  Italiano (seed automatico al primo avvio, modificabili), per ognuno si configura il
  **badge (immagine)** e il **punteggio minimo** per ottenerlo.
- Il grado di un operatore è il più alto con soglia ≤ punti; dalla **Classifica** l'admin
  può anche **assegnare manualmente un grado** a un utente (override) o correggere i punti.

### Anagrafica campi da gioco
- CRUD dei campi (nome, indirizzo, coordinate, note, attivo/disattivo), riservato agli admin.

## Installazione

Sul server, da root (lo stesso script fa anche l'update alle versioni successive):

```bash
git clone https://github.com/novatia/atakcivsaas.git
cd atakcivsaas/ots/scripts
chmod +x install-eventcalendar-plugin.sh
./install-eventcalendar-plugin.sh          # installa/aggiorna + restart + verifica
./install-eventcalendar-plugin.sh --check  # mostra solo la versione installata
./install-eventcalendar-plugin.sh --pull   # git pull del repo e poi installa
```

Equivalente manuale, come utente `ots`:

```bash
cd atakcivsaas/ots/plugins/OTS-EventCalendar-Plugin
sudo -u ots /home/ots/.opentakserver_venv/bin/pip install --upgrade .
sudo systemctl restart opentakserver
```

In alternativa, build del pacchetto con Poetry (versione presa dal tag git) e install del wheel
dalla pagina Plugins della web UI di OTS:

```bash
pipx install poetry
poetry self add "poetry-dynamic-versioning[plugin]"
poetry build
```

Al primo avvio il plugin crea le proprie tabelle (`ec_*`) nel database di OTS e fa il seed
dei gradi di default.

## Accesso alla UI

- Admin: web UI di OTS → **Plugins** → OTS-EventCalendar-Plugin.
- Tutti gli utenti loggati: `https://<server>/api/plugins/ots_eventcalendar_plugin/ui`
  (comodo da linkare nel menu o come location nginx dedicata, es. `/calendario`).

## Configurazione (`~/ots/config.yml`)

| Chiave | Default | Descrizione |
|---|---|---|
| `OTS_EVENTCALENDAR_PLUGIN_ENABLED` | `true` | Abilita il plugin |
| `OTS_EVENTCALENDAR_POINTS_PER_PRESENCE` | `10` | Punti per presenza confermata |

## API (prefisso `/api/plugins/ots_eventcalendar_plugin`)

| Metodo e rotta | Ruolo | Descrizione |
|---|---|---|
| `GET /me` | utente | Profilo: punti, grado, ruoli |
| `GET /events` · `POST /events` | utente · admin | Lista eventi (con propria RSVP e conteggi) · creazione |
| `PUT/DELETE /events/<id>` | admin | Modifica / eliminazione evento |
| `POST /events/<id>/rsvp` | utente | `{"status": "present\|absent\|maybe\|not_configured"}` |
| `GET/POST /events/<id>/attendance` | admin | Elenco presenze · conferma `{"user_id", "confirmed"}` |
| `GET /fields` · `POST/PUT/DELETE /fields…` | utente · admin | Anagrafica campi da gioco |
| `POST /import/ics` | admin | `{"url": "…", "default_field_id": n}` o file `.ics` |
| `POST /import/csv` | admin | multipart `file` + `default_field_id` |
| `GET /ranks` · `POST/PUT/DELETE /ranks…` | utente · admin | Gradi |
| `POST /ranks/<id>/badge` | admin | Upload immagine badge (multipart `file`) |
| `GET /badges/<file>` | utente | Immagine badge |
| `GET /leaderboard` | utente | Classifica con grado risolto |
| `POST /users/<id>/rank` | admin | Override manuale del grado (`rank_id` o `null`) |
| `POST /users/<id>/score` | admin | Correzione manuale del punteggio |

I badge caricati vengono salvati in
`~/ots/plugins/ots_eventcalendar_plugin/badges/` (inclusi nel backup di `update-ots.sh`).

## Esempio CSV

Vedi [`examples/eventi-esempio.csv`](examples/eventi-esempio.csv).

## Licenza

GPL-3.0-or-later (requisito per i plugin OpenTAKServer distribuiti pubblicamente).
