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
| 🚩 **Capture the Flag** | Spawn A, Spawn B, 1–8 Bandiere (rinominabili) | Perimetro campo (opz.) |
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

Tab **Template missioni**: titolo, descrizione, modalità, durata, **campo da
gioco** opzionale (dall'anagrafica: centra la mappa dell'editor sul campo, con
📍, così non si cerca ogni volta), data package da annunciare al Play, e
l'**editor su mappa** (Leaflet da CDN, layer OSM +
satellite Esri — serve internet nel browser): si clicca il tipo di marker
nella palette e poi sulla mappa; i marker si trascinano, le aree si disegnano
a vertici. La palette mostra i conteggi rispetto ai limiti della modalità e il
template si può salvare anche incompleto (il Play però richiede i minimi).
Dalla card del template, **👁 Anteprima** mostra su mappa marker, aree e campo
senza aprire l'editor. Con «🎯 Crea missione» attivo, i data package del
template non vengono fileshareati al Play ma finiscono tra i **contenuti della
missione Data Sync**: i giocatori li ricevono iscrivendosi (🎯 Assegna missione).

Si è scelto l'editing da browser invece dei data package con nomi standard:
meno passaggi e meno errori per l'utente finale, nessun round-trip
ATAK→server, validazione immediata contro l'anagrafica della modalità.

### Play, luce verde e match engine

Il ciclo di vita di una partita ha due passi, gestiti dal server:

1. **▶ Play** su un template completo **prepara la partita** (stato *pronta*,
   link al template in `template_id`): marker e aree vengono pushati come CoT
   ai destinatari (stale provvisorio di 24 h), ogni data package assegnato
   viene annunciato con un CoT `b-f-t-r` (fileshare) e in **chat generale**
   esce l'invito a raggiungere gli spawn. Il timer NON parte. Se il template
   ha il flag **🎯 Crea missione**, nasce anche una **missione Data Sync**
   collegata alla partita (nome `<titolo>-<run>`, salvato in
   `gm_matches.mission_name`): l'admin può definirne i dataset dal tab
   Missioni o da ATAK, e con **🎯 Assegna missione** (accanto alla luce verde,
   nel tab **Sessione**) gli EUD dei team ricevono l'invito `t-x-m-i` con
   token a iscriversi.
