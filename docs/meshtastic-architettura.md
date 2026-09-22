# Meshtastic in OpenTAKServer + MilSim Companion — analisi dell'architettura

Analisi condotta sul sorgente reale, non sulla documentazione:

| Cosa | Versione / origine |
|------|--------------------|
| OpenTAKServer | **1.7.13** (sdist da PyPI, la stessa installata su `n3test002`) |
| MilSim Companion | 3.12.1 (`ots/plugins/OTS-MilSim-Companion-Plugin`) |
| Plugin Meshtastic per ATAK | `github.com/meshtastic/ATAK-Plugin`, `main` |

Tutto quello che segue è stato verificato leggendo il codice; dove una cosa non
è verificabile senza traffico reale è scritto esplicitamente.

---

## 1. API plugin di OpenTAKServer: cosa offre davvero

`opentakserver/plugins/Plugin.py` + `PluginManager.py`:

```python
class Plugin(BasePlugin):
    group = "opentakserver.plugin"
    blueprint: Blueprint | None = None
    def activate(self, app: Flask, enabled: bool) -> None: ...
    def stop(self) -> None: ...
    def get_info(self) -> dict | None: ...
    def load_metadata(self) -> {}: ...
```

**Non esiste alcun hook sugli eventi CoT.** Un plugin riceve solo l'oggetto
Flask in `activate()` e può registrare un blueprint. Non ci sono callback
`on_cot`, `on_eud`, né un bus di eventi interno.

Conseguenza: per osservare o instradare CoT un plugin **deve parlare
direttamente con RabbitMQ**. Non è una forzatura: il commento in
`eud_handler/EudHandler.py:467` lo dice esplicitamente —

> `# Route all CoTs to the firehose exchange for plugins and users that connect directly to RabbitMQ`

Il firehose è l'hook ufficiale per i plugin. MilSim Companion lo usa già in
uscita (`cot.py::deliver`).

### Dove girano i plugin

`app.py:497-501` — `PluginManager.load_plugins()/activate()` è chiamato **solo**
in `create_app()` del processo web principale. `cot_parser/cot_parser.py` e
`eud_handler/eud_handler.py` hanno un `create_app()` proprio, minimale, che
**non** carica i plugin (verificato con grep su tutto il sorgente).

Conseguenza pratica importante: lo stato in memoria del monitor vive nello
stesso processo che serve le rotte HTTP del plugin. Niente IPC, niente
sincronizzazione fra processi. Resta comunque un guard per non far partire due
consumer nello stesso processo.

---

## 2. Exchange, queue e routing key realmente in uso (OTS 1.7.13)

Dichiarati in `app.py:163-173`:

| Exchange | Tipo | Uso |
|----------|------|-----|
| `cot_parser` | direct | routing key `cot_parser` → pool di processi `cot_parser` (persistenza + smistamento) |
| `firehose` | **fanout** | copia di **ogni** CoT, «per i plugin» |
| `groups` | topic | routing key `<NOME_GRUPPO>.OUT` e `__ANON__.OUT` → code degli EUD |
| `dms` | direct | routing key = `uid` o `callsign` dell'EUD |
| `missions` | topic | `missions.<nome>` |
| `chatrooms` | direct | chat |
| `amq.topic` | topic | traffico MQTT Meshtastic grezzo (plugin MQTT di RabbitMQ) |

Le code degli EUD (`EudHandler.py:513-599`) si legano a: `groups` con
`<gruppo>.OUT` per ogni membership OUT abilitata (oppure `__ANON__.OUT`),
`dms` con il proprio uid e callsign, `missions`.

**Il firehose non è più consumato dagli EUD in 1.7.13**: pubblicarci sopra non
consegna nulla ai client, serve solo come feed di osservazione.

### Smistamento di un CoT in arrivo da un EUD

`eud_handler/EudHandler.py:467-481` pubblica **due** copie:

```python
{"uid": self.uid, "cot": str(event)}                           # → firehose
{"uid": self.uid, "cot": str(event), "user_id": self.user.id}  # → cot_parser
```

