"""Mappa offline HD da un ordine SkyFi (modulo offline_map).

La parte pura (scelta del sorgente, livelli di zoom, argomenti GDAL,
manifest e zip) gira ovunque. La conversione vera richiede i comandi GDAL
(`apt install gdal-bin`) e la libreria Python osgeo per creare i GeoTIFF di
prova: senza, quei test vengono saltati.
"""

import sqlite3
import sys
import xml.etree.ElementTree as ET
import zipfile

import pytest

from ots_milsim_companion_plugin import offline_map as om


def _band(n, interp, type_="Byte", **stats):
    return {"band": n, "colorInterpretation": interp, "type": type_, **stats}


RGB16_NIR = {
    "bands": [
        _band(1, "Red", "UInt16", mean=900, stdDev=200, minimum=0, maximum=4000),
        _band(2, "Green", "UInt16", mean=1000, stdDev=200, minimum=0, maximum=4000),
        _band(3, "Blue", "UInt16", mean=1100, stdDev=200, minimum=0, maximum=4000),
        _band(4, "Undefined", "UInt16", mean=1200, stdDev=200, minimum=0, maximum=4000, noDataValue=0),
    ]
}
RGBA8 = {"bands": [_band(1, "Red"), _band(2, "Green"), _band(3, "Blue"), _band(4, "Alpha")]}


def _pairs(args, flag):
    return [args[i + 1] for i, a in enumerate(args) if a == flag]


# ----------------------------------------------------------------------
# Sorgente e zoom
# ----------------------------------------------------------------------


def test_pick_source_prefers_cog():
    # il COG è l'originale senza perdita, il view-ready è già JPEG
    order = {"downloadCogUrl": "x", "cogSize": 5, "downloadViewReadyCogUrl": "y", "viewReadyCogSize": 3}
    assert om.pick_source(order) == ("cog", 5)
    assert om.pick_source(order, "view-ready") == ("view-ready", 3)
    assert om.pick_source({"downloadViewReadyCogUrl": "y"}, "cog") == ("view-ready", 0)
    assert om.pick_source({"downloadPayloadUrl": "x"}) is None


# Deliverable SkyFi reale (ordine 26383Z2P): EPSG:3857, pixel dello zoom 20
SKYFI_GT = [1099736.400833335472271, 0.149291070869492, 0, 5637217.996999653056264, 0, -0.149291070869638]


def test_aligned_zoom_detects_skyfi_grid():
    wkt = 'PROJCRS["WGS 84 / Pseudo-Mercator", ID["EPSG",3857]]'
    info = {"coordinateSystem": {"wkt": wkt}, "geoTransform": SKYFI_GT}
    assert om.aligned_zoom(info) == 20
    shifted = {**info, "geoTransform": [SKYFI_GT[0] + 0.05, *SKYFI_GT[1:]]}
    assert om.aligned_zoom(shifted) is None  # origine a mezzo pixel
    utm = {"coordinateSystem": {"wkt": 'PROJCRS["UTM 32N", ID["EPSG",32632]]'}, "geoTransform": SKYFI_GT}
    assert om.aligned_zoom(utm) is None


def test_native_zoom_never_loses_detail():
    # 30 cm a terra a 45° di latitudine ≈ 0,42 m in EPSG:3857: z18 (0,60)
    # perderebbe dettaglio, z19 (0,30) no
    assert om.native_zoom(0.42) == 19
    # risoluzione esatta di un livello (o quasi): non si sale di un livello
    assert om.native_zoom(om.zoom_resolution(18)) == 18
    assert om.native_zoom(om.zoom_resolution(18) * 0.999) == 18
    assert om.native_zoom(0.001) == om.MAX_ZOOM
    with pytest.raises(om.OfflineMapError):
        om.native_zoom(0)


def test_overview_factors_down_to_one_tile():
    assert om.overview_factors(256, 256) == []
    assert om.overview_factors(4299, 3607) == ["2", "4", "8", "16", "32"]


# ----------------------------------------------------------------------
# Argomenti GDAL
# ----------------------------------------------------------------------


