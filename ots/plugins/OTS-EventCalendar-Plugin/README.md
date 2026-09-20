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

### Anagrafica giocatori
- Presenze, classifica, punti e gradi fanno riferimento all'**anagrafica giocatori**
  (tab "Giocatori", admin), parallela agli account OTS: si registrano i giocatori con
  nome/cognome/callsign anche se non hanno un account.
- Un giocatore può essere **associato a un account OTS** (opzionale, un account per
  giocatore): serve solo a permettergli di dichiarare da solo la propria presenza dal
  link dell'evento.

### Presenze
- Il calendario completo è visibile **solo agli admin**: gli operatori ricevono il link
  del singolo evento (es. sul gruppo WhatsApp) e vedono una **pagina dedicata a quell'evento**.
- Lì il giocatore con account associato dichiara: **Presente / Non presente / In dubbio**;
  il default è **Non configurato**.
- Nella tab Presenze l'admin vede **tutta la squadra** (giocatori attivi) e può confermare
  chiunque, anche chi non ha dichiarato nulla; il bottone **"Tutta la squadra presente"**
  conferma tutti in un colpo (e c'è l'annulla-tutto simmetrico).
- Chiunque sia loggato può registrare sull'evento un **ospite "in prova"** con nome e
  cognome (senza account): compare nella lista presenze dell'admin, che può confermarne
  la presenza (senza punti). L'ospite può essere rimosso da chi l'ha registrato o da un admin.
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

### Replay giocata (admin)
- Bottone **"▶ Giocata"** su ogni evento: apre un **player su mappa** (Leaflet + OpenStreetMap)
  che rigioca i movimenti degli EUD collegati al server durante l'evento, con velocità
  regolabile **1x / 3x / 5x / 10x / 100x**, slider temporale, scia del percorso e
  legenda con on/off per singolo EUD.
- Le tracce vengono lette dalle tabelle `points`/`euds` di OpenTAKServer (popolate dal
  processo `cot_parser`): nessuna tabella aggiuntiva, funziona retroattivamente su tutti
  i dati già registrati. Gli orari degli eventi sono interpretati nel fuso
  `OTS_EVENTCALENDAR_TIMEZONE` (default `Europe/Rome`), i punti CoT sono in UTC.
- Richiede la connessione internet nel browser (libreria mappa da CDN e tile OSM) e una
  retention adeguata sul job `delete_old_data` di OTS, altrimenti le giocate vecchie
  spariscono.

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

In alternativa, build del wheel con Poetry (`poetry build`) e install dalla pagina Plugins
della web UI di OTS. La versione del pacchetto è statica in `pyproject.toml`
(tenerla allineata a `ots_eventcalendar_plugin/__init__.py` quando si rilascia una modifica).

Al primo avvio il plugin crea le proprie tabelle (`ec_*`) nel database di OTS e fa il seed
dei gradi di default.

## Accesso alla UI

- Admin: web UI di OTS → **Plugins** → OTS-EventCalendar-Plugin.
- Tutti gli utenti loggati: `https://<server>/api/plugins/ots_eventcalendar_plugin/ui`
  (comodo da linkare nel menu o come location nginx dedicata, es. `/calendario`).

### Link diretto a un evento (WhatsApp)

Ogni evento ha il bottone **📋 Copia link**, che mette negli appunti un messaggio pronto da
incollare (titolo, campo, orari) con il link diretto `…/ui#event-<id>`. Chi apre il link
arriva sulla pagina con l'evento evidenziato; se non è loggato vede l'invito al login di
OpenTAKServer (la pagina è pubblica, i dati restano protetti dalle API).

## Configurazione (`~/ots/config.yml`)

| Chiave | Default | Descrizione |
|---|---|---|
| `OTS_EVENTCALENDAR_PLUGIN_ENABLED` | `true` | Abilita il plugin |
| `OTS_EVENTCALENDAR_POINTS_PER_PRESENCE` | `10` | Punti per presenza confermata |
| `OTS_EVENTCALENDAR_TIMEZONE` | `Europe/Rome` | Fuso orario degli orari del calendario (per il replay: i punti CoT sono in UTC) |

## API (prefisso `/api/plugins/ots_eventcalendar_plugin`)

| Metodo e rotta | Ruolo | Descrizione |
|---|---|---|
| `GET /me` | utente | Profilo: punti, grado, ruoli |
| `GET /events` · `POST /events` | utente · admin | Lista eventi (con propria RSVP e conteggi) · creazione |
| `PUT/DELETE /events/<id>` | admin | Modifica / eliminazione evento |
| `POST /events/<id>/rsvp` | utente | `{"status": "present\|absent\|maybe\|not_configured"}` |
| `GET/POST /events/<id>/attendance` | admin | Elenco presenze · conferma `{"user_id", "confirmed"}` |
| `GET /events/<id>/replay` | admin | Tracce GPS degli EUD nella finestra dell'evento (`?step=N` = max un punto ogni N s per EUD, default 5) |
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