dove `self.uid` è **l'uid dell'EUD connesso**, non l'uid dell'evento.

`cot_parser/cot_parser.py::route_cot()` (riga 1142) decide così:

1. ci sono `<dest>` → `dms` per callsign/uid;
2. niente dest e niente `user_id` → `__ANON__.OUT`;
3. niente dest → per ogni `GroupUser(user_id, direction=IN, enabled=True)`
   pubblica su `groups` con routing key `<gruppo>.OUT`; se l'utente non ha
   gruppi IN → `__ANON__.OUT`.

**Quindi un CoT rilanciato da un EUD viene instradato ai gruppi dell'utente che
possiede quell'EUD.** Vale anche per i tracker Meshtastic rilanciati: è di
fatto il fallback «gruppo dell'EUD sorgente», già nativo.

### Il tracker resta un'entità distinta

`insert_cot()` salva `sender_uid = uid dell'EUD relay` ma `uid = event.uid`;
`parse_point()` salva `Point.uid = event.uid` e `Point.device_uid = uid EUD`.
Il CoT inoltrato ai client mantiene `event.uid` originale. **OTS non riscrive
l'uid né il callsign del tracker con quelli dell'EUD**: il requisito «ALPHA,
ALPHA-1, ALPHA-2 come tre entità» è già soddisfatto dal core.

Nota: il tracker **non** ottiene una riga in `euds` (nessun `<takv>` nel CoT
rilanciato), quindi non compare nella lista EUD della web UI di OTS; compare in
`cot`/`points` e sulle mappe degli ATAK.

---

## 3. Integrazione Meshtastic nativa di OTS (Path B)

`controllers/meshtastic_controller.py`, attiva solo con `OTS_ENABLE_MESHTASTIC: true`.

- Consuma una coda `meshtastic` legata a **`amq.topic` con routing key `#`**
  (riga 74-80). Il traffico arriva lì dal plugin MQTT di RabbitMQ: il broker
  MQTT è RabbitMQ stesso.
- **Il nome del canale Meshtastic si ricava dalla routing key**:
  `basic_deliver.routing_key.split(".")[3]` (riga 150). Lo schema costruito in
  uscita è `"{OTS_MESHTASTIC_TOPIC}.2.e.{canale}."` (riga 222), cioè il topic
  MQTT `<topic>/2/e/<canale>/<!nodeid>` con `/` → `.`.
- L'indice di canale è in `mp.channel`; RSSI/SNR/hop sono in `mp.rx_rssi`,
  `mp.rx_snr`, `mp.hop_limit`/`mp.hop_start`.
- Genera CoT con `<takv platform="Meshtastic" os="Meshtastic"
  meshtastic_id="...">` e `<contact endpoint="MQTT">` (riga 276-300).
- **Instradamento: un unico gruppo fisso.** `protobuf_to_cot()` (riga 710)
  pubblica su `groups` con routing key `f"{OTS_MESHTASTIC_GROUP}.OUT"` —
  default `Meshtastic`. Nessuna mappatura per canale, nessuna configurabilità
  oltre quel singolo nome.

Questa è esattamente la lacuna che la feature colma.

Config rilevante (`defaultconfig.py:147-152`):

```yaml
OTS_ENABLE_MESHTASTIC: False
OTS_MESHTASTIC_TOPIC: opentakserver
OTS_MESHTASTIC_GROUP: Meshtastic
OTS_MESHTASTIC_DOWNLINK_CHANNELS: []
```

Tabella `meshtastic_channels` (`models/Meshtastic.py`): `name`, **`psk`**,
`uplink_enabled`, `downlink_enabled`, `position_precision`, parametri LoRa.
Il campo `psk` è un segreto: non deve mai finire in una UI di debug.

---

## 4. Path A — relay via plugin Meshtastic di ATAK: cosa sopravvive davvero

Risposta alla domanda della sezione 22 del capitolato, ricavata dal sorgente
del plugin ATAK (`app/src/main/java/com/atakmap/android/meshtastic/`).