def test_translate_16bit_rgb_nir():
    args = om.translate_args(RGB16_NIR)
    # RGB + l'alfa creata da -dstalpha, che nel VRT è la banda 5
    assert _pairs(args, "-b") == ["1", "2", "3", "5"]
    assert _pairs(args, "-ot") == ["Byte"]
    assert args[args.index("-scale_1") + 1: args.index("-scale_1") + 5] == ["400", "1400", "0", "255"]
    assert "-scale_4" in args
    assert "ZOOM_LEVEL_STRATEGY=AUTO" in args
    assert "TILING_SCHEME=GoogleMapsCompatible" in args
    assert "TILE_FORMAT=PNG" in args and not any(a.startswith("QUALITY=") for a in args)  # default senza perdita


def test_translate_tile_formats():
    assert "QUALITY=95" in om.translate_args(RGBA8, "jpeg95")
    args = om.translate_args(RGBA8, "jpeg85")
    assert "TILE_FORMAT=AUTO" in args and "QUALITY=85" in args
    with pytest.raises(om.OfflineMapError):
        om.translate_args(RGBA8, "tiff")


def test_translate_8bit_with_alpha_keeps_values():
    args = om.translate_args(RGBA8)
    assert _pairs(args, "-b") == ["1", "2", "3", "4"]
    assert "-ot" not in args and "-scale_1" not in args


def test_translate_gray_and_missing_interp():
    gray = {"bands": [_band(1, "Gray")]}
    assert _pairs(om.translate_args(gray), "-b") == ["1", "2"]
    assert _pairs(om.translate_args(gray), "-colorinterp") == ["gray,alpha"]
    multi = {"bands": [_band(i, "Undefined") for i in range(1, 6)]}
    assert _pairs(om.translate_args(multi), "-b") == ["1", "2", "3", "6"]
    with pytest.raises(om.OfflineMapError):
        om.translate_args({"bands": []})
    with pytest.raises(om.OfflineMapError):
        om.translate_args({"bands": [_band(1, "Red", "UInt16")]})  # senza statistiche


def test_warp_args():
    args = om.warp_args(RGB16_NIR, om.zoom_resolution(19))
    assert _pairs(args, "-t_srs") == ["EPSG:3857"]
    assert _pairs(args, "-srcnodata") == ["0"]
    assert "-dstalpha" in args and "-tap" in args
    assert "-srcnodata" not in om.warp_args(RGBA8)  # l'alfa del sorgente basta
    assert "-tr" not in om.warp_args(RGBA8)
    assert _pairs(om.warp_args(RGBA8, 0.3, "near"), "-r") == ["near"]


# ----------------------------------------------------------------------
# Esecuzione e pacchetto
# ----------------------------------------------------------------------


def test_run_reads_progress_and_errors():
    seen = []
    script = (
        "import sys, time\n"
        "for p in (0, 10, 50):\n"
        "    sys.stdout.write(f'{p}...'); sys.stdout.flush(); time.sleep(0.1)\n"
        "print('100 - done.')"
    )
    om.run([sys.executable, "-c", script], seen.append)
    assert 0.5 in seen and seen[-1] == 1.0
    with pytest.raises(om.OfflineMapError, match="codice 3"):
        om.run([sys.executable, "-c", "import sys; print('rotto'); sys.exit(3)"])
    with pytest.raises(om.OfflineMapError, match="non eseguibile"):
        om.run(["comando-che-non-esiste-davvero"])


def test_run_survives_non_blocking_pipe(monkeypatch):
    # Dentro OTS (eventlet/gevent) il pipe è non bloccante: os.read senza
    # dati pronti solleva EAGAIN («[Errno 11] Resource temporarily unavailable»)
    real_read = om.os.read
    calls = {"n": 0}

    def flaky_read(fd, n):
        calls["n"] += 1
        if calls["n"] % 2:
            raise BlockingIOError(11, "Resource temporarily unavailable")
        return real_read(fd, n)

    monkeypatch.setattr(om.os, "read", flaky_read)
    monkeypatch.setattr(om.time, "sleep", lambda s: None)
    seen = []
    out = om.run([sys.executable, "-c", "print('0...50...100 - done.')"], seen.append)
    assert "done" in out and seen[-1] == 1.0 and calls["n"] > 2


def test_build_package(tmp_path):
    gpkg = tmp_path / "map.gpkg"
    gpkg.write_bytes(b"finto geopackage")
    digest, size = om.build_package(str(gpkg), str(tmp_path / "p.zip"), "SkyFi-X HD-z19", "SkyFi-X HD")
    assert len(digest) == 64 and size == (tmp_path / "p.zip").stat().st_size
    with zipfile.ZipFile(tmp_path / "p.zip") as zf:
        root = ET.fromstring(zf.read("MANIFEST/manifest.xml"))
        params = {p.get("name"): p.get("value") for p in root.iter("Parameter")}
        entry = root.find("Contents/Content").get("zipEntry")
        assert params["name"] == "SkyFi-X HD-z19"
        assert entry == f"{params['uid']}/SkyFi-X HD.gpkg"
        assert zf.read(entry) == b"finto geopackage"
        assert zf.getinfo(entry).compress_type == zipfile.ZIP_STORED


