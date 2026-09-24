"""Analisi e riparazione dei data package (zip TAK) senza Flask né database.

Il problema che risolve (verificato su ATAK-CIV, vedi Manuale Operatore Rev 17,
sez. 7): il Data Package tool di WinTAK scrive le aree disegnate (`u-d-*`) con
stale = creazione + 7 giorni, i marker (`a-*`) con stale + 1 anno. Passata la
settimana, all'import ATAK scarta le aree come scadute — in Data Package tool
compaiono le righe «Map Item» con «Not Found» in rosso — e tiene i marker.
In più ogni .cot di WinTAK inizia con un BOM UTF-8 e alcuni hanno la
processing instruction `<?visible true?>` dopo la dichiarazione XML.

La riparazione riscrive `time`/`start` = adesso e `stale` = adesso + N anni
sul solo tag `<event>`, a livello di testo: uid, punti, colori e il resto del
file restano byte per byte come li ha scritti WinTAK. Nel manifest cambiano
name (suffisso _vN) e uid, così ATAK non riusa il pacchetto rotto già
importato.

Si legge lo zip (contenuto o percorso) e si produce il nuovo zip in memoria:
salvarlo in UPLOAD_FOLDER e registrarlo nel DB è compito dell'app. Nessun
file viene mai estratto su disco.
"""

from __future__ import annotations

import io
import os
import re
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone

# Limiti contro zip enormi o zip bomb. Un data package di aree e marker pesa
# qualche KB; con mappe/ortofoto dentro si arriva a decine di MB.
MAX_PACKAGE_BYTES = 100 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 300 * 1024 * 1024
MAX_ENTRIES = 2000
# Un singolo .cot o il manifest vengono letti interi in memoria
MAX_XML_ENTRY_BYTES = 5 * 1024 * 1024

# Sotto questa soglia l'entità è «in scadenza»: ATAK la importa ancora, ma
# fra pochi giorni succederà la stessa cosa
EXPIRING_WITHIN = timedelta(days=7)

MANIFEST_PATH = "MANIFEST/manifest.xml"

STATUS_OK = "ok"
STATUS_EXPIRING = "expiring"
STATUS_EXPIRED = "expired"
STATUS_DIRTY = "dirty"  # BOM o processing instruction, ma non scaduta
STATUS_ERROR = "error"

MODE_EXPIRED = "expired"  # solo scadute e in scadenza
MODE_ALL = "all"

BOM = b"\xef\xbb\xbf"

_ISO_RE = re.compile(
    r"^\s*(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?\s*(Z|[+-]\d{2}:?\d{2})?\s*$"
)
# Tag di apertura di <event>: i valori fra virgolette possono contenere '>'
_EVENT_TAG_RE = re.compile(rb"<event\b(?:[^>\"']|\"[^\"]*\"|'[^']*')*>")
# Processing instruction diverse dalla dichiarazione <?xml ...?>
_PI_RE = re.compile(rb"<\?(?![xX][mM][lL][\s?])[^?]*(?:\?(?!>)[^?]*)*\?>[ \t]*(?:\r?\n)?")


class DataPackageError(ValueError):
    """Zip rifiutato: malformato, pericoloso o troppo grande."""


# ----------------------------------------------------------------------
# Utilità
# ----------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_cot_time(value: str | None) -> datetime | None:
    """Timestamp CoT → datetime UTC. Accetta i decimali di WinTAK (`.57Z`),
    quelli di ATAK (`.123Z`) e gli offset; senza fuso vale UTC."""
    if not value:
        return None
    m = _ISO_RE.match(value)
    if not m:
        return None
    year, month, day, hour, minute, second, frac, tz = m.groups()
    micro = int((frac or "0")[:6].ljust(6, "0"))
    try:
        dt = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second), micro)
    except ValueError:
        return None
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
        return (dt - sign * offset).replace(tzinfo=timezone.utc)
    return dt.replace(tzinfo=timezone.utc)