2. **🚦 Inizia partita** (tab Sessione) dà la **luce verde**: annuncio 🟢 in chat,
   `started_at`/`ends_at` fissati, marker ripubblicati con lo stale vero
   (fine partita +2') e da lì **il tempo lo tiene il server**.

Il **match engine** è un thread con tick da 1 secondo (avviato in `activate()`,
con un lease su DB — `gm_engine_lease` — che garantisce una sola istanza attiva
anche se OTS carica il plugin in più processi): allo scadere di `ends_at`
chiude la partita da solo, cancella i marker dagli EUD (`t-x-d-d`) e annuncia
🏁 l'esito deciso dall'**arbitro tipizzato** della modalità (`engine.py`):

- **CTF / TDM / Dominio**: si chiude solo a tempo (esito sul campo; per
  Dominio il punteggio server con target 100 arriverà con l'orchestratore);
- **Bomb Defusal**: può finire **prima del tempo** — gli eventi 💣 Piazzata /
  ✂️ Disinnescata (vincono i difensori) / 💥 Esplosa (vincono gli attaccanti)
  sono bottoni del GM nella tab Sessione oggi, e la stessa API
  (`POST /matches/<id>/event`) domani la chiamerà l'orchestratore in campo;
  a tempo scaduto senza esplosione vincono i difensori.

Nella tab Sessione: countdown live, eventi di partita, **📡 Ripubblica** (stessi
UID, per gli EUD entrati dopo), **⏹ Termina / 🚫 Annulla** manuali; nello
storico esito (vincitore + motivo: tempo/obiettivo/manuale) e **▶ Replay
partita**: il player su mappa filtrato esattamente sulla finestra
`started_at → ended_at` tenuta dal server (già in UTC come i punti CoT).

### Team e destinatari dei CoT

**Composizione delle squadre (dalla 3.13.1).** La tab si chiama *Mappatura Team* e
oltre a scegliere Team A / Team B / osservatori permette di **aggiungere e togliere
utenti dai gruppi** senza passare dalla pagina Groups di OpenTAKServer.

**Assegnare a una squadra è automatico.** Mettendo qualcuno in Team A o Team B
il server applica da solo la regola, in una sola operazione:

1. lo **toglie dall'altra squadra** — si sta in una squadra sola;
2. gli dà **IN + OUT** sulla squadra scelta: riceve i compagni ed è visto da loro;
3. gli dà **solo IN** sul gruppo osservatori: pubblica agli arbitri **senza
   riceverli**.

Il punto 3 è quello che tiene separate le squadre. Se i giocatori avessero anche
`OUT` sul gruppo osservatori, Alpha riceverebbe tutto quello che pubblica Bravo
e le due squadre si vedrebbero fra loro — che è l'errore facile da fare
assegnando i gruppi a mano.

Lo schema che ne esce:

| chi | Alpha | Bravo | Osservatori |
|---|---|---|---|
| giocatore Alpha | IN + OUT | — | IN |
| giocatore Bravo | — | IN + OUT | IN |
| osservatore | — | — | IN + OUT |

Gli arbitri vedono entrambe le squadre e restano invisibili ai giocatori. Un
arbitro può scendere in campo (assegnandolo a una squadra) senza perdere il
ruolo: uscendo dalla squadra torna arbitro completo.

Togliendo qualcuno da una squadra smette anche di pubblicare agli osservatori —
a meno che sia lui stesso un osservatore, nel qual caso il suo `IN` resta.

Le regole stanno in `teams.py` come funzioni pure (`plan_assign`, `plan_remove`)
e sono coperte dai test: `app.py` esegue soltanto il piano che decidono. Sui
gruppi che non hanno un ruolo nella Mappatura Team (visibili con «mostra tutti i
gruppi») resta l'aggiunta semplice IN + OUT, senza regole di partita.

Due cose da sapere, entrambe imposte da come OTS modella i gruppi:

- **L'appartenenza è per utente, non per EUD.** La tabella `groups_users` ha
  `(user_id, group_id, direction)` e la stessa API di OTS lo dichiara: *«this will
  allow all the user's EUDs to subscribe and unsubscribe»*. Aggiungendo un utente
  entrano in squadra **tutti i suoi dispositivi**; per avere due EUD della stessa
  persona in squadre diverse servono due account OTS.
- **Le due direzioni fanno mestieri opposti.** `OUT` = gli EUD dell'utente
  **ricevono** il traffico del gruppo (binding della coda in `EudHandler`), `IN`
  = i CoT di quegli EUD **vengono visti** da quel gruppo (`route_cot`). La UI
  mostra il ruolo di ciascuno: `👁 osserva` per chi riceve, `📡 pubblica` per chi
  è solo visto, e un avviso solo quando in una squadra ne manca una.

**Effetto immediato anche sugli EUD collegati.** OpenTAKServer lega le code al
gruppo *solo quando l'EUD si connette*: con la sua API, aggiungere un utente a un
gruppo non cambia nulla finché non si riavvia ATAK (togliendolo invece sbinda
subito). Il plugin fa il `queue_bind` sull'exchange `groups` nel momento stesso
dell'aggiunta, così il cambio di squadra vale all'istante. Un EUD che non si è mai
collegato non ha ancora una coda: il bind non riesce, la UI lo dice e quel
dispositivo verrà legato da solo al primo avvio.

Con LDAP attivo (`OTS_ENABLE_LDAP`) la modifica è rifiutata, come fa OTS: i gruppi
si gestiscono sul server LDAP.


I destinatari sono i **gruppi ATAK** definiti sul server: la tabella `groups`
di OpenTAKServer, gestita dalla pagina **Groups** della web UI di OTS (lì si
creano i gruppi e si assegnano gli utenti; il plugin li **legge soltanto**).
Gli EUD di un gruppo sono i dispositivi degli utenti assegnati
(`groups_users` → `EUD.user_id`).

Nel tab **Team** si configura la **mappatura dei ruoli** con tre menu — quale
gruppo ATAK è il **Team A**, quale il **Team B** e quale fa da **osservatore
broadcast** — salvata in `config.yml`; sotto, i gruppi con i loro utenti/EUD
per il controllo pre-partita. Il pannello del **Play si apre già
precompilato** con questa mappatura, modificabile per la singola partita:

- **Team A / Team B**: lo spawn di un team lo vede **solo quel gruppo**
  (+ osservatori) — il Team B non sa dove spawna il Team A;
- **bandiere, bomb site, punti di dominio, aree, chat e data package** vanno a
  tutti i gruppi coinvolti (l'audience è dichiarata per tipo di marker
  nell'anagrafica, campo `audience` in `game_modes.py`);
- **osservatori** (broadcast): il gruppo che vede tutto, es. headquarter/admin;
- senza gruppi selezionati la missione va **a tutti** gli EUD (comportamento
  storico).

La consegna mirata pubblica ogni CoT sull'exchange **`dms`** di OTS con
routing key = uid dell'EUD (la coda di ogni EUD è legata lì): i cambi di
appartenenza ai gruppi valgono subito, anche a partita in corso con
«Ripubblica». Il broadcast senza gruppi usa invece `cot_parser` + `firehose`
come l'endpoint `DELETE /api/markers` di OTS. Nota: i CoT mirati non passano
dal `cot_parser`, quindi non vengono persistiti nella tabella markers di OTS
(lo stato della partita vive nel plugin).

### SkyFi: ordini e asset satellitari (tab SkyFi)

Ereditato dal fork OTS-SkyFi-Plugin (upstream brian7704, che non distribuisce la UI):

- lista ordini SkyFi **paginata** con ricerca, **anteprime**, stato delivery e costo;
- **download dei deliverable** (image / payload / COG / view-ready) via **proxy
  backend**: l'API key SkyFi non arriva mai al browser;
- **📦 Data package ATAK**: crea un data package OTS con i tile WMTS dell'ordine
  (layer SkyFi + Google Hybrid), scaricabile dagli EUD. Pesa pochi KB perché
  contiene solo l'XML della sorgente: ATAK scarica i tile da SkyFi in streaming
  (serve rete) e allo zoom massimo sono più sgranati dell'immagine originale;
- **🛰 Mappa offline HD**: il server scarica il GeoTIFF dell'ordine e lo
  converte con GDAL in un **GeoPackage** a tile EPSG:3857 dentro un data
  package: su ATAK la mappa si vede senza rete e alla risoluzione piena.
  - **Sorgente**: di default il **COG**, l'originale senza perdita; il
    view-ready è la stessa immagine già compressa da SkyFi in JPEG (ordine
    26383Z2P, 3 km²: COG 1,2 GB LZW contro view-ready 97 MB JPEG q95, stessi
    15798×22130 pixel da 10,5 cm a terra). Il payload (bande grezze) non si usa.
  - **Formato**: GeoPackage con tile PNG senza perdita (default), JPEG 95 o
    JPEG 85; oppure **GeoTIFF COG senza perdita** (DEFLATE, overview interne,
    niente tile), che ATAK apre come immagine nativa. Motivo: il GeoPackage PNG
    dell'ordine 26383Z2P conteneva lo zoom 20 completo (3715 tile, 264 MB) ma
    ATAK-CIV lo mostrava a blocchi da ~0,9 m (zoom 17). Sullo stesso COG il
    GeoTIFF viene 455 MB in 21 s, pixel identici.
  - Pipeline in `offline_map.py`: riproiezione con `-tr` pari alla risoluzione
    esatta del livello di zoom subito più fine del nativo e `-tap` (un solo
    ricampionamento cubico); se il sorgente è già sulla griglia di quel livello
    — i deliverable SkyFi lo sono: EPSG:3857, pixel dello zoom 20, origine su un
    pixel intero — si usa `near` e con le tile PNG i **pixel sono identici**
    all'originale. 16 bit → 8 bit con stretch media ± 2,5σ per banda, livelli
    inferiori con `gdaladdo`. Sulla card: zoom, cm/pixel a terra, sorgente e
    se i pixel sono stati copiati identici.
  - Zoom massimo sceglibile (ogni livello in meno divide la dimensione per 4);
    limite 2 GB per pacchetto (`data_packages.size` di OTS è un intero a 32
    bit). Gira in background, un job alla volta, con avanzamento sulla card;
    lo stato dei job è in memoria e si perde al riavvio di OTS. Serve
    `gdal-bin` sul server (lo script di install lo installa se manca).
  - Oltre lo zoom nativo ATAK può solo ingrandire i pixel: nessun formato
    aggiunge dettaglio che l'immagine non ha.
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

### Data package: scadenza delle entità e riparazione (tab Data Package)

Il **Data Package tool di WinTAK** scrive le aree disegnate (`u-d-f` e in
genere `u-d-*`) con `stale` = creazione **+ 7 giorni**, i marker (`a-*`)
con + 1 anno. Passata la settimana **ATAK-CIV scarta le aree all'import** perché
scadute: in Data Package tool compaiono righe «Map Item» con «Not Found» in
rosso e in mappa arrivano solo i marker. In più ogni `.cot` di WinTAK inizia
con un BOM UTF-8 e alcuni hanno `<?visible true?>` dopo la dichiarazione XML.
È la correzione manuale del Manuale Operatore (Rev 17, sez. 7), resa un bottone.

Il tab elenca i data package del server e, aprendo ogni zip **in sola lettura**,
mostra per ogni `.cot` uid, tipo, callsign, stale e uno stato:
✅ valida · ⚠️ scade entro 7 giorni · ❌ scaduta (ATAK la scarta) · 🟠 BOM o
processing instruction · ⛔ XML non leggibile. Badge riassuntivo per pacchetto
(«4 scadute su 7»), dettaglio espandibile; mappe, PDF e immagini compaiono
come «file», senza stato. Anche le incoerenze fra manifest e contenuto (voce
del manifest senza file e viceversa, manifest assente) sono segnalate ⛔.

**🔧 Ripara** apre un'anteprima prima/dopo (stale vecchio → nuovo per ogni
entità) con la durata (default `OTS_MILSIM_DP_FIX_STALE_YEARS` = 5 anni) e la
scelta fra *solo scadute e in scadenza* e *tutte*. La conferma crea un
**nuovo** data package, l'originale non viene toccato:

- in ogni `.cot`: `time` e `start` = adesso, `stale` = adesso + N anni sul solo
  tag `<event>` (riscrittura testuale: uid, punti, colori e tutto il resto
  restano byte per byte), BOM e `<?visible true?>` tolti;
- nel manifest: `name` con suffisso `_vN` e `uid` nuovo, così ATAK non riusa il
  pacchetto rotto già importato;
- nuovo zip salvato come `UPLOAD_FOLDER/<sha256>.zip`, nuova riga `data_packages`
  con filename `<originale>_vN.zip` (primo N libero) e gli stessi keywords,
  tool, expiration e flag di installazione dell'originale. `creator_uid` è
  l'ultimo EUD dell'utente corrente (FK verso `euds.uid`, come per SkyFi).

Dopo la riparazione il tab propone di **scaricare** riparato e originale e di
**eliminare l'originale** (doppia conferma). L'eliminazione cancella riga e
file come `DELETE /api/data_packages` di OTS, ma **rifiuta** se il pacchetto è
ancora agganciato a una missione Data Sync o a un template di missione, e non
tocca mai i pacchetti di connessione al server (`*_CONFIG.zip`, certificati).
Un «nascondi» non c'è: in OTS 1.7.13 `/Marti/sync/search` non filtra per
`tool`, quindi marcare il pacchetto non lo toglierebbe dalla lista di ATAK.

Sugli EUD che hanno già importato il pacchetto rotto: eliminarlo da Data
Package tool prima di importare la versione `_vN`.

Zip rifiutati in blocco: non validi o troncati, voci con percorso assoluto o
`..` (zip-slip), voci cifrate o duplicate, oltre 100 MB compressi, 300 MB
decompressi o 2000 voci. Nessun file viene mai estratto su disco. La logica
sta in `datapackage.py` (nessuna dipendenza da Flask o dal DB), coperta dai
test con il pacchetto reale `tests/fixtures/TacticalScoutCodogno.zip`.

Verificato solo il comportamento di **ATAK-CIV**; come iTAK e WinTAK trattino
all'import le entità scadute non è stato provato.

#### Rinomina

**✏️ Rinomina** cambia il nome sul server **e dentro lo zip**: ATAK, una volta
installato il pacchetto, mostra il `name` del manifest, non il nome del server.
Lo zip viene riscritto con il name nuovo e **lo stesso uid** (riscaricandolo,
ATAK sostituisce il pacchetto già installato invece di affiancarne un secondo);
tutto il resto è copiato identico. Cambiando lo zip cambia l'hash: riga
`data_packages`, contenuti delle missioni e template passano al nuovo hash
nella stessa transazione, poi il vecchio file viene cancellato. L'estensione
`.zip` resta sempre; nome già usato → 409. I pacchetti di connessione al
server non si rinominano.

#### File da consultare (PDF, immagini, documenti)

- **📎 Aggiungi file** su un pacchetto: crea una **nuova versione `_vN`** con
  tutto il contenuto dell'originale (voci copiate identiche, a blocchi: va bene
  anche per le mappe offline da GB) più i file scelti; name e uid del manifest
  cambiano, così ATAK non riusa il pacchetto già importato. Stessi keywords,
  tool e flag di installazione dell'originale, che resta sul server finché non
  lo elimini.
- **➕ Nuovo data package**: un pacchetto da zero con solo i file caricati.
- Ogni file finisce in `<uid>/<nome>` ed è elencato nel manifest, come fa ATAK
  (due file con lo stesso nome non si pestano). Nomi ripuliti (solo il
  basename, niente separatori), massimo 50 file per volta e 500 MB per file.
  Su ATAK i file si aprono dal Data Package tool con l'app del telefono.
- Se nginx rifiuta l'upload (413) il limite è `client_max_body_size` del
  server web, non del plugin.

### Meshtastic / TAK tracker (tab Meshtastic e Canali Meshtastic)

Monitor operativo dei tag Meshtastic che arrivano al server e instradamento
**canale Meshtastic → gruppo TAK**, senza alcuna modifica a OpenTAKServer.
L'analisi completa dell'architettura (verificata sul sorgente di OTS 1.7.13 e
del plugin Meshtastic per ATAK) è in
[`docs/meshtastic-architettura.md`](../../../docs/meshtastic-architettura.md).

**Come osserva.** L'API plugin di OTS non ha hook sui CoT: il monitor consuma
una coda esclusiva legata all'exchange **`firehose`** (fanout, dichiarato da
OTS proprio «per i plugin»), quindi vede ogni CoT senza sottrarne a nessuno.
Facoltativamente osserva anche il traffico MQTT grezzo su `amq.topic`.