# ----------------------------------------------------------------------
# Conversione vera (serve GDAL)
# ----------------------------------------------------------------------

gdal = pytest.importorskip("osgeo.gdal", reason="libreria GDAL Python assente") if not om.missing_tools() else None
needs_gdal = pytest.mark.skipif(gdal is None, reason="comandi GDAL assenti")


def _geotiff(path, dtype, bands, alpha):
    import numpy as np
    from osgeo import osr

    gdal.UseExceptions()
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32632)  # UTM 32N, come la zona di Codogno
    w, h = 600, 500
    ds = gdal.GetDriverByName("GTiff").Create(str(path), w, h, bands, dtype)
    ds.SetGeoTransform([555000, 0.3, 0, 5000000, 0, -0.3])
    ds.SetProjection(srs.ExportToWkt())
    yy, xx = np.mgrid[0:h, 0:w]
    inside = (xx + yy) > 150  # angolo vuoto
    for i in range(bands):
        band = ds.GetRasterBand(i + 1)
        if alpha and i == bands - 1:
            band.WriteArray((inside * 255).astype("uint8"))
            band.SetColorInterpretation(gdal.GCI_AlphaBand)
            continue
        value = (300 + ((xx // 10 + yy // 10) % 2) * 2000 + i * 50) if dtype != gdal.GDT_Byte else (50 + i * 40)
        band.WriteArray((value * inside).astype("uint16" if dtype != gdal.GDT_Byte else "uint8"))
        if not alpha:
            band.SetNoDataValue(0)
        if i < 3:
            band.SetColorInterpretation([gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand][i])
    ds = None


@needs_gdal
@pytest.mark.parametrize("kind", ["cog16", "viewready"])
def test_convert_end_to_end(tmp_path, kind):
    src = tmp_path / "src.tif"
    if kind == "cog16":
        _geotiff(src, gdal.GDT_UInt16, 4, alpha=False)
    else:
        _geotiff(src, gdal.GDT_Byte, 4, alpha=True)
    gpkg = tmp_path / "map.gpkg"
    phases = []
    result = om.convert(str(src), str(gpkg), str(tmp_path), on_step=lambda p, f: phases.append(p), tile_format="png")
    assert result["native_zoom"] == result["zoom"] == 19
    assert {"analisi", "conversione", "livelli di zoom"} <= set(phases)

    db = sqlite3.connect(gpkg)
    table = db.execute("select table_name from gpkg_contents").fetchone()[0]
    top = db.execute("select max(zoom_level) from gpkg_tile_matrix where table_name=?", (table,)).fetchone()[0]
    pixel = db.execute(
        "select pixel_x_size from gpkg_tile_matrix where table_name=? and zoom_level=?", (table, top)
    ).fetchone()[0]
    assert top == 19 and pixel == pytest.approx(om.zoom_resolution(19))
    kinds = {bytes(d[:2]) for (d,) in db.execute(f"select tile_data from '{table}' where zoom_level=19")}
    assert kinds == {b"\x89P"}  # PNG senza perdita
    db.close()

    ds = gdal.Open(str(gpkg))
    data = ds.ReadAsArray()
    assert ds.RasterCount == 4
    assert (data[3] == 0).any() and (data[3] == 255).any()  # bordi trasparenti
    assert data[:3][:, data[3] > 0].mean() > 20  # non tutto nero dopo lo stretch


@needs_gdal
def test_convert_jpeg_tiles(tmp_path):
    src = tmp_path / "src.tif"
    _geotiff(src, gdal.GDT_Byte, 4, alpha=True)
    gpkg = tmp_path / "map.gpkg"
    assert om.convert(str(src), str(gpkg), str(tmp_path), tile_format="jpeg85")["tile_format"] == "jpeg85"
    db = sqlite3.connect(gpkg)
    table = db.execute("select table_name from gpkg_contents").fetchone()[0]
    kinds = {bytes(d[:2]) for (d,) in db.execute(f"select tile_data from '{table}' where zoom_level=19")}
    db.close()
    assert b"\xff\xd8" in kinds  # JPEG dove l'immagine è piena, PNG solo ai bordi


@needs_gdal
def test_convert_zoom_cap(tmp_path):
    src = tmp_path / "src.tif"
    _geotiff(src, gdal.GDT_Byte, 4, alpha=True)
    result = om.convert(str(src), str(tmp_path / "map.gpkg"), str(tmp_path), max_zoom=17, tile_format="png")
    assert result == {**result, "native_zoom": 19, "zoom": 17}


@needs_gdal
def test_convert_aligned_source_is_pixel_identical(tmp_path):
    # Come i deliverable SkyFi: EPSG:3857, pixel dello zoom 20, origine sulla griglia
    import numpy as np
    from osgeo import osr

    gdal.UseExceptions()
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(3857)
    src = tmp_path / "aligned.tif"
    w, h = 700, 600
    ds = gdal.GetDriverByName("GTiff").Create(str(src), w, h, 3, gdal.GDT_Byte)
    ds.SetGeoTransform(SKYFI_GT)
    ds.SetProjection(srs.ExportToWkt())
    rng = np.random.default_rng(1)
    data = rng.integers(1, 256, size=(3, h, w), dtype=np.uint8)  # rumore: qualsiasi filtro si vedrebbe
    for i in range(3):
        ds.GetRasterBand(i + 1).WriteArray(data[i])
        ds.GetRasterBand(i + 1).SetColorInterpretation([gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand][i])
    ds = None

    gpkg = tmp_path / "map.gpkg"
    result = om.convert(str(src), str(gpkg), str(tmp_path), tile_format="png")
    assert result["aligned"] and result["resampling"] == "near" and result["zoom"] == 20
    assert result["ground_resolution"] == pytest.approx(0.1054, abs=1e-3)  # 10,5 cm a Codogno

    out = gdal.Open(str(gpkg))
    gt = out.GetGeoTransform()
    ox, oy = round((SKYFI_GT[0] - gt[0]) / gt[1]), round((SKYFI_GT[3] - gt[3]) / gt[5])
    assert np.array_equal(out.ReadAsArray(ox, oy, w, h)[:3], data)  # PNG: pixel identici


@needs_gdal
def test_convert_geotiff_is_pixel_identical(tmp_path):
    import numpy as np
    from osgeo import osr

    gdal.UseExceptions()
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(3857)
    src = tmp_path / "aligned.tif"
    w, h = 700, 600
    ds = gdal.GetDriverByName("GTiff").Create(str(src), w, h, 3, gdal.GDT_Byte)
    ds.SetGeoTransform(SKYFI_GT)
    ds.SetProjection(srs.ExportToWkt())
    data = np.random.default_rng(2).integers(1, 256, size=(3, h, w), dtype=np.uint8)
    for i in range(3):
        ds.GetRasterBand(i + 1).WriteArray(data[i])
        ds.GetRasterBand(i + 1).SetColorInterpretation([gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand][i])
    ds = None

    tif = tmp_path / "map.tif"
    result = om.convert(str(src), str(tif), str(tmp_path), tile_format="geotiff")
    assert result["aligned"] and result["tile_format"] == "geotiff"
    out = gdal.Open(str(tif))
    assert out.GetMetadata("IMAGE_STRUCTURE").get("COMPRESSION") == "DEFLATE"
    assert out.GetRasterBand(1).GetOverviewCount() >= 1
    gt = out.GetGeoTransform()
    ox, oy = round((SKYFI_GT[0] - gt[0]) / gt[1]), round((SKYFI_GT[3] - gt[3]) / gt[5])
    assert np.array_equal(out.ReadAsArray(ox, oy, w, h)[:3], data)


# ----------------------------------------------------------------------
# Stato della vegetazione (NDVI)
# ----------------------------------------------------------------------

SKYFI_COG_INFO = {"bands": [_band(1, "Red"), _band(2, "Green"), _band(3, "Blue"), _band(4, "Undefined")]}


def test_red_nir_bands():
    assert om.red_nir_bands(SKYFI_COG_INFO) == (1, 4)  # quarta banda senza etichetta = nir (metadati SkyFi)
    described = {"bands": [_band(1, "Red"), _band(2, "Undefined", description="NIR")]}
    assert om.red_nir_bands(described) == (1, 2)
    with pytest.raises(om.OfflineMapError, match="infrarossa"):
        om.red_nir_bands(RGBA8)  # view-ready: niente infrarosso


def test_vegetation_stops_relative_and_color_table():
    absolute = om.vegetation_stops("assoluta")
    assert absolute == om.VEGETATION_STOPS
    rel = om.vegetation_stops("relativa", 0.094, 0.504)
    xs = [x for x, _ in rel]
    assert xs == sorted(xs)
    assert xs[-1] == pytest.approx(0.504)  # il verde più scuro sul 98° percentile della scena
    assert [c for _, c in rel] == [c for _, c in absolute]  # stessi colori, valori ridistribuiti
    table = om.color_table(rel).splitlines()
    assert table[0] == "nv 0 0 0 0" and table[-1] == "1.0001 0 0 0 0"  # NaN e inf fuori area trasparenti
    with pytest.raises(om.OfflineMapError):
        om.vegetation_stops("relativa")


def test_build_package_with_extra_files(tmp_path):
    tif = tmp_path / "map.tif"
    tif.write_bytes(b"tiff")
    legend = tmp_path / "leg.png"
    legend.write_bytes(b"png")
    om.build_package(str(tif), str(tmp_path / "p.zip"), "X", "X", ".tif", [(str(legend), "Legenda vegetazione.png")])
    with zipfile.ZipFile(tmp_path / "p.zip") as zf:
        entries = [c.get("zipEntry") for c in ET.fromstring(zf.read("MANIFEST/manifest.xml")).iter("Content")]
        assert entries[0].endswith("/X.tif") and entries[1].endswith("/Legenda vegetazione.png")
        assert zf.read(entries[1]) == b"png"


@needs_gdal
def test_vegetation_end_to_end(tmp_path):
    import numpy as np
    from osgeo import osr

    gdal.UseExceptions()
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(3857)
    src = tmp_path / "cog4.tif"
    w, h = 400, 300
    ds = gdal.GetDriverByName("GTiff").Create(str(src), w, h, 4, gdal.GDT_Byte)
    ds.SetGeoTransform(SKYFI_GT)
    ds.SetProjection(srs.ExportToWkt())
    red = np.full((h, w), 40, np.uint8)
    nir = np.full((h, w), 40, np.uint8)
    nir[:, 100:200] = 120   # NDVI 0,5: vegetazione
    red[:, 200:300] = 120   # NDVI -0,5: acqua
    red[:, 300:], nir[:, 300:] = 0, 0  # fuori area
    for i, (band, interp) in enumerate([(red, gdal.GCI_RedBand), (red, gdal.GCI_GreenBand), (red, gdal.GCI_BlueBand), (nir, gdal.GCI_Undefined)]):
        ds.GetRasterBand(i + 1).WriteArray(band)
        ds.GetRasterBand(i + 1).SetColorInterpretation(interp)
    ds = None

    out = tmp_path / "veg.tif"
    result = om.vegetation(str(src), str(out), str(tmp_path), scale="assoluta")
    assert result["product"] == "vegetation" and result["aligned"] and result["zoom"] == 20
    img = gdal.Open(str(out))
    gt = img.GetGeoTransform()
    ox, oy = round((SKYFI_GT[0] - gt[0]) / gt[1]), round((SKYFI_GT[3] - gt[3]) / gt[5])
    a = img.ReadAsArray(ox, oy, w, h)
    soil, veg, water, outside = (a[:, 150, x] for x in (50, 150, 250, 350))
    assert tuple(soil[:3]) == (120, 90, 70)          # NDVI 0 = suolo nudo
    assert veg[1] > veg[0] and veg[1] > veg[2]       # NDVI 0,5 = verde
    assert water[2] > water[0]                       # NDVI -0,5 = blu
    assert soil[3] == veg[3] == water[3] == 255 and outside[3] == 0  # fuori area trasparente

    rel = om.vegetation(str(src), str(tmp_path / "veg_rel.tif"), str(tmp_path), scale="relativa")
    # unica vegetazione della scena a NDVI 0,5 (istogramma a classi da ~0,008)
    assert rel["ndvi_range"][0] == pytest.approx(0.5, abs=0.01)

    legend = tmp_path / "legenda.png"
    om.vegetation_legend(str(legend), rel["stops"], "relativa", "Stato della vegetazione · prova", "immagine del 6 aprile 2023")
    assert legend.stat().st_size > 1000
