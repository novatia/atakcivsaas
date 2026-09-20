# OTS-MilSim-Companion-Plugin

**MilSim Soft-Air Companion** per **OpenTAKServer** (>= 1.7): un unico plugin che
unisce il calendario eventi con presenze/punteggi/gradi (ex OTS-EventCalendar-Plugin,
fino alla 2.3.0), le **modalità di gioco** con template di missione e Play
(ex OTS-GameMode-Plugin) e l'integrazione **SkyFi + missioni Data Sync + stato
mappe PCN** (ex OTS-SkyFi-Plugin). Usa il login e gli utenti di OpenTAKServer.

> **Migrazione**: le tabelle DB (`ec_*`, `gm_*`) e le chiavi di `config.yml`
> (`OTS_EVENTCALENDAR_*`, `OTS_SKYFI_PLUGIN_API_KEY`) sono invariate: dati e
> configurazione sopravvivono. Lo script di install disinstalla da solo i
> vecchi pacchetti (EventCalendar, GameMode, SkyFi). Cambia l'URL della UI
> (vedi sotto): i link `#event-<id>` condivisi in precedenza vanno rigenerati.

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

### Modalità di gioco: anagrafica

Definita in [`ots_milsim_companion_plugin/game_modes.py`](ots_milsim_companion_plugin/game_modes.py):
ogni modalità dichiara i tipi di marker e di area richiesti (con min/max).

| Modalità | Marker richiesti | Aree |
|---|---|---|
| 🚩 **Capture the Flag** | Spawn A, Spawn B, 1–2 Bandiere | Perimetro campo (opz.) |
| 💣 **Bomb Defusal** | Spawn A, Spawn B, Bomb site A, Bomb site B | **Area valida ordigni**, perimetro (opz.) |
| ⚔️ **Team Deathmatch** | Spawn A, Spawn B | Perimetro campo (opz.) |
| 🏰 **Dominio** | Spawn A, Spawn B, 2–8 Punti di dominio | **Un'area di validità per ogni punto** (verificato al Play), perimetro (opz.) |

In Dominio la meccanica di cattura dei punti da parte delle squadre non è
ancora implementata: per ora il Play pusha punti e aree, la presa è gestita
a voce/arbitro (vedi Roadmap, orchestratore in campo).

Resa su ATAK/WinTAK (simbologia nativa dove i colori coincidono):

| Marker | Colore | Su ATAK | CoT |
|---|---|---|---|
| Spawn Team A | rosso | rombo (ostile) | `a-h-G` |
| Spawn Team B | blu | quadrato (amico) | `a-f-G` |
| Bandiera | giallo | simbolo sconosciuto | `a-u-G` |
| Bomb site A/B | viola | spot marker colorato | `b-m-p-s-m` |
| Punto di dominio | arancione | spot marker colorato | `b-m-p-s-m` |
| Aree | viola / arancione / verde | poligono colorato | `u-d-f` |

### Template di missione ed editor su mappa

Tab **Template missioni**: titolo, descrizione, modalità, durata, data package da
annunciare al Play, e l'**editor su mappa** (Leaflet da CDN, layer OSM +
satellite Esri — serve internet nel browser): si clicca il tipo di marker
nella palette e poi sulla mappa; i marker si trascinano, le aree si disegnano
a vertici. La palette mostra i conteggi rispetto ai limiti della modalità e il
template si può salvare anche incompleto (il Play però richiede i minimi).

Si è scelto l'editing da browser invece dei data package con nomi standard:
meno passaggi e meno errori per l'utente finale, nessun round-trip
ATAK→server, validazione immediata contro l'anagrafica della modalità.

### Play e partite

**▶ Play** su un template completo crea la partita (tab **Partite**):