### Come funziona il relay

`MeshtasticReceiver` riceve i pacchetti dal servizio Meshtastic, costruisce un
`CotEvent` e poi:

```java
CotMapComponent.getInternalDispatcher().dispatch(cotEvent);
if (prefs.getBoolean(Constants.PREF_PLUGIN_SERVER, false)) {
    CotMapComponent.getExternalDispatcher().dispatch(cotEvent);   // ← "Relay to Server"
}
```

Il CoT che arriva al server è quindi **lo stesso** che ATAK mostra localmente:
nessun arricchimento, nessun wrapper, nessun rewrite dell'uid.

### CoT generato da un PLI TAK_PACKET (il caso del TAK Tracker)

Da `MeshtasticReceiver.java:985-1082`:

```xml
<event version="2.0" uid="<TAKPacket.contact.device_callsign>"
       type="a-f-G-U-C" how="m-g"
       time="..." start="..." stale="+10 min">
  <point lat="..." lon="..." hae="9999999.0" ce="9999999.0" le="9999999.0"/>
  <detail>
    <contact callsign="<TAKPacket.contact.callsign>" endpoint="0.0.0.0:4242:tcp"/>
    <__group role="Team Member" name="Cyan"/>
    <status battery="33"/>
    <track speed="0" course="0"/>
    <__meshtastic/>
  </detail>
</event>
```

### Verdetto: il canale Meshtastic NON sopravvive al relay

`<__meshtastic/>` è creato **vuoto, senza alcun attributo** — in tutte e nove
le occorrenze del sorgente (`new CotDetail("__meshtastic")` seguito solo da
`cotDetail.addChild(meshDetail)`). È un **flag di provenienza**, niente di più.

| Informazione | Sopravvive al relay ATAK? |
|---|---|
| uid stabile del tracker | **SÌ** (`event.uid` = `device_callsign` del TAKPacket) |
| callsign | **SÌ** (`<contact callsign>`) |
| lat/lon | **SÌ** |
| altitudine | NO (il PLI la porta, il CoT scrive `hae=9999999.0`) |
| batteria | **SÌ** (`<status battery>`) |
| team/ruolo ATAK | **SÌ** (`<__group>`) — è il team ATAK, **non** il canale Meshtastic |
| velocità/rotta | **SÌ** (`<track>`) |
| flag «viene da Meshtastic» | **SÌ** (`<__meshtastic/>`) |
| **indice di canale Meshtastic** | **NO** |
| **nome del canale Meshtastic** | **NO** |
| node ID Meshtastic (`!bbad0ac8`) | **NO** |
| RSSI / SNR / hop count | **NO** |
| voltaggio / firmware / hw model | **NO** |
| TAK role «TAK_TRACKER» | NO (il `role` è il `MemberRole` del TAKPacket: Team Member/Team Lead/…) |

Il canale **esiste** lato ATAK (`payload.getChannel()` a riga 855 e il filtro
`PREF_PLUGIN_FILTER_BY_CHANNEL` a riga 847-860) ma serve solo a scartare i
pacchetti fuori canale: non viene mai scritto nel CoT.

Corollario operativo: **un EUD ATAK con il plugin Meshtastic rilancia di fatto
un solo canale**, quello selezionato nelle sue preferenze, se il filtro è
attivo. Questo rende l'euristica «canale = quello configurato per l'EUD
sorgente» ragionevole, ma resta un'assunzione di configurazione, non un dato:
va dichiarata dall'amministratore, mai indovinata.

### Cosa si può fare, in ordine di costo

1. **Fallback al gruppo dell'EUD sorgente** — già il comportamento nativo di
   `route_cot()`, zero righe di codice, zero assunzioni. Scelto come default.
2. **Mappatura statica tag → canale/gruppo** decisa dall'amministratore nella
   UI, con l'uid/node del tag come chiave. Copre il caso reale «questo tracker
   è di BRAVO anche se lo rilancia un telefono di ALPHA».