def format_cot_time(dt: datetime) -> str:
    """Formato che scrive ATAK: millisecondi e Z."""
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def add_years(dt: datetime, years: int) -> datetime:
    try:
        return dt.replace(year=dt.year + years)
    except ValueError:  # 29 febbraio → 28
        return dt.replace(year=dt.year + years, day=28)


def _split_version(name: str) -> tuple[str, int, str]:
    """`Campi_v3.zip` → ("Campi", 3, ".zip"); senza suffisso la versione è 1."""
    stem, ext = (name[:-4], name[-4:]) if name.lower().endswith(".zip") else (name, "")
    m = re.search(r"_v(\d+)$", stem)
    if m:
        return stem[: m.start()], int(m.group(1)), ext
    return stem, 1, ext


def version_of(name: str) -> int:
    return _split_version(name)[1]


def with_version(name: str, version: int) -> str:
    """Nome con suffisso _vN (sostituisce quello che c'è già)."""
    stem, _, ext = _split_version(name)
    return f"{stem}_v{version}{ext}"


def next_version_name(name: str) -> str:
    """`Pacchetto` → `Pacchetto_v2`, `Pacchetto_v2` → `Pacchetto_v3`.
    L'estensione .zip, se c'è, resta in fondo."""
    return with_version(name, version_of(name) + 1)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _is_cot_name(name: str) -> bool:
    return name.lower().endswith(".cot")


def _is_manifest_name(name: str) -> bool:
    return name.replace("\\", "/").lower() == MANIFEST_PATH.lower()


# ----------------------------------------------------------------------
# Apertura sicura
# ----------------------------------------------------------------------


def _check_entry_name(name: str) -> None:
    if not name or "\x00" in name:
        raise DataPackageError("voce dello zip con nome vuoto o non valido")
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise DataPackageError(f"percorso assoluto nello zip: {name}")
    if any(part == ".." for part in normalized.split("/")):
        raise DataPackageError(f"percorso con '..' nello zip (zip-slip): {name}")


def open_package(source: bytes | str | os.PathLike, check_size: bool = True) -> zipfile.ZipFile:
    """Apre lo zip in sola lettura dopo averne controllato dimensione, numero
    di voci, nomi (niente assoluti né `..`) e cifratura. `source` è il
    contenuto oppure il percorso del file: col percorso si legge solo
    l'indice dello zip e i file XML, non le mappe.

    check_size=False salta i limiti di dimensione (non quelli sulle voci):
    per la sola analisi di un file su disco, dove una mappa offline da GB non
    viene mai letta."""
    try:
        size = len(source) if isinstance(source, (bytes, bytearray)) else os.path.getsize(source)
    except OSError as e:
        raise DataPackageError(f"file non leggibile: {e}") from e
    if check_size and size > MAX_PACKAGE_BYTES:
        raise DataPackageError(
            f"pacchetto troppo grande ({size // (1024 * 1024)} MB, massimo {MAX_PACKAGE_BYTES // (1024 * 1024)} MB)"
        )
    try:
        zf = zipfile.ZipFile(io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, OSError) as e:
        raise DataPackageError(f"zip non valido: {e}") from e

    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        raise DataPackageError(f"troppe voci nello zip ({len(infos)}, massimo {MAX_ENTRIES})")
    total = 0
    seen = set()
    for info in infos:
        _check_entry_name(info.filename)
        if info.flag_bits & 0x1:
            raise DataPackageError(f"voce cifrata nello zip: {info.filename}")
        if info.filename in seen:
            raise DataPackageError(f"voce duplicata nello zip: {info.filename}")
        seen.add(info.filename)
        total += info.file_size
    if check_size and total > MAX_UNCOMPRESSED_BYTES:
        raise DataPackageError(
            f"contenuto decompresso troppo grande ({total // (1024 * 1024)} MB, massimo {MAX_UNCOMPRESSED_BYTES // (1024 * 1024)} MB)"
        )
    return zf


