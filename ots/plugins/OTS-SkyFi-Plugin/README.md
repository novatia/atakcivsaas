# OTS-SkyFi-Plugin

Plugin OpenTAKServer per gli ordini [SkyFi](https://app.skyfi.com): lista degli
asset satellitari acquistati con anteprima, download dei deliverable e
creazione di data package ATAK con i tile WMTS.

Fork di [brian7704/OTS-SkyFi-Plugin](https://github.com/brian7704/OTS-SkyFi-Plugin)
(GPL-3.0-or-later): l'upstream non distribuisce la UI (submodule mai
committato) e non pubblica su PyPI, quindi il plugin vive qui con una UI
propria, statica, servita direttamente dal pacchetto — nessun build Node
richiesto.

## Funzioni

- **Ordini**: elenco paginato degli ordini dell'account SkyFi (archive e
  tasking) con anteprima, stato delivery, località, risoluzione, area e costo;
  ricerca per località/codice/stato.
- **Download asset**: i deliverable disponibili (immagine, payload, COG,
  view-ready COG) si scaricano dal browser passando dal backend come proxy —
  l'API key non raggiunge mai il client.
- **Data package ATAK**: per gli ordini con tile WMTS crea un data package
  (Google Hybrid + layer SkyFi) pronto per gli EUD, come nell'upstream.
- **Assegna a missione**: un deliverable (payload di default) viene scaricato
  da SkyFi sul server e aggiunto ai contenuti di una missione Data Sync — gli
  EUD iscritti ricevono il CoT di mission change e scaricano il file da
  `/Marti/sync/content`, come se fosse stato caricato da ATAK.
- **Contenuti missione** (tab *Missioni*): la web UI di OTS non mostra i
  dataset condivisi sulle missioni Data Sync; questo tab li elenca per
  missione (qualunque sia la fonte: plugin, ATAK, web UI) con download dal
  browser e rimozione dalla missione. La rimozione replica il flusso di
  `DELETE /Marti/api/missions/<name>/contents`: toglie solo il link
  contenuto↔missione, registra un `MissionChange` `REMOVE_CONTENT` e notifica
  gli EUD iscritti; il file resta sul server e nello storico della missione.
  Dalla 1.3.0 i contenuti immagine (foto GeoCam incluse) hanno l'**anteprima**
  in tabella: miniatura servita inline da `/missions/<name>/contents/<hash>/preview`,
  clic per aprirla a dimensione piena.
- **Configurazione**: API key modificabile dalla UI, salvata in `config.yml`.

## Configurazione

| Chiave | Default | Descrizione |
|---|---|---|
| `OTS_SKYFI_PLUGIN_ENABLED` | `True` | Abilita il plugin |
| `OTS_SKYFI_PLUGIN_API_KEY` | `""` | API key SkyFi (account Pro: app.skyfi.com → Profile → API Key) |

Senza API key il log mostra "Failed to get orders" quando si apre la UI:
inserirla nel tab Configurazione.

## Installazione

Sul server, dal clone del repo:

```bash
sudo ./ots/scripts/update-skyfi-plugin.sh
```

UI (solo ruolo `administrator`): `https://<server>/api/plugins/ots_skyfi_plugin/ui`
