"""Analisi e riparazione dei data package WinTAK (modulo datapackage).

La fixture TacticalScoutCodogno.zip è un pacchetto reale creato col Data
Package tool di WinTAK: 4 aree u-d-f con stale 2026-02-25 (creazione + 7
giorni) e 3 marker a-n-G con stale 2027-02-18, tutti con BOM UTF-8, uno con
`<?visible true?>`. È il caso che ATAK-CIV importa a metà («Map Item — Not
Found» per le aree).
"""

import io
import pathlib
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from ots_milsim_companion_plugin import datapackage as dp

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "TacticalScoutCodogno.zip"
# Dopo la scadenza delle aree (25/02/2026) e prima di quella dei marker (18/02/2027)
NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)

AREAS = {
    "2b5e30bf-d86c-4087-b36d-9a2407873a9c",
    "1b4226a6-ed5b-4e96-be18-179de366a4d5",
    "83b5855d-2894-46b2-a010-b1c13c3e2197",
    "fd842c3e-d9e2-464c-a6cd-b95913079ccc",
}
MARKERS = {
    "598c1ed0-373d-4e3b-bd34-ed7d5556aca6",
    "6453aaf6-d468-4c45-a448-97948b96a3a8",
    "44fdf7ae-48d2-4170-85a6-362b744658a5",
}


@pytest.fixture
def package() -> bytes:
    return FIXTURE.read_bytes()


def _zip(entries: dict) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return out.getvalue()