**Due strade, informazioni molto diverse:**

| | Path A — relay ATAK | Path B — MQTT diretto |
|---|---|---|
| Come arriva | tag → LoRa → nodo su Android → plugin Meshtastic di ATAK → *Relay to Server* | tag → LoRa → gateway → MQTT → RabbitMQ |
| Riconoscimento | `<__meshtastic/>` nel `<detail>` | `<takv platform="Meshtastic">` / `<contact endpoint="MQTT">` |
| uid e callsign del tracker | **sì** (uid stabile, non riscritto con quello dell'EUD) | sì |
| posizione, batteria, team ATAK | **sì** | sì |
| **canale Meshtastic** | **NO** — `<__meshtastic/>` è vuoto | **sì** (sta nella routing key MQTT) |
| node id, RSSI, SNR, hop | **NO** | sì |

> ⚠️ Il canale Meshtastic **non sopravvive** al relay ATAK. Il plugin non lo
> deduce mai (nemmeno dal callsign del tag): lo mostra come `UNKNOWN` e applica
> la politica di fallback scelta dall'amministratore, oppure la dichiarazione
> manuale fatta sul singolo tag.

**Live Monitor** — si aggiorna da solo (polling incrementale ogni 2 s, lo stato
vive in memoria: nessuna query per tag a ogni refresh). In alto le card
*active / known tags, RX negli ultimi 60 s, unknown channel, routing errors* e i
semafori di plugin, RabbitMQ, firehose, observer MQTT e Meshtastic nativo di OTS.
Poi la tabella dei tag (stato LIVE/RECENT/STALE, canale, gruppo OTS, ultimo
contatto, GPS, RSSI, SNR, sorgente) con filtri, e la console eventi con filtri,
pausa, pulisci e auto-scroll (cronologia limitata in memoria, nessuna tabella di
log che cresce all'infinito).

**Dettaglio tag** — anagrafica completa (solo i valori realmente disponibili:
quello che il transport non porta è marcato «non disponibile», mai inventato),
**strade di ricezione** (lo stesso tag ricevuto da più gateway resta un solo
oggetto logico), **traccia della decisione di instradamento** passo per passo
(ricevuto via → EUD sorgente e suoi gruppi → canale → mappatura o fallback →
gruppo finale → esito), **dichiarazione manuale** del canale/gruppo e
**packet inspector** con «View Raw CoT».

**Sanificazione.** Il CoT mostrato nella UI di debug passa da un filtro che
oscura ogni attributo o elemento il cui nome somigli a un segreto (`psk`,
`password`, `token`, `api_key`, `cookie`, `certificate`…): PSK dei canali
Meshtastic, credenziali MQTT e token non possono finire sullo schermo. Il CoT
**instradato** agli EUD resta invece l'originale, intatto.

**Instradamento e isolamento.** La consegna usa `basic_publish(exchange="groups",
routing_key="<gruppo>.OUT")` — esattamente il meccanismo di `cot_parser`: valgono
le normali autorizzazioni di gruppo, chi non è nel gruppo non riceve il tag.
Nessun broadcast a tutti i client.

**Limitazione nota:** il plugin può solo *aggiungere* la consegna al gruppo
mappato, non può togliere quella nativa. OTS instrada comunque il CoT rilanciato
ai gruppi dell'EUD che l'ha rilanciato (decisione presa dentro `route_cot()`, in
un altro processo, senza punti di estensione). Un tag su canale BRAVO rilanciato
da un telefono di ALPHA sarà quindi visto da BRAVO *e* da ALPHA; chi non sta in
nessuno dei due non lo vede. Per evitare la doppia consegna, quando il gruppo
mappato coincide con quello dell'EUD sorgente il plugin non ripubblica.

### Stato servizi (tab Manutenzione)

Il quadro dei servizi da cui dipende tutto il resto, senza aprire una sessione SSH.

Il plugin gira come utente `ots` dentro il processo web: **non può usare
`systemctl`**. Ma non serve — a RabbitMQ si possono fare domande migliori di
«l'unit è attiva?»:

- `queue_declare(passive=True)` sulla coda **`cot_parser`** dice quanti
  **consumer** ci sono e quanti messaggi sono in attesa. Zero consumer significa
  che nessuno sta smistando i CoT, ed è il guasto vero: una unit «active» con il
  processo figlio morto darebbe comunque zero qui.
- la stessa domanda sulla coda di un EUD dice se quel dispositivo è **davvero
  collegato**, senza `ss` da root. «Collegato» qui è un fatto, non una deduzione
  dall'ultima posizione ricevuta.

Cosa mostra: smistamento CoT, scrittura a database (età dell'ultimo CoT),
EUD collegati, RabbitMQ, e gli osservatori del plugin (firehose e MQTT).
Sotto, la tabella dei dispositivi con chi è collegato e quanti messaggi ha in
attesa in coda.

I giudizi sono calibrati per non gridare al lupo: senza EUD collegati la
tabella `cot` è ferma per forza e non viene segnalata; una coda EUD inesistente
vuol dire «mai collegato», non «guasto»; una coda che si accumula con un
consumer attivo è un avviso, non un errore. Stanno in `health.py` come funzioni
pure e sono coperti dai test.

Per **riavviare** un servizio serve comunque il server: la pagina dice cosa non
va e con quale comando, ma non ha i privilegi per farlo. La riparazione
automatica è compito di `ots/scripts/check-services.sh` (vedi il README del
repo), che gira come root da un timer.

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
| `OTS_EVENTCALENDAR_GM_TEAM_A_ID` | `0` | Gruppo ATAK di default del Team A (id `groups` di OTS, 0 = non impostato) |
| `OTS_EVENTCALENDAR_GM_TEAM_B_ID` | `0` | Gruppo ATAK di default del Team B |
| `OTS_EVENTCALENDAR_GM_OBSERVER_TEAM_IDS` | `[]` | Gruppo ATAK osservatore broadcast di default |
| `OTS_MILSIM_MESH_ENABLED` | `true` | Monitor Meshtastic attivo (osserva il firehose CoT) |
| `OTS_MILSIM_MESH_MQTT_OBSERVER` | `false` | Osserva anche il traffico MQTT grezzo: unica strada da cui arrivano canale, node id, RSSI/SNR |
| `OTS_MILSIM_MESH_LIVE_SECONDS` | `60` | Sotto questa età il tag è LIVE |
| `OTS_MILSIM_MESH_RECENT_SECONDS` | `300` | Sotto questa età è RECENT, oltre STALE |
| `OTS_MILSIM_MESH_GPS_STALE_SECONDS` | `120` | Oltre questa età la posizione è «stale» |
| `OTS_MILSIM_MESH_FALLBACK_POLICY` | `source_eud_group` | Canale non determinabile: `source_eud_group`, `default_group`, `meshtastic_group`, `ignore` |
| `OTS_MILSIM_MESH_DEFAULT_GROUP_ID` | `0` | Gruppo per la politica `default_group` (id `groups` di OTS) |
| `OTS_MILSIM_DP_FIX_STALE_YEARS` | `5` | Anni di validità scritti dalla riparazione dei data package (1–50) |

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
| `GET /datapackages/status` | admin | Tutti i data package con lo stato delle entità CoT (scadute, in scadenza, BOM/PI, errori) |
| `GET /datapackages/<hash>/download` | admin | Download dello zip dal browser |
| `POST /datapackages/<hash>/repair/preview` | admin | Anteprima prima/dopo della riparazione `{"years", "mode": "expired\|all"}` |
| `POST /datapackages/<hash>/repair` | admin | Crea la copia riparata `_vN` (l'originale resta) |
| `POST /datapackages/<hash>/files` | admin | Multipart `files` (multiplo): nuova versione `_vN` con i file aggiunti |
| `POST /datapackages` | admin | Multipart `name` + `files`: nuovo data package con i soli file caricati |
| `PATCH /datapackages/<hash>` | admin | `{"filename"}`: rinomina sul server e nel manifest (stesso uid, hash nuovo propagato a missioni e template) |
| `DELETE /datapackages/<hash>` | admin | Elimina riga e file; 409 se usato da missioni o template |
| `GET /groups` | admin | Gruppi ATAK di OTS con utenti ed EUD (sola lettura) |
| `POST /templates/<id>/play` | admin | Prepara la missione (stato *pronta*) e pusha ai destinatari; body opzionale `{"team_a_id", "team_b_id", "observer_team_ids"}` (id della tabella groups di OTS) |
| `GET /matches` | admin | Partite (pronte, in corso e storico) |
| `POST /matches/<id>/start` | admin | 🚦 Luce verde: annuncio + timer del server (chiusura automatica) |
| `POST /matches/<id>/invite` | admin | 🎯 Invita gli EUD dei team alla missione Data Sync della partita |
| `POST /matches/<id>/event` | admin | Evento arbitro (`{"event": "bomb_planted\|bomb_defused\|bomb_exploded"}`) |
| `POST /matches/<id>/republish` | admin | Ripubblica marker/aree (stessi UID) |
| `POST /matches/<id>/end` | admin | Termina/annulla manualmente: cancella i marker dagli EUD |
| `DELETE /matches/<id>` | admin | Elimina una sessione terminata dallo storico (replay compreso) |
| `GET /matches/<id>/replay` | admin | Tracce GPS nella finestra `started_at → ended_at` (`?step=N`) |
| `GET /orders` · `GET /orders/<uid>` | admin | Ordini SkyFi (paginati, `?search=`) · dettaglio |
| `GET /orders/<uid>/image` | admin | Anteprima ordine (data-URI, via proxy) |
| `GET /orders/<uid>/download/<tipo>` | admin | Proxy del deliverable (image/payload/cog/view-ready) |
| `POST /orders/<uid>/data_package` | admin | Data package ATAK con i tile WMTS dell'ordine |
| `POST /orders/<uid>/offline_map` | admin | `{"max_zoom": "native"\|10-22, "source": "cog"\|"view-ready", "format": "png"\|"jpeg95"\|"jpeg85"\|"geotiff"}`: avvia la mappa offline HD (202) |
| `GET /orders/offline_maps` | admin | Stato dei job mappa offline + comandi GDAL mancanti |
| `POST /orders/<uid>/mission` | admin | `{"mission", "deliverable_type"}`: asset nella missione Data Sync |
| `GET /missions` · `GET /missions/<nome>/contents` | admin | Missioni Data Sync · contenuti condivisi |
| `GET /missions/<nome>/contents/<hash>/download` · `/preview` | admin | Download / anteprima immagine di un contenuto |
| `DELETE /missions/<nome>/contents/<hash>` | admin | Rimuove il contenuto dalla missione (notifica EUD) |
| `GET /pcn/status` | admin | Semaforo WMS PCN (una GetMap di prova per servizio) |
| `GET /maintenance/health` | admin | Stato dei servizi critici: coda cot_parser, flusso CoT, EUD davvero collegati |
| `POST /groups/<id>/members` | admin | Aggiunge un utente (e tutti i suoi EUD) al gruppo, direzioni IN+OUT, con bind immediato delle code |
| `DELETE /groups/<id>/members/<user_id>` | admin | Toglie l'utente dal gruppo e sbinda le code |
| `GET /meshtastic/state?since=<seq>` | admin | Snapshot del monitor (card, semafori, tag) + delta del log eventi |
| `GET /meshtastic/tags/<key>` | admin | Dettaglio tag: anagrafica, strade di ricezione, traccia di routing, pacchetti recenti |
| `POST /meshtastic/tags/<key>` | admin | Dichiarazione manuale di canale/gruppo per quel tag |
| `DELETE /meshtastic/tags/<key>` | admin | Dimentica il tag (elenco conosciuti + dichiarazioni manuali) |
| `GET/POST /meshtastic/mappings` · `PUT/DELETE /meshtastic/mappings/<id>` | admin | Mappature canale → gruppo TAK |
| `POST /meshtastic/events/clear` | admin | Svuota la console eventi |

I badge caricati vengono salvati in
`~/ots/plugins/ots_milsim_companion_plugin/badges/` (inclusi nel backup di `update-ots.sh`).

## Test

```bash
cd ots/plugins/OTS-MilSim-Companion-Plugin
python -m pytest tests -q
```

I test dell'integrazione Meshtastic girano senza OpenTAKServer installato
(`tests/conftest.py` sostituisce `pika`, `flask` e `opentakserver.extensions`
con stub e l'accesso al DB con monkeypatch) e coprono: rilevamento del tag e
identità stabile, posizione, mappatura esplicita del canale, canali multipli
senza incroci, canale sconosciuto con tutte le politiche di fallback, relay
ATAK (identità del tracker distinta da quella dell'EUD), ricezione duplicata da
più gateway, invecchiamento LIVE→RECENT→STALE, isolamento fra gruppi e
sanificazione dei payload di debug.

`tests/test_datapackage.py` usa un data package reale di WinTAK
(`tests/fixtures/TacticalScoutCodogno.zip`) con "adesso" iniettato: conteggio
delle scadute, riparazione (BOM e `<?visible?>` tolti, uid e resto dei byte
invariati, manifest con name/uid nuovi, round-trip parsabile), file non CoT
copiati identici, zip malformati, zip-slip e limiti di dimensione.

`tests/test_offline_map.py` copre scelta del sorgente, livelli di zoom,
argomenti GDAL (bande, stretch 16→8 bit, alfa, nodata), avanzamento e errori
dei comandi, manifest e zip. Se sono installati i comandi GDAL e la libreria
Python `osgeo`, converte anche due GeoTIFF sintetici (UInt16 RGB+NIR con nodata,
Byte RGBA) e controlla nel GeoPackage zoom massimo, risoluzione esatta,
tile JPEG, bordi trasparenti e stretch; altrimenti quei test vengono saltati.

## Changelog

- **3.20.0** — mappa offline HD: formato **GeoTIFF COG senza perdita** (niente
  tile, ATAK lo apre come immagine nativa), perché ATAK-CIV mostrava il
  GeoPackage PNG allo zoom 17 pur avendo lo zoom 20 completo.
- **3.19.1** — mappa offline HD: il GeoPackage nel pacchetto porta il nome
  unico del pacchetto. Con due pacchetti dello stesso ordine il file aveva lo
  stesso nome di quello già installato e in uso su ATAK: l'import falliva e
  ATAK riscaricava in loop (il download da 299 MB arrivava sempre intero).
- **3.19.0** — mappa offline HD: sorgente di default il **COG** (originale
  senza perdita) invece del view-ready già JPEG, tile **PNG senza perdita** di
  default (JPEG 95/85 a scelta), pixel copiati identici (`near`) quando il
  sorgente è già sulla griglia di ATAK come i deliverable SkyFi; la card
  mostra cm/pixel a terra, sorgente e formato.
- **3.18.1** — la rinomina cambia anche il `name` del manifest dentro lo zip
  (quello che ATAK mostra da installato), con lo stesso uid; il nuovo hash
  viene propagato a contenuti delle missioni e template.
- **3.18.0** — tab Data Package: **✏️ Rinomina** (nome sul server e nei
  contenuti delle missioni; zip e hash invariati).
- **3.17.0** — tab Data Package: **📎 Aggiungi file** (PDF, immagini,
  documenti) a un pacchetto, in una nuova versione `_vN` accanto
  all'originale, e **➕ Nuovo data package** da zero con i file caricati.
- **3.16.1** — mappa offline HD: dentro OTS (eventlet/gevent) il pipe dei
  comandi GDAL è non bloccante e la lettura dell'avanzamento falliva con
  «[Errno 11] Resource temporarily unavailable»; ora si riprova dopo una pausa.
  Il timeout dei comandi vale anche durante la lettura dell'output.
- **3.16.0** — tab SkyFi: **🛰 Mappa offline HD**, data package con il
  GeoPackage dell'immagine originale convertita da GDAL (risoluzione piena,
  senza rete), job in background con avanzamento. La tab Data Package analizza
  anche pacchetti oltre i 100 MB (legge solo indice e XML); la riparazione
  resta limitata. Nuova dipendenza di sistema `gdal-bin`.
- **3.15.0** — tab **Data Package**: stato delle entità CoT di ogni pacchetto
  (scadute, in scadenza, BOM/PI, errori), riparazione in una copia `_vN` con
  anteprima prima/dopo, download ed eliminazione dell'originale. Nuova chiave
  `OTS_MILSIM_DP_FIX_STALE_YEARS`.

## Roadmap

- **Orchestratore in campo** (Raspberry + LoRaWAN): le partite sono già
  interrogabili via API (`GET /matches`), un dispositivo in campo potrà
  leggere lo stato della partita e comparire come marker/entità attiva sulla
  mappa (es. l'ordigno stesso che trasmette il proprio stato).
- **Cattura dei punti di dominio**: registrare quale squadra controlla ogni
  punto (dall'orchestratore in campo o manualmente dal Game Master) e
  aggiornarne il colore sugli EUD.
- Punteggi di fine partita agganciati all'anagrafica giocatori.
- **Canale Meshtastic nel relay ATAK**: proporre al plugin Meshtastic per ATAK
  di valorizzare `<__meshtastic channel="…" channel_name="…" node_id="…"/>`
  invece dell'elemento vuoto di oggi. Il parser del monitor legge già quegli
  attributi: il giorno che arrivassero, il routing per canale funzionerebbe
  anche via relay senza dichiarazioni manuali.

## Esempio CSV

Vedi [`examples/eventi-esempio.csv`](examples/eventi-esempio.csv).

## Licenza

GPL-3.0-or-later (requisito per i plugin OpenTAKServer distribuiti pubblicamente).