1. marker e aree vengono pushati come CoT a **tutti gli EUD collegati**, con
   `stale` = fine partita (+2'): allo scadere della durata **spariscono da
   soli** dagli ATAK;
2. ogni data package assegnato viene annunciato con un CoT `b-f-t-r`
   (fileshare): gli EUD ricevono la proposta di download dal server;
3. la partenza (titolo, modalità, durata) viene annunciata nella **chat
   generale** (All Chat Rooms).

Nella tab Partite: countdown, **📡 Ripubblica** (stessi UID, per gli EUD
entrati a partita in corso) e **⏹ Termina** (CoT `t-x-d-d` di cancellazione +
annuncio in chat). Lo storico resta nel DB (`gm_matches`, con lo snapshot del
template al momento del Play).

Il broadcast dei CoT usa lo stesso meccanismo dell'endpoint `DELETE /api/markers`
di OTS (exchange RabbitMQ `cot_parser` + `firehose`).

### SkyFi: ordini e asset satellitari (tab SkyFi)

Ereditato dal fork OTS-SkyFi-Plugin (upstream brian7704, che non distribuisce la UI):

- lista ordini SkyFi **paginata** con ricerca, **anteprime**, stato delivery e costo;
- **download dei deliverable** (image / payload / COG / view-ready) via **proxy
  backend**: l'API key SkyFi non arriva mai al browser;
- **📦 Data package ATAK**: crea un data package OTS con i tile WMTS dell'ordine
  (layer SkyFi + Google Hybrid), scaricabile dagli EUD;
- **🎯 Missione**: scarica il deliverable sul server e lo aggiunge ai contenuti di
  una missione **Data Sync**, replicando il flusso di `/Marti/sync/upload` +
  `PUT /Marti/api/missions/<name>/contents` (dedup per sha256, MissionChange
  ADD_CONTENT, CoT `t-x-m-c` sull'exchange `missions`): gli EUD iscritti vengono
  notificati e scaricano il file;
- configurazione **API key** nel tab stesso (si genera su app.skyfi.com → Profile
  → API Key, account SkyFi Pro; salvata in `config.yml`).

### Missioni Data Sync (tab Missioni)

La web UI di OTS non mostra i contenuti (dataset) delle missioni Data Sync:
questo tab li elenca per missione — con anteprima delle immagini, dimensione,
autore e data — e permette **download dal browser** e **rimozione** (replica di
`DELETE /Marti/api/missions/<name>/contents`: si toglie solo il link
contenuto↔missione con MissionChange REMOVE_CONTENT e notifica agli EUD, il
file resta su disco per lo storico).

In cima al tab c'è lo **stato mappe PCN** (Geoportale Italia): il pulsante
«Verifica adesso» chiede una vera tile `GetMap` (WMS 1.1.1, EPSG:3857) a
ciascuno dei 5 servizi usati nei data package del gruppo (IGM 25/100/250k,
ortofoto 2006/2012), perché il catalogo del PCN risponde anche quando la
generazione delle immagini è rotta: quando il servizio è giù, su ATAK/WinTAK
le mappe restano verdi/vuote senza alcun errore, e il semaforo permette di
distinguere subito il guasto del Ministero da un problema nostro.
Nessun automatismo: il check parte solo dal pulsante.

## Installazione

Sul server, da root (lo stesso script fa anche l'update alle versioni successive
e disinstalla i vecchi OTS-EventCalendar-Plugin / OTS-GameMode-Plugin):

```bash
git clone https://github.com/novatia/atakcivsaas.git
cd atakcivsaas/ots/scripts
chmod +x install-milsim-companion-plugin.sh
./install-milsim-companion-plugin.sh          # installa/aggiorna + restart + verifica
./install-milsim-companion-plugin.sh --check  # mostra solo la versione installata
./install-milsim-companion-plugin.sh --pull   # git pull del repo e poi installa
```

Equivalente manuale, come utente `ots`:

```bash
cd atakcivsaas/ots/plugins/OTS-MilSim-Companion-Plugin
sudo -u ots /home/ots/.opentakserver_venv/bin/pip uninstall --yes OTS-EventCalendar-Plugin OTS-GameMode-Plugin
sudo -u ots /home/ots/.opentakserver_venv/bin/pip install --upgrade .
sudo systemctl restart opentakserver
```

Al primo avvio il plugin crea le proprie tabelle (`ec_*` e `gm_*`) nel database di OTS,
fa il seed dei gradi di default e migra la cartella badge dal vecchio nome del plugin.

## Accesso alla UI

- Admin: web UI di OTS → **Plugins** → OTS-MilSim-Companion-Plugin.
- Tutti gli utenti loggati: `https://<server>/api/plugins/ots_milsim_companion_plugin/ui`
  (comodo da linkare nel menu o come location nginx dedicata, es. `/companion`).

### Link diretto a un evento (WhatsApp)

Ogni evento ha il bottone **📋 Copia link**, che mette negli appunti un messaggio pronto da
incollare (titolo, campo, orari) con il link diretto `…/ui#event-<id>`. Chi apre il link
arriva sulla pagina con l'evento evidenziato; se non è loggato vede l'invito al login di
OpenTAKServer (la pagina è pubblica, i dati restano protetti dalle API).

## Configurazione (`~/ots/config.yml`)

Il prefisso `OTS_EVENTCALENDAR_` è storico (il plugin nasce come calendario): le chiavi
restano invariate per compatibilità con i config esistenti.

| Chiave | Default | Descrizione |
|---|---|---|
| `OTS_EVENTCALENDAR_PLUGIN_ENABLED` | `true` | Abilita il plugin |
| `OTS_EVENTCALENDAR_POINTS_PER_PRESENCE` | `10` | Punti per presenza confermata |
| `OTS_EVENTCALENDAR_TIMEZONE` | `Europe/Rome` | Fuso orario degli orari del calendario (per il replay: i punti CoT sono in UTC) |
| `OTS_EVENTCALENDAR_GM_CALLSIGN` | `Game Master` | Firma di marker, chat e fileshare al Play |
| `OTS_EVENTCALENDAR_GM_SERVER_ADDRESS` | `""` | Hostname/IP per i download dei data package (vuoto = host della web UI) |
| `OTS_SKYFI_PLUGIN_API_KEY` | `""` | API key SkyFi (stessa chiave del vecchio OTS-SkyFi-Plugin) |

## API (prefisso `/api/plugins/ots_milsim_companion_plugin`)

| Metodo e rotta | Ruolo | Descrizione |
|---|---|---|
| `GET /me` | utente | Profilo: punti, grado, ruoli |
| `GET /events` · `POST /events` | admin | Lista eventi (con propria RSVP e conteggi) · creazione |
| `PUT/DELETE /events/<id>` | admin | Modifica / eliminazione evento |
| `POST /events/<id>/rsvp` | utente | `{"status": "present\|absent\|maybe\|not_configured"}` |
| `GET/POST /events/<id>/attendance` | admin | Elenco presenze · conferma `{"player_id", "confirmed"}` |
| `GET /events/<id>/replay` | admin | Tracce GPS degli EUD nella finestra dell'evento (`?step=N` = max un punto ogni N s per EUD, default 5) |
| `GET /fields` · `POST/PUT/DELETE /fields…` | utente · admin | Anagrafica campi da gioco |
| `POST /import/ics` | admin | `{"url": "…", "default_field_id": n}` o file `.ics` |
| `POST /import/csv` | admin | multipart `file` + `default_field_id` |
| `GET /ranks` · `POST/PUT/DELETE /ranks…` | utente · admin | Gradi |
| `POST /ranks/<id>/badge` | admin | Upload immagine badge (multipart `file`) |
| `GET /badges/<file>` | utente | Immagine badge |
| `GET /leaderboard` | utente | Classifica con grado risolto |
| `POST /players/<id>/rank` | admin | Override manuale del grado (`rank_id` o `null`) |
| `POST /players/<id>/score` | admin | Correzione manuale del punteggio |
| `GET /modes` | admin | Anagrafica modalità/marker/aree |
| `GET/POST /templates` · `PUT/DELETE /templates/<id>` | admin | CRUD template di missione |
| `POST /templates/<id>/duplicate` | admin | Copia di un template |
| `GET /datapackages` | admin | Data package OTS disponibili |
| `POST /templates/<id>/play` | admin | Crea la partita e pusha tutto agli EUD |
| `GET /matches` | admin | Partite (in corso e storico) |
| `POST /matches/<id>/republish` | admin | Ripubblica marker/aree (stessi UID) |
| `POST /matches/<id>/end` | admin | Termina: cancella i marker dagli EUD |
| `GET /orders` · `GET /orders/<uid>` | admin | Ordini SkyFi (paginati, `?search=`) · dettaglio |
| `GET /orders/<uid>/image` | admin | Anteprima ordine (data-URI, via proxy) |
| `GET /orders/<uid>/download/<tipo>` | admin | Proxy del deliverable (image/payload/cog/view-ready) |
| `POST /orders/<uid>/data_package` | admin | Data package ATAK con i tile WMTS dell'ordine |
| `POST /orders/<uid>/mission` | admin | `{"mission", "deliverable_type"}`: asset nella missione Data Sync |
| `GET /missions` · `GET /missions/<nome>/contents` | admin | Missioni Data Sync · contenuti condivisi |
| `GET /missions/<nome>/contents/<hash>/download` · `/preview` | admin | Download / anteprima immagine di un contenuto |
| `DELETE /missions/<nome>/contents/<hash>` | admin | Rimuove il contenuto dalla missione (notifica EUD) |
| `GET /pcn/status` | admin | Semaforo WMS PCN (una GetMap di prova per servizio) |

I badge caricati vengono salvati in
`~/ots/plugins/ots_milsim_companion_plugin/badges/` (inclusi nel backup di `update-ots.sh`).

## Roadmap

- **Orchestratore in campo** (Raspberry + LoRaWAN): le partite sono già
  interrogabili via API (`GET /matches`), un dispositivo in campo potrà
  leggere lo stato della partita e comparire come marker/entità attiva sulla
  mappa (es. l'ordigno stesso che trasmette il proprio stato).
- **Cattura dei punti di dominio**: registrare quale squadra controlla ogni
  punto (dall'orchestratore in campo o manualmente dal Game Master) e
  aggiornarne il colore sugli EUD.
- Punteggi di fine partita agganciati all'anagrafica giocatori.

## Esempio CSV

Vedi [`examples/eventi-esempio.csv`](examples/eventi-esempio.csv).

## Licenza

GPL-3.0-or-later (requisito per i plugin OpenTAKServer distribuiti pubblicamente).