3. **Correlazione con il feed MQTT**: quando lo stesso tag arriva anche via
   gateway MQTT il canale c'è, e viene riusato per le ricezioni via relay dello
   stesso node.
4. Modifica al plugin Meshtastic di ATAK (aggiungere `channel`, `channel_name`,
   `node_id` come attributi di `<__meshtastic>`). È un progetto upstream terzo:
   **non toccato**, solo documentato qui come proposta.

---

## 5. Decisione architetturale

Nessun fork di OpenTAKServer. Tutto dentro MilSim Companion:

```
EUD ATAK ──TCP/SSL──► eud_handler ──► firehose (fanout) ──► MilSim Meshtastic monitor
                                  └──► cot_parser ──► groups.<gruppo EUD>.OUT   (nativo OTS)

gateway MQTT ──► amq.topic ──► (opz.) MilSim MQTT observer → canale + node id + RSSI/SNR
                          └──► meshtastic_controller di OTS → groups.Meshtastic.OUT (nativo)

MilSim routing ──► groups.<gruppo mappato>.OUT   (stesso meccanismo di OTS, stesso exchange)
```

- **Osservazione**: coda esclusiva e auto-delete legata a `firehose`. Non
  interferisce con nessun consumer esistente (è un fanout).
- **Canale**: mai indovinato. Da MQTT si legge dal topic; dal relay ATAK non
  c'è → `UNKNOWN` esplicito e politica di fallback scelta dall'amministratore.
- **Instradamento**: `basic_publish(exchange="groups", routing_key=f"{gruppo}.OUT")`,
  identico a quello che fa `cot_parser`. Nessun broadcast a tutti i client,
  nessuna scorciatoia sull'autorizzazione: chi non è nel gruppo non riceve.
- **Nessuna doppia generazione di CoT**: il monitor non ripubblica mai un CoT
  già visto (dedup per uid+time), e l'instradamento è soppresso quando il
  gruppo mappato è già fra quelli in cui OTS ha instradato nativamente.

### Limitazione nota (e perché non giustifica un fork)

Il plugin **non può impedire** a `cot_parser` di instradare il CoT rilanciato
ai gruppi dell'EUD sorgente: quella decisione è presa dentro `route_cot()`, in
un altro processo, senza alcun punto di estensione. Il plugin può solo
**aggiungere** una consegna al gruppo mappato.

Effetto pratico: un tag su canale BRAVO rilanciato da un telefono di ALPHA sarà
visto da BRAVO (grazie al plugin) **e** da ALPHA (per via del relay). Chi non
sta né in ALPHA né in BRAVO non lo vede: l'isolamento verso i gruppi estranei
regge.

Per l'isolamento stretto servirebbe un hook in `route_cot()` — p.es. una lista
di callable `app.config["OTS_COT_ROUTERS"]` consultata prima dello smistamento
per gruppo, con facoltà di sostituire la decisione. È la modifica minima
proponibile upstream; **non è stata implementata** e non serve per il caso
d'uso descritto.

---

## 6. Riepilogo dei fatti su cui si basa l'implementazione

| Fatto | Dove verificato |
|---|---|
| L'API plugin non ha hook CoT | `plugins/Plugin.py`, `plugins/PluginManager.py` |
| I plugin girano solo nel processo web | `app.py:497`, assenza di PluginManager in `cot_parser`/`eud_handler` |
| `firehose` è fanout e serve ai plugin | `app.py:170`, `eud_handler/EudHandler.py:467` |
| Il CoT rilanciato porta `uid` del tracker, non dell'EUD | `cot_parser.py:101-140` |
| Lo smistamento per gruppo usa `groups`/`<nome>.OUT` | `cot_parser.py:1195-1212`, `EudHandler.py:559-568` |
| Meshtastic nativo instrada a un solo gruppo | `meshtastic_controller.py:710` |
| Il canale MQTT si legge dalla routing key | `meshtastic_controller.py:150`, `:222` |
| `<__meshtastic/>` è vuoto | `MeshtasticReceiver.java`, 9 occorrenze |
| `psk` dei canali è in DB | `models/Meshtastic.py` |