def _read_xml_entry(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    if info.file_size > MAX_XML_ENTRY_BYTES:
        raise DataPackageError(f"{info.filename} troppo grande per un file XML ({info.file_size} byte)")
    try:
        return zf.read(info)
    except (zipfile.BadZipFile, OSError, ValueError, EOFError) as e:
        raise DataPackageError(f"{info.filename} illeggibile: {e}") from e


# ----------------------------------------------------------------------
# Analisi
# ----------------------------------------------------------------------


def _prolog_end(raw: bytes) -> int:
    """Indice del tag <event>: prima c'è solo il prologo (dichiarazione, PI)."""
    m = _EVENT_TAG_RE.search(raw)
    return m.start() if m else len(raw)


def analyze_cot(raw: bytes, now: datetime) -> dict:
    """Stato di un singolo .cot. Non solleva: un XML rotto diventa STATUS_ERROR."""
    has_bom = raw.startswith(BOM)
    body = raw[len(BOM):] if has_bom else raw
    pis = [m.group(0).strip().decode("utf-8", "replace") for m in _PI_RE.finditer(body[: _prolog_end(body)])]
    result = {
        "uid": None,
        "type": None,
        "callsign": None,
        "time": None,
        "stale": None,
        "bom": has_bom,
        "processing_instructions": pis,
        "status": STATUS_ERROR,
        "error": None,
    }
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        result["error"] = f"XML non parsabile: {e}"
        return result
    if _local(root.tag) != "event":
        result["error"] = f"non è un evento CoT (radice <{_local(root.tag)}>)"
        return result

    result["uid"] = root.get("uid")
    result["type"] = root.get("type")
    result["time"] = root.get("time")
    result["stale"] = root.get("stale")
    for el in root.iter():
        if _local(el.tag) == "contact" and el.get("callsign"):
            result["callsign"] = el.get("callsign")
            break

    if not result["uid"]:
        result["error"] = "evento senza uid"
        return result
    stale = parse_cot_time(result["stale"])
    if stale is None:
        result["error"] = f"stale mancante o non leggibile ({result['stale']!r})"
        return result

    if stale <= now:
        result["status"] = STATUS_EXPIRED
    elif stale <= now + EXPIRING_WITHIN:
        result["status"] = STATUS_EXPIRING
    elif has_bom or pis:
        result["status"] = STATUS_DIRTY
    else:
        result["status"] = STATUS_OK
    return result


def _parse_manifest(raw: bytes) -> dict:
    """name, uid e voci (zipEntry, ignore) del manifest v2."""
    body = raw[len(BOM):] if raw.startswith(BOM) else raw
    root = ET.fromstring(body)
    info = {"name": None, "uid": None, "version": root.get("version"), "entries": []}
    for el in root:
        if _local(el.tag) == "Configuration":
            for param in el:
                if _local(param.tag) == "Parameter" and param.get("name") in ("name", "uid"):
                    info[param.get("name")] = param.get("value")
        elif _local(el.tag) == "Contents":
            for content in el:
                if _local(content.tag) == "Content" and content.get("zipEntry"):
                    info["entries"].append(
                        {
                            "zip_entry": content.get("zipEntry"),
                            "ignore": (content.get("ignore") or "false").lower() == "true",
                        }
                    )
    return info


def analyze(data: bytes | str | os.PathLike, now: datetime | None = None) -> dict:
    """Analizza un data package. Solleva DataPackageError solo se lo zip è
    da rifiutare in blocco; i problemi dei singoli file finiscono nel report."""
    # Su disco si leggono solo indice e XML: i limiti di dimensione servono
    # per il contenuto in memoria e per la riparazione, che ricostruisce lo zip
    with open_package(data, check_size=isinstance(data, (bytes, bytearray))) as zf:
        return _analyze(zf, now or _utcnow())


def _analyze(zf: zipfile.ZipFile, now: datetime) -> dict:
    files = [i for i in zf.infolist() if not i.is_dir()]
    names = {i.filename for i in files}

    manifest = None
    issues = []
    manifest_info = next((i for i in files if _is_manifest_name(i.filename)), None)
    if manifest_info is None:
        issues.append({"status": STATUS_ERROR, "message": "manifest assente (MANIFEST/manifest.xml)"})
    else:
        try:
            manifest = _parse_manifest(_read_xml_entry(zf, manifest_info))
        except (ET.ParseError, DataPackageError) as e:
            issues.append({"status": STATUS_ERROR, "message": f"manifest non leggibile: {e}"})

    entities, other_files = [], []
    for info in files:
        if _is_manifest_name(info.filename):
            continue
        if _is_cot_name(info.filename):
            try:
                entity = analyze_cot(_read_xml_entry(zf, info), now)
            except DataPackageError as e:
                entity = {"uid": None, "type": None, "callsign": None, "time": None, "stale": None,
                          "bom": False, "processing_instructions": [], "status": STATUS_ERROR, "error": str(e)}
            entity["path"] = info.filename
            entity["size"] = info.file_size
            entities.append(entity)
        else:
            other_files.append({"path": info.filename, "size": info.file_size})

    if manifest is not None:
        listed = {e["zip_entry"] for e in manifest["entries"]}
        for entry in manifest["entries"]:
            if entry["zip_entry"] not in names:
                issues.append({"status": STATUS_ERROR, "message": f"voce del manifest senza file: {entry['zip_entry']}"})
        for info in files:
            if not _is_manifest_name(info.filename) and info.filename not in listed:
                issues.append({"status": STATUS_ERROR, "message": f"file non elencato nel manifest: {info.filename}"})

    counts = {s: 0 for s in (STATUS_OK, STATUS_EXPIRING, STATUS_EXPIRED, STATUS_DIRTY, STATUS_ERROR)}
    for e in entities:
        counts[e["status"]] += 1
    repairable = [e for e in entities if e["status"] in (STATUS_EXPIRED, STATUS_EXPIRING, STATUS_DIRTY)]

    return {
        "manifest": manifest,
        "entities": entities,
        "files": other_files,
        "issues": issues,
        "counts": counts,
        "cot_total": len(entities),
        "needs_repair": bool(repairable),
        "summary": summarize(counts, len(entities), issues),
        "analyzed_at": now.isoformat(),
    }


def summarize(counts: dict, total: int, issues: list) -> str:
    """Badge testuale: «4 scadute su 7», «tutte valide (3)», …"""
    if not total and not issues:
        return "nessuna entità CoT"
    parts = []
    if counts.get(STATUS_EXPIRED):
        parts.append(f"{counts[STATUS_EXPIRED]} scadute su {total}")
    if counts.get(STATUS_EXPIRING):
        parts.append(f"{counts[STATUS_EXPIRING]} in scadenza")
    if counts.get(STATUS_ERROR):
        parts.append(f"{counts[STATUS_ERROR]} non leggibili")
    if counts.get(STATUS_DIRTY):
        parts.append(f"{counts[STATUS_DIRTY]} con BOM/PI")
    if issues:
        parts.append(f"{len(issues)} problemi di struttura")
    if not parts:
        return f"tutte valide ({total})" if total else "nessuna entità CoT"
    return " · ".join(parts)


# ----------------------------------------------------------------------
# Riparazione
# ----------------------------------------------------------------------


def _set_attr(tag: bytes, name: bytes, value: bytes) -> bytes:
    pattern = re.compile(rb"(\s" + name + rb"\s*=\s*)([\"'])[^\"']*\2")
    if pattern.search(tag):
        return pattern.sub(lambda m: m.group(1) + m.group(2) + value + m.group(2), tag, count=1)
    # Attributo assente: aggiunto prima della chiusura del tag
    close = tag.rfind(b"/>") if tag.endswith(b"/>") else len(tag) - 1
    return tag[:close].rstrip() + b" " + name + b'="' + value + b'"' + tag[close:]


def clean_cot(raw: bytes, new_times: tuple[str, str, str] | None) -> bytes:
    """Toglie BOM e processing instruction dal prologo e, se `new_times`
    (time, start, stale) è dato, li riscrive sul tag <event>. Il resto dei
    byte resta identico."""
    body = raw[len(BOM):] if raw.startswith(BOM) else raw
    end = _prolog_end(body)
    body = _PI_RE.sub(b"", body[:end]) + body[end:]
    if new_times:
        m = _EVENT_TAG_RE.search(body)
        if not m:
            raise DataPackageError("tag <event> non trovato")
        tag = m.group(0)
        for name, value in zip((b"time", b"start", b"stale"), new_times):
            tag = _set_attr(tag, name, value.encode("ascii"))
        body = body[: m.start()] + tag + body[m.end():]
    return body


def _rewrite_manifest(raw: bytes, new_name: str, new_uid: str, add_entries: list[str] = ()) -> bytes:
    body = raw[len(BOM):] if raw.startswith(BOM) else raw
    root = ET.fromstring(body)
    config = next((el for el in root if _local(el.tag) == "Configuration"), None)
    if config is None:
        config = ET.Element("Configuration")
        root.insert(0, config)
    found = set()
    for param in config:
        if _local(param.tag) == "Parameter" and param.get("name") in ("name", "uid"):
            param.set("value", new_name if param.get("name") == "name" else new_uid)
            found.add(param.get("name"))
    for key, value in (("uid", new_uid), ("name", new_name)):
        if key not in found:
            ET.SubElement(config, "Parameter", {"name": key, "value": value})
    if add_entries:
        contents = next((el for el in root if _local(el.tag) == "Contents"), None)
        if contents is None:
            contents = ET.SubElement(root, "Contents")
        for entry in add_entries:
            ET.SubElement(contents, "Content", {"ignore": "false", "zipEntry": entry})
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode").encode("utf-8")


def new_manifest(name: str, uid: str, entries: list[str]) -> bytes:
    """Manifest v2 minimo, come lo scrive ATAK: name, uid e una Content per file."""
    return _rewrite_manifest(b'<MissionPackageManifest version="2"/>', name, uid, entries)


def repair(
    data: bytes | str | os.PathLike,
    years: int = 5,
    mode: str = MODE_EXPIRED,
    now: datetime | None = None,
    new_name: str | None = None,
    new_uid: str | None = None,
) -> tuple[bytes, dict]:
    """Costruisce un nuovo zip riparato. Ritorna (zip, report); il report
    è anche l'anteprima «prima/dopo» (basta non salvare lo zip).

    mode=MODE_EXPIRED rinnova solo le entità scadute o in scadenza entro 7
    giorni; MODE_ALL tutte. BOM e `<?visible?>` si tolgono comunque da ogni
    .cot. Le entità non leggibili vengono copiate senza modifiche.
    """
    if mode not in (MODE_EXPIRED, MODE_ALL):
        raise DataPackageError(f"modalità non valida: {mode}")
    if not isinstance(years, int) or isinstance(years, bool) or not 1 <= years <= 50:
        raise DataPackageError("la durata deve essere un numero intero di anni fra 1 e 50")
    now = (now or _utcnow()).astimezone(timezone.utc)
    with open_package(data) as zf:
        return _repair(zf, years, mode, now, new_name, new_uid)


def _repair(zf, years, mode, now, new_name, new_uid) -> tuple[bytes, dict]:
    before = _analyze(zf, now)

    old_name = (before["manifest"] or {}).get("name") or "DataPackage"
    new_name = new_name or next_version_name(old_name)
    new_uid = new_uid or str(uuid.uuid4())
    stamp = format_cot_time(now)
    new_times = (stamp, stamp, format_cot_time(add_years(now, years)))

    by_path = {e["path"]: e for e in before["entities"]}
    changes, skipped = [], []
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zf.infolist():
            target = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            target.compress_type = zipfile.ZIP_DEFLATED
            target.external_attr = info.external_attr
            if info.is_dir():
                zout.writestr(target, b"")
                continue
            if _is_manifest_name(info.filename) and before["manifest"] is not None:
                zout.writestr(target, _rewrite_manifest(_read_xml_entry(zf, info), new_name, new_uid))
                continue
            entity = by_path.get(info.filename)
            if entity is None:
                # Mappe, PDF, immagini, manifest illeggibile: copia identica a blocchi
                with zf.open(info) as src, zout.open(target, "w") as dst:
                    while chunk := src.read(1024 * 1024):
                        dst.write(chunk)
                continue
            raw = _read_xml_entry(zf, info)
            if entity["status"] == STATUS_ERROR:
                zout.writestr(target, raw)
                skipped.append({"path": info.filename, "error": entity["error"]})
                continue
            renew = mode == MODE_ALL or entity["status"] in (STATUS_EXPIRED, STATUS_EXPIRING)
            fixed = clean_cot(raw, new_times if renew else None)
            check = analyze_cot(fixed, now)
            if check["status"] == STATUS_ERROR or check["uid"] != entity["uid"]:
                raise DataPackageError(f"riparazione di {info.filename} non verificabile: {check['error'] or 'uid cambiato'}")
            zout.writestr(target, fixed)
            changes.append(
                {
                    "path": info.filename,
                    "uid": entity["uid"],
                    "type": entity["type"],
                    "callsign": entity["callsign"],
                    "status_before": entity["status"],
                    "status_after": check["status"],
                    "stale_before": entity["stale"],
                    "stale_after": check["stale"],
                    "renewed": renew,
                    "bom_removed": entity["bom"],
                    "pi_removed": entity["processing_instructions"],
                }
            )

    result = out.getvalue()
    return result, {
        "old_name": old_name,
        "new_name": new_name,
        "old_uid": (before["manifest"] or {}).get("uid"),
        "new_uid": new_uid,
        "years": years,
        "mode": mode,
        "new_stale": new_times[2],
        "changes": changes,
        "skipped": skipped,
        "renewed": sum(1 for c in changes if c["renewed"]),
        "size": len(result),
    }


# ----------------------------------------------------------------------
# File allegati (PDF, immagini, documenti da consultare)
# ----------------------------------------------------------------------

# Limiti dei file aggiunti dalla tab Data Package. Il pacchetto di partenza
# può essere grande (mappa offline): si copia a blocchi, mai in memoria.
MAX_ADDED_FILES = 50
MAX_ADDED_FILE_BYTES = 500 * 1024 * 1024

_UNSAFE_NAME_RE = re.compile(r"[^\w.\-()' ]+")


def safe_file_name(name: str) -> str:
    """Nome del file dentro lo zip: solo il basename, niente separatori o
    caratteri strani, estensione conservata. Le lettere accentate restano."""
    base = re.split(r"[\\/]", name or "")[-1]
    base = _UNSAFE_NAME_RE.sub("_", base).strip(" ._") or "file"
    stem, ext = os.path.splitext(base)
    return stem[:120] + ext[:16] if len(base) > 136 else base


def add_files(
    source: str | os.PathLike | None,
    out_path: str | os.PathLike,
    files: list[tuple[str, str | os.PathLike]],
    new_name: str,
    new_uid: str | None = None,
) -> dict:
    """Scrive in `out_path` un nuovo data package: il contenuto di `source`
    (None = pacchetto nuovo, vuoto) più `files` = [(nome, percorso locale)].

    Ogni file va in una cartella propria `<uid>/<nome>` come fa ATAK, così
    due file con lo stesso nome non si pestano, ed è elencato nel manifest.
    Name e uid del manifest cambiano sempre: ATAK non deve riusare il
    pacchetto già importato. Le voci esistenti sono copiate identiche,
    compressione compresa."""
    if not files:
        raise DataPackageError("nessun file da aggiungere")
    if len(files) > MAX_ADDED_FILES:
        raise DataPackageError(f"troppi file in una volta ({len(files)}, massimo {MAX_ADDED_FILES})")
    for name, path in files:
        size = os.path.getsize(path)
        if size > MAX_ADDED_FILE_BYTES:
            raise DataPackageError(
                f"{name}: {size // (1024 * 1024)} MB, massimo {MAX_ADDED_FILE_BYTES // (1024 * 1024)} MB per file"
            )
    new_uid = new_uid or str(uuid.uuid4())
    added = []
    for name, path in files:
        clean = safe_file_name(name)
        added.append({"name": clean, "entry": f"{uuid.uuid4()}/{clean}", "size": os.path.getsize(path), "path": path})

    existing = _write_package(source, out_path, new_name, new_uid, added)
    names = set(existing)
    return {
        "new_name": new_name,
        "new_uid": new_uid,
        "added": [
            {"name": a["name"], "entry": a["entry"], "size": a["size"], "duplicate": a["name"] in names}
            for a in added
        ],
        "size": os.path.getsize(out_path),
    }


def rename_package(source: str | os.PathLike, out_path: str | os.PathLike, new_name: str) -> dict:
    """Riscrive il data package con il name del manifest cambiato: è quello
    che ATAK mostra una volta installato. L'uid del manifest resta lo stesso,
    così ATAK riconosce il pacchetto e reimportandolo sostituisce quello già
    installato invece di affiancarne un secondo. Tutto il resto è copiato
    identico; senza manifest se ne crea uno (con uid nuovo)."""
    with open_package(source, check_size=False) as zf:
        manifest_info = next((i for i in zf.infolist() if _is_manifest_name(i.filename)), None)
        uid = None
        if manifest_info is not None:
            try:
                uid = _parse_manifest(_read_xml_entry(zf, manifest_info)).get("uid")
            except ET.ParseError as e:
                raise DataPackageError(f"manifest non leggibile: {e}") from e
    uid = uid or str(uuid.uuid4())
    _write_package(source, out_path, new_name, uid, [])
    return {"new_name": new_name, "uid": uid, "size": os.path.getsize(out_path)}


def _write_package(source, out_path, new_name: str, new_uid: str, added: list[dict]) -> list[str]:
    """Scrive `out_path`: manifest con name/uid dati (più le voci di `added`),
    le voci di `source` copiate identiche a blocchi, poi i file di `added`.
    Ritorna i basename delle voci copiate da `source`."""
    entries = [a["entry"] for a in added]
    existing = []
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zout:
        if source is None:
            zout.writestr(MANIFEST_PATH, new_manifest(new_name, new_uid, entries))
        else:
            with open_package(source, check_size=False) as zf:
                infos = zf.infolist()
                if len(infos) + len(added) > MAX_ENTRIES:
                    raise DataPackageError(f"troppe voci nello zip (massimo {MAX_ENTRIES})")
                manifest_info = next((i for i in infos if _is_manifest_name(i.filename)), None)
                files_in_zip = [i.filename for i in infos if not i.is_dir() and not _is_manifest_name(i.filename)]
                if manifest_info is None:
                    # Pacchetto senza manifest: se ne scrive uno che elenca tutto
                    zout.writestr(MANIFEST_PATH, new_manifest(new_name, new_uid, files_in_zip + entries))
                else:
                    try:
                        raw = _read_xml_entry(zf, manifest_info)
                        manifest = _rewrite_manifest(raw, new_name, new_uid, entries)
                    except ET.ParseError as e:
                        raise DataPackageError(f"manifest non leggibile: {e}") from e
                    target = zipfile.ZipInfo(manifest_info.filename, date_time=manifest_info.date_time)
                    target.compress_type = zipfile.ZIP_DEFLATED
                    zout.writestr(target, manifest)
                for info in infos:
                    if _is_manifest_name(info.filename):
                        continue
                    target = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                    target.compress_type = info.compress_type
                    target.external_attr = info.external_attr
                    if info.is_dir():
                        zout.writestr(target, b"")
                        continue
                    with zf.open(info) as src, zout.open(target, "w", force_zip64=True) as dst:
                        while chunk := src.read(1024 * 1024):
                            dst.write(chunk)
                    existing.append(os.path.basename(info.filename))
        for a in added:
            zout.write(a["path"], a["entry"])
    return existing
