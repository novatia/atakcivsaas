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
    update-ots.sh                      # aggiornamento backend + UI con backup e verifica
    install-eventcalendar-plugin.sh    # installa/aggiorna il plugin calendario nel venv
  plugins/
    OTS-EventCalendar-Plugin/   # calendario eventi, presenze, punteggi e gradi
  systemd/
    opentakserver-cot-parser.service   # unit per il parser CoT (vedi Troubleshooting)
```

### Plugin

- **[OTS-EventCalendar-Plugin](ots/plugins/OTS-EventCalendar-Plugin/README.md)** — calendario
  eventi (sede dall'anagrafica campi da gioco, inizio/fine, descrizione), import da Google
  Calendar (ICS) o CSV, RSVP utenti (presente / non presente / in dubbio, default non
  configurato), conferma presenze sul campo da parte degli admin con assegnazione punti,
  classifica e gradi militari con badge configurabili.

### Troubleshooting

- **EUD connessi ma invisibili in mappa / tabelle `cot` e `points` vuote**: il main di OTS
  non avvia il processo `cot_parser` (che consuma i CoT da RabbitMQ e li scrive nel DB),
  nonostante `OTS_COT_PARSER_PROCESSES: 1` in `config.yml`. Soluzione: unit dedicata —
  ```bash
  cp ots/systemd/opentakserver-cot-parser.service /etc/systemd/system/
  systemctl daemon-reload && systemctl enable --now opentakserver-cot-parser
  ```
  Dopo ogni upgrade verificare che giri: `ps aux | grep cot_parser`.

### Note

- Non modificare mai i file in `site-packages/opentakserver/` o la UI installata: il prossimo upgrade li sovrascrive. Le personalizzazioni vanno fatte come plugin OTS o come location nginx separate, versionate in questo repo.
- Backend e UI vanno tenuti allineati di versione.
