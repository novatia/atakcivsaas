# Data package: PCN Geoportale Italia — Mappe WMS

Sorgenti mappa WMS del Geoportale Nazionale (ex PCN, `wms.pcn.minambiente.it`, MASE)
in formato `customWmsMapSource` per ATAK-CIV.

## Contenuto

| File | Layer | Zoom | Note |
|---|---|---|---|
| `PCN_Ortofoto_2012.xml` | Ortofoto AGEA 2012, 50 cm/px | 13–19 | visibile solo a scale > 1:100.000 |
| `PCN_Ortofoto_2006.xml` | Ortofoto 2006 | 13–19 | idem |
| `PCN_IGM_25000.xml` | Carta topografica IGM 1:25.000 | 13–16 | visibile solo a scale > 1:100.000 |
| `PCN_IGM_100000.xml` | Carta topografica IGM 1:100.000 | 11–14 | visibile solo a scale > 1:300.000 |
| `PCN_IGM_250000.xml` | Carta corografica IGM 1:250.000 | 11–13 | idem |

Tutte le sorgenti usano WMS 1.1.1, EPSG:3857, JPEG. Endpoint solo HTTP
(il server reindirizza HTTPS → HTTP). Uso gratuito, nessuna condizione applicata
(dichiarato nei GetCapabilities dei servizi).

## Build del data package

```bash
./build.sh   # genera PCN-Geoportale-Italia.zip
```

## Installazione su ATAK-CIV

1. Copiare `PCN-Geoportale-Italia.zip` sul telefono (o caricarlo su OTS come data package).
2. In ATAK: **☰ → Import → Local SD** e selezionare lo zip.
3. Le mappe compaiono nel selettore sorgenti (globo → Mobile).

In alternativa i singoli XML si possono copiare direttamente in
`atak/imagery/mobile/mapsources/` sul device.

## Avvertenze

- Sotto lo zoom minimo indicato il server restituisce tile vuote (limite di scala lato server).
- I server PCN hanno lentezze/down occasionali: per uso sul campo pre-cachare
  l'area da ATAK (download offline dell'area).
