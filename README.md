# atakcivsaas

Repo di lavoro per i server TAK:

1. **OpenTAKServer** (`ots/`) — script di gestione e modifiche personalizzate per il server di produzione (installazione pip su Ubuntu, servizio systemd `opentakserver`, dati in `/home/ots/ots`).
2. **FreeTAKServer via Docker** (file nella root: `Dockerfile`, `docker-compose.yml`, `Jenkinsfile`, `config/`) — setup legacy, mantenuto per la pipeline Jenkins esistente.

## OpenTAKServer

### Layout sul server

| Cosa | Dove |
|------|------|
| Venv / codice | `/home/ots/.opentakserver_venv/` |
| Dati e config | `/home/ots/ots/` (`config.yml`, `ca/`, `logs/`, `plugins/`) |
| Servizio | `systemctl {status,restart} opentakserver` |
| Web UI | file statici serviti da nginx (root nei config in `/etc/nginx/sites-enabled/`) |
| Porte | 8080 HTTP (nginx), 8443 HTTPS mutual-TLS (nginx), 8089 CoT SSL (`eud_handler`) |

### Aggiornamento

Sul server, da root:

```bash
git clone https://github.com/novatia/atakcivsaas.git
cd atakcivsaas/ots/scripts
chmod +x update-ots.sh
./update-ots.sh --check    # mostra installata vs ultima su PyPI
./update-ots.sh            # backup + upgrade backend + restart + verifica porte
./update-ots.sh --ui       # aggiorna anche la web UI servita da nginx
```

Lo script fa il backup di `/home/ots/ots` in `/root/ots-backups/` (ruota gli ultimi 5) prima di toccare qualsiasi cosa, e stampa il comando di rollback alla fine.

### Struttura del repo

```
ots/
  scripts/
    update-ots.sh                        # aggiornamento backend + UI con backup e verifica
    install-milsim-companion-plugin.sh   # installa/aggiorna il plugin MilSim Companion nel venv
  plugins/
    OTS-MilSim-Companion-Plugin/   # calendario, presenze, punteggi/gradi + modalità di gioco
  systemd/
    opentakserver-cot-parser.service   # unit per il parser CoT (vedi Troubleshooting)
```

### Plugin

- **[OTS-MilSim-Companion-Plugin](ots/plugins/OTS-MilSim-Companion-Plugin/README.md)** —
  MilSim Soft-Air Companion (unifica gli ex OTS-EventCalendar-Plugin, OTS-GameMode-Plugin
  e OTS-SkyFi-Plugin): calendario eventi (sede dall'anagrafica campi da gioco, inizio/fine,
  descrizione), import da Google Calendar (ICS) o CSV, RSVP utenti, conferma presenze sul
  campo con assegnazione punti, classifica e gradi militari con badge, replay giocata su
  mappa; le **modalità di gioco** (Capture the Flag, Bomb Defusal, Team Deathmatch,
  Dominio): template di missione disegnati su mappa dal browser (spawn point, bandiere,
  bomb site, punti di dominio, aree) e **▶ Play** che crea la partita pushando marker,
  aree e data package a tutti gli EUD collegati; **SkyFi** (ordini satellitari, download
  deliverable via proxy, data package ATAK, asset nelle missioni Data Sync), gestione
  contenuti delle **missioni Data Sync** e semaforo dello stato **mappe PCN**;
  **Meshtastic** (dalla 3.13.0): Live Monitor dei tag/TAK tracker che arrivano
  al server — dal relay del plugin Meshtastic di ATAK o dal feed MQTT — con
  traccia della decisione di instradamento, packet inspector con CoT grezzo
  sanificato e mappatura **canale Meshtastic → gruppo TAK**
  ([analisi dell'architettura](docs/meshtastic-architettura.md)).

### Troubleshooting

- **EUD connessi ma invisibili in mappa / tabelle `cot` e `points` vuote**: il main di OTS
  non avvia il processo `cot_parser` (che consuma i CoT da RabbitMQ, li scrive nel DB e
  soprattutto li **smista ai gruppi** con `route_cot`), nonostante
  `OTS_COT_PARSER_PROCESSES: 1` in `config.yml`. Soluzione: unit dedicata —
  ```bash
  cp ots/systemd/opentakserver-cot-parser.service /etc/systemd/system/
  systemctl daemon-reload && systemctl enable --now opentakserver-cot-parser
  ```
  Dopo ogni upgrade verificare che giri: `ps aux | grep cot_parser`.

- **`cot_parser` muore da solo e systemd non lo riavvia** (osservato il 2026-09-22 dopo
  12 ore di esercizio). Il `main()` di cot_parser forka un figlio e poi fa `os.waitpid()`
  su di lui: quando il figlio muore, la waitpid ritorna, `main()` finisce e il **padre esce
  con codice 0**. Per systemd è un'uscita riuscita, quindi con `Restart=on-failure` l'unit
  resta `inactive (dead)` in silenzio. Firma nel journal: un `Deactivated successfully`
  **senza** nessun `Stopping` che lo precede.

  L'unit nel repo usa quindi `Restart=always`. Se la tua copia in
  `/etc/systemd/system/` è vecchia, riallineala:
  ```bash
  cp ots/systemd/opentakserver-cot-parser.service /etc/systemd/system/
  systemctl daemon-reload && systemctl restart opentakserver-cot-parser
  ```

  **Come si manifesta**: gli EUD sono connessi, il traffico CoT arriva (il monitor
  Meshtastic di MilSim Companion continua a mostrarlo, perché il firehose lo alimenta
  `eud_handler`), ma nessun EUD vede più gli altri e la tabella `cot` smette di crescere.
  Diagnosi in due comandi:
  ```bash
  ps aux | grep -c "[c]ot_parser"
  sudo -u postgres psql opentakserver -c "SELECT sender_uid, type, timestamp FROM cot ORDER BY id DESC LIMIT 5;"
  ```
  Se il primo dà `0` e l'ultima riga di `cot` è vecchia, è questo. Bug upstream da
  segnalare: il padre dovrebbe uscire con codice diverso da zero quando il figlio muore.

### Documentazione

- **[docs/meshtastic-architettura.md](docs/meshtastic-architettura.md)** —
  come OpenTAKServer 1.7.13 riceve e instrada i CoT (exchange RabbitMQ, code,
  routing key), cosa offre davvero l'API plugin, come funziona l'integrazione
  Meshtastic nativa e — verificato sul sorgente del plugin Meshtastic per ATAK —
  **quali informazioni sopravvivono al «Relay to Server» e quali no**.

### Note

- Non modificare mai i file in `site-packages/opentakserver/` o la UI installata: il prossimo upgrade li sovrascrive. Le personalizzazioni vanno fatte come plugin OTS o come location nginx separate, versionate in questo repo.
- Backend e UI vanno tenuti allineati di versione.
