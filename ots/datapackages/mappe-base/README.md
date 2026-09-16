# Data package: Mappe base — Satellite e topografiche

Sorgenti tile globali, veloci e senza limiti di scala, in formato `customMapSource`
per ATAK-CIV e WinTAK. A differenza delle mappe PCN (WMS ministeriale, lento e
visibile solo a zoom stretti) queste funzionano a **tutti** i livelli di zoom.

## Contenuto

| File | Sorgente | Zoom | Uso |
|---|---|---|---|
| `Esri_Satellite.xml` | Esri World Imagery | 0–19 | satellite ad alta risoluzione, la scelta di default |
| `Google_Hybrid.xml` | Google Hybrid | 0–20 | satellite + nomi strade/luoghi |
| `Esri_Topo.xml` | Esri World Topo Map | 0–19 | topografica moderna |
| `OpenStreetMap.xml` | OSM standard | 0–19 | stradale |
| `OpenTopoMap.xml` | OpenTopoMap | 0–17 | topografica con curve di livello |

Tutte testate con tile reali (HTTP 200, immagini valide) il 2026-09-16.

## Installazione

- **WinTAK**: copia gli `.xml` in `C:\ProgramData\WinTAK\Imagery` e riavvia WinTAK.
- **ATAK**: importa `Mappe-Base.zip` (☰ → Import → Local SD) o caricalo su OTS
  come data package.

## Build

```bash
./build.sh   # genera Mappe-Base.zip
```