def _entries(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {i.filename: zf.read(i) for i in zf.infolist()}


def _event(uid, type_="u-d-f", stale="2026-02-25T10:57:01.57Z", prolog=b"", bom=False):
    body = (
        b'<?xml version="1.0" encoding="utf-8" standalone="yes"?>\r\n' + prolog
        + f'<event version="2.0" uid="{uid}" type="{type_}" time="2026-02-18T10:57:01.57Z" '
          f'start="2026-02-18T10:57:01.57Z" stale="{stale}" how="h-e">'
          f'<point lat="45.0" lon="9.0" hae="0" ce="9999999" le="9999999"/>'
          f'<detail><contact callsign="{uid}-cs"/></detail></event>'.encode()
    )
    return (dp.BOM + body) if bom else body


def _manifest(entries, name="Pkg", uid="11111111-1111-1111-1111-111111111111"):
    contents = "".join(
        f'<Content zipEntry="{e}" ignore="false"><Parameter name="uid" value="x"/></Content>' for e in entries
    )
    return (
        f'<MissionPackageManifest version="2"><Configuration>'
        f'<Parameter name="name" value="{name}"/><Parameter name="uid" value="{uid}"/>'
        f"</Configuration><Contents>{contents}</Contents></MissionPackageManifest>"
    ).encode()


# ----------------------------------------------------------------------
# Analisi della fixture reale
# ----------------------------------------------------------------------


def test_fixture_analysis(package):
    report = dp.analyze(package, NOW)
    assert report["cot_total"] == 7
    assert report["manifest"]["name"] == "TacticalScout_DP"
    assert report["manifest"]["uid"] == "87e3a6dc-a80a-4037-bdd5-7bb8cbbde2e6"
    assert report["issues"] == []
    assert report["files"] == []

    by_uid = {e["uid"]: e for e in report["entities"]}
    assert set(by_uid) == AREAS | MARKERS
    for uid in AREAS:
        assert by_uid[uid]["type"] == "u-d-f"
        assert by_uid[uid]["status"] == dp.STATUS_EXPIRED
    for uid in MARKERS:
        assert by_uid[uid]["type"] == "a-n-G"
        # Non scaduti, ma con il BOM di WinTAK
        assert by_uid[uid]["status"] == dp.STATUS_DIRTY
    assert all(e["bom"] for e in report["entities"])
    assert by_uid["1b4226a6-ed5b-4e96-be18-179de366a4d5"]["processing_instructions"] == ["<?visible true?>"]
    assert by_uid["598c1ed0-373d-4e3b-bd34-ed7d5556aca6"]["callsign"] == "Forming Up Point - Castelnuovo"

    assert report["counts"][dp.STATUS_EXPIRED] == 4
    assert report["summary"].startswith("4 scadute su 7")
    assert report["needs_repair"]


def test_now_is_injectable(package):
    # Il 20/02/2026 le aree erano ancora vive ma in scadenza (stale il 25)
    report = dp.analyze(package, datetime(2026, 2, 20, tzinfo=timezone.utc))
    assert report["counts"][dp.STATUS_EXPIRED] == 0
    assert report["counts"][dp.STATUS_EXPIRING] == 4
    # Nel 2028 è scaduto tutto
    report = dp.analyze(package, datetime(2028, 1, 1, tzinfo=timezone.utc))
    assert report["counts"][dp.STATUS_EXPIRED] == 7
    assert report["summary"].startswith("7 scadute su 7")


def test_parse_cot_time_variants():
    assert dp.parse_cot_time("2026-02-25T10:57:01.57Z") == datetime(2026, 2, 25, 10, 57, 1, 570000, tzinfo=timezone.utc)
    assert dp.parse_cot_time("2026-02-25T10:57:01Z") == datetime(2026, 2, 25, 10, 57, 1, tzinfo=timezone.utc)
    assert dp.parse_cot_time("2026-02-25T12:57:01+02:00") == datetime(2026, 2, 25, 10, 57, 1, tzinfo=timezone.utc)
    assert dp.parse_cot_time("ieri") is None
    assert dp.parse_cot_time(None) is None


def test_next_version_name():
    assert dp.next_version_name("TacticalScout_DP") == "TacticalScout_DP_v2"
    assert dp.next_version_name("TacticalScout_DP_v2") == "TacticalScout_DP_v3"
    assert dp.next_version_name("Campi.zip") == "Campi_v2.zip"
    assert dp.next_version_name("Campi_v9.zip") == "Campi_v10.zip"
    assert dp.version_of("Campi.zip") == 1
    assert dp.with_version("Campi_v2.zip", 5) == "Campi_v5.zip"


def test_path_source_same_as_bytes(package):
    """L'app passa il percorso in UPLOAD_FOLDER: stesso risultato dei bytes."""
    by_path = dp.analyze(FIXTURE, NOW)
    by_bytes = dp.analyze(package, NOW)
    assert by_path == by_bytes
    fixed, report = dp.repair(str(FIXTURE), now=NOW, new_uid="u")
    assert report["renewed"] == 4
    assert dp.analyze(fixed, NOW)["manifest"]["uid"] == "u"


def test_missing_file_rejected(tmp_path):
    with pytest.raises(dp.DataPackageError):
        dp.analyze(tmp_path / "non-esiste.zip", NOW)


# ----------------------------------------------------------------------
# Riparazione della fixture reale
# ----------------------------------------------------------------------


def test_repair_expired_only(package):
    fixed, report = dp.repair(package, years=5, mode=dp.MODE_EXPIRED, now=NOW)
    after = dp.analyze(fixed, NOW)

    # Stesse voci, stessi uid
    assert set(_entries(fixed)) == set(_entries(package))
    assert {e["uid"] for e in after["entities"]} == AREAS | MARKERS
    assert after["issues"] == []
    assert after["counts"][dp.STATUS_OK] == 7
    assert not after["needs_repair"]

    by_uid = {e["uid"]: e for e in after["entities"]}
    for uid in AREAS:
        assert by_uid[uid]["stale"] == "2031-09-22T12:00:00.000Z"
    # «solo le scadute»: i marker tengono lo stale originale, ma perdono il BOM
    for uid in MARKERS:
        assert by_uid[uid]["stale"].startswith("2027-02-18")
    assert report["renewed"] == 4
    assert report["new_stale"] == "2031-09-22T12:00:00.000Z"


def test_repair_all_and_bytes(package):
    fixed, report = dp.repair(package, years=3, mode=dp.MODE_ALL, now=NOW)
    assert report["renewed"] == 7
    for name, data in _entries(fixed).items():
        if not name.endswith(".cot"):
            continue
        assert not data.startswith(dp.BOM), name
        assert b"<?visible" not in data, name
        root = ET.fromstring(data)  # round-trip parsabile
        assert root.get("time") == root.get("start") == "2026-09-22T12:00:00.000Z"
        assert root.get("stale") == "2029-09-22T12:00:00.000Z"


def test_repair_keeps_everything_else_identical(package):
    """Solo time/start/stale, BOM e PI cambiano: punti, colori, link e il
    resto dei byte restano quelli scritti da WinTAK."""
    fixed, _ = dp.repair(package, years=5, mode=dp.MODE_ALL, now=NOW)
    old, new = _entries(package), _entries(fixed)
    for name in old:
        if not name.endswith(".cot"):
            continue
        a = ET.fromstring(old[name][len(dp.BOM):])
        b = ET.fromstring(new[name])
        for attr in set(a.attrib) - {"time", "start", "stale"}:
            assert a.get(attr) == b.get(attr), (name, attr)
        assert ET.tostring(a.find("detail")) == ET.tostring(b.find("detail"))
        assert ET.tostring(a.find("point")) == ET.tostring(b.find("point"))
        # Tutto ciò che segue il tag <event> è identico byte per byte
        old_tail = old[name][dp._EVENT_TAG_RE.search(old[name]).end():]
        new_tail = new[name][dp._EVENT_TAG_RE.search(new[name]).end():]
        assert old_tail == new_tail, name


def test_repair_manifest_new_name_and_uid(package):
    fixed, report = dp.repair(package, now=NOW)
    manifest = dp.analyze(fixed, NOW)["manifest"]
    assert manifest["name"] == "TacticalScout_DP_v2" == report["new_name"]
    assert manifest["uid"] != "87e3a6dc-a80a-4037-bdd5-7bb8cbbde2e6"
    assert manifest["uid"] == report["new_uid"]
    assert report["old_uid"] == "87e3a6dc-a80a-4037-bdd5-7bb8cbbde2e6"
    # Le voci del manifest non cambiano
    old_entries = dp.analyze(package, NOW)["manifest"]["entries"]
    assert manifest["entries"] == old_entries


def test_repair_is_not_in_place(package):
    before = bytes(package)
    dp.repair(package, now=NOW)
    assert package == before


# ----------------------------------------------------------------------
# Casi sintetici
# ----------------------------------------------------------------------


def test_non_cot_files_are_listed_and_copied():
    tiles = b'<?xml version="1.0"?><customMapSource><name>PCN</name></customMapSource>'
    pdf = b"%PDF-1.4 finto"
    png = bytes(range(256)) * 10
    data = _zip({
        "MANIFEST/manifest.xml": _manifest(["a/a.cot", "maps/pcn.xml", "doc/briefing.pdf", "img/campo.png"]),
        "a/a.cot": _event("a", bom=True),
        "maps/pcn.xml": tiles,
        "doc/briefing.pdf": pdf,
        "img/campo.png": png,
    })
    report = dp.analyze(data, NOW)
    assert report["cot_total"] == 1
    assert {f["path"] for f in report["files"]} == {"maps/pcn.xml", "doc/briefing.pdf", "img/campo.png"}
    assert report["issues"] == []

    fixed, _ = dp.repair(data, now=NOW)
    out = _entries(fixed)
    assert out["maps/pcn.xml"] == tiles
    assert out["doc/briefing.pdf"] == pdf
    assert out["img/campo.png"] == png


def test_package_without_cot():
    data = _zip({"MANIFEST/manifest.xml": _manifest(["m/pcn.xml"]), "m/pcn.xml": b"<customMapSource/>"})
    report = dp.analyze(data, NOW)
    assert report["cot_total"] == 0
    assert not report["needs_repair"]
    assert report["summary"] == "nessuna entità CoT"


def test_manifest_mismatch_both_ways():
    data = _zip({
        "MANIFEST/manifest.xml": _manifest(["a/a.cot", "manca/manca.cot"]),
        "a/a.cot": _event("a", stale="2030-01-01T00:00:00Z"),
        "orfano/orfano.cot": _event("orfano", stale="2030-01-01T00:00:00Z"),
    })
    messages = [i["message"] for i in dp.analyze(data, NOW)["issues"]]
    assert any("senza file" in m and "manca/manca.cot" in m for m in messages)
    assert any("non elencato" in m and "orfano/orfano.cot" in m for m in messages)


def test_missing_manifest_is_reported():
    data = _zip({"a/a.cot": _event("a")})
    report = dp.analyze(data, NOW)
    assert report["manifest"] is None
    assert any("manifest assente" in i["message"] for i in report["issues"])


def test_broken_cot_is_error_and_copied_untouched():
    broken = b'\xef\xbb\xbf<?xml version="1.0"?><event uid="x" stale="2020'
    data = _zip({
        "MANIFEST/manifest.xml": _manifest(["b/b.cot", "a/a.cot"]),
        "b/b.cot": broken,
        "a/a.cot": _event("a"),
    })
    report = dp.analyze(data, NOW)
    by_path = {e["path"]: e for e in report["entities"]}
    assert by_path["b/b.cot"]["status"] == dp.STATUS_ERROR
    assert "non parsabile" in by_path["b/b.cot"]["error"]

    fixed, rep = dp.repair(data, now=NOW)
    assert _entries(fixed)["b/b.cot"] == broken
    assert [s["path"] for s in rep["skipped"]] == ["b/b.cot"]


def test_missing_stale_is_error():
    raw = b'<event uid="x" type="u-d-f" time="2026-01-01T00:00:00Z"><detail/></event>'
    assert dp.analyze_cot(raw, NOW)["status"] == dp.STATUS_ERROR


def test_pi_without_bom_is_dirty():
    raw = _event("a", stale="2030-01-01T00:00:00Z", prolog=b"<?visible true?>\r\n")
    result = dp.analyze_cot(raw, NOW)
    assert result["status"] == dp.STATUS_DIRTY
    cleaned = dp.clean_cot(raw, None)
    assert b"<?visible" not in cleaned
    assert cleaned.startswith(b"<?xml")


def test_leap_day_stale():
    assert dp.add_years(datetime(2028, 2, 29, tzinfo=timezone.utc), 1) == datetime(2029, 2, 28, tzinfo=timezone.utc)


# ----------------------------------------------------------------------
# Zip da rifiutare
# ----------------------------------------------------------------------


@pytest.mark.parametrize("name", ["../evil.cot", "a/../../evil.cot", "/etc/passwd", "C:/Windows/evil.cot", "a\\..\\evil.cot"])
def test_zip_slip_rejected(name):
    data = _zip({"MANIFEST/manifest.xml": _manifest([name]), name: _event("a")})
    with pytest.raises(dp.DataPackageError):
        dp.analyze(data, NOW)
    with pytest.raises(dp.DataPackageError):
        dp.repair(data, now=NOW)


def test_not_a_zip_rejected():
    with pytest.raises(dp.DataPackageError):
        dp.analyze(b"questo non e' uno zip", NOW)


def test_truncated_zip_rejected(package):
    with pytest.raises(dp.DataPackageError):
        dp.analyze(package[: len(package) // 2], NOW)


def test_size_limits(monkeypatch, package):
    monkeypatch.setattr(dp, "MAX_PACKAGE_BYTES", len(package) - 1)
    with pytest.raises(dp.DataPackageError, match="troppo grande"):
        dp.analyze(package, NOW)


def test_uncompressed_limit(monkeypatch):
    data = _zip({"MANIFEST/manifest.xml": _manifest(["big.bin"]), "big.bin": b"\0" * 100_000})
    monkeypatch.setattr(dp, "MAX_UNCOMPRESSED_BYTES", 50_000)
    with pytest.raises(dp.DataPackageError, match="decompresso"):
        dp.analyze(data, NOW)


def test_invalid_repair_parameters(package):
    with pytest.raises(dp.DataPackageError):
        dp.repair(package, years=0, now=NOW)
    with pytest.raises(dp.DataPackageError):
        dp.repair(package, years=True, now=NOW)
    with pytest.raises(dp.DataPackageError):
        dp.repair(package, mode="qualcosa", now=NOW)


def test_size_limits_skipped_for_files_on_disk(monkeypatch, tmp_path, package):
    # Mappa offline da GB: l'analisi di un file su disco legge solo indice e XML
    path = tmp_path / "grande.zip"
    path.write_bytes(package)
    monkeypatch.setattr(dp, "MAX_PACKAGE_BYTES", len(package) - 1)
    monkeypatch.setattr(dp, "MAX_UNCOMPRESSED_BYTES", 1)
    assert dp.analyze(str(path), NOW)["cot_total"] == 7
    # la riparazione ricostruisce lo zip in memoria: i limiti restano
    with pytest.raises(dp.DataPackageError, match="troppo grande"):
        dp.repair(str(path), now=NOW)
