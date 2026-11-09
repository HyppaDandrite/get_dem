"""Offline regression tests: run with python -m unittest discover -s tests -v."""

from contextlib import redirect_stdout
import io
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import dem_tools as d
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import nbformat
import numpy as np
import rasterio
from rasterio.transform import from_origin

ROOT = Path(__file__).resolve().parents[1]


class DemTests(unittest.TestCase):
    def setUp(self):
        # All disposable data stays in the known project workspace.
        self.temp = TemporaryDirectory(prefix="verify_", dir=ROOT)
        self.folder = Path(self.temp.name)

    def tearDown(self):
        plt.close("all")
        assert self.folder.resolve().is_relative_to(ROOT)
        self.temp.cleanup()

    def tile(self, name="source.tif", data=None, x=2614000, top=1089200,
             resolution=2, epsg=2056):
        if data is None:
            data = np.full((100, 100), 2500, dtype="float32")
        path = self.folder / name
        with rasterio.open(path, "w", driver="GTiff", height=data.shape[0],
                           width=data.shape[1], count=1, dtype="float32", crs=f"EPSG:{epsg}",
                           transform=from_origin(x, top, resolution, resolution), nodata=d.NODATA) as dst:
            dst.write(data.astype("float32"), 1)
        return path

    def test_invalid_rectangles(self):
        cases = [None, 4, "rectangle", (), [1, 2, 3], [1, 2, 3, 4, 5],
                 [True, 1089000, 2620000, 1095000], ["2614000", 1089000, 2620000, 1095000],
                 [float("nan"), 1089000, 2620000, 1095000],
                 [2614000, 1089000, float("inf"), 1095000],
                 [10**1000, 1089000, 2620000, 1095000],
                 [2620000, 1089000, 2614000, 1095000],
                 [2614000, 1095000, 2620000, 1089000],
                 [2614000, 1089000, 2614000, 1095000],
                 [7.6, 45.9, 7.7, 46.0], [614000, 89000, 620000, 95000],
                 [2614000, 1089000, 2614099, 1090000],
                 [2614000, 1089000, 2624001, 1090000]]
        for aoi in cases:
            with self.subTest(aoi=aoi), self.assertRaises(ValueError):
                d.validate_aoi(aoi)

    def test_valid_limits_and_alignment(self):
        self.assertEqual(d.validate_aoi(d.CERVIN), d.CERVIN)
        for side in [100, 10000]:
            d.validate_aoi([2614000, 1089000, 2614000 + side, 1089000 + side])
        self.assertEqual(d.aligned_bounds((2614001, 1089001, 2614101, 1089101)),
                         (2614000, 1089000, 2614125, 1089125))

    def item(self, year=2019, tx=2614, ty=1089, resolution="2", gsd=2):
        item_id = f"swissalti3d_{year}_{tx}-{ty}"
        filename = f"{item_id}_{resolution}_2056_5728.tif"
        asset = SimpleNamespace(href=f"https://data.geo.admin.ch/{filename}", extra_fields={"gsd": gsd})
        return SimpleNamespace(id=item_id, assets={"dem": asset})

    def test_tile_selection_resolution_latest_and_exact_overlap(self):
        bounds = (2614000, 1089000, 2615000, 1090000)
        items = [self.item(), self.item(year=2023), self.item(year=2023),
                 self.item(resolution="0.5", gsd=0.5), self.item(tx=2615), self.item(ty=1090)]
        selected = d.select_tiles(items, bounds)
        self.assertEqual(len(selected), 1)
        self.assertIn("2023", selected[0]["filename"])
        self.assertEqual(d.select_tiles([self.item(resolution="0.5", gsd=0.5)], bounds), [])
        self.assertEqual(d.select_tiles([self.item(gsd=0.5)], bounds), [])
        self.assertEqual(len(d.select_tiles([self.item(gsd=None)], bounds)), 1)

    def test_catalog_empty_and_network_failure(self):
        with patch.object(d.Client, "open") as client:
            client.return_value.search.return_value.items.return_value = iter([])
            with self.assertRaisesRegex(ValueError, "No 2 m"):
                d.find_tiles(d.CERVIN)
            client.side_effect = ConnectionError("offline")
            with self.assertRaisesRegex(RuntimeError, "catalog"):
                d.find_tiles(d.CERVIN)

    def test_corrupt_wrong_crs_and_wrong_resolution_tiles(self):
        good = self.tile()
        self.assertTrue(d.valid_tile(good))
        self.assertFalse(d.valid_tile(good, "1220" + "0" * 64))
        digest = d.hashlib.sha256(good.read_bytes()).hexdigest()
        self.assertTrue(d.valid_tile(good, "1220" + digest))
        wrong = self.tile("wrong.tif", resolution=25)
        self.assertFalse(d.valid_tile(wrong))
        self.assertFalse(d.valid_tile(self.tile("crs.tif", epsg=4326)))
        damaged = self.folder / "damaged.tif"
        damaged.write_bytes(good.read_bytes()[:100])
        self.assertFalse(d.valid_tile(damaged))
        with self.assertRaisesRegex(ValueError, "2 m LV95"):
            d.create_grid([wrong], (2614000, 1089000, 2614200, 1089200))

    def test_download_repair_and_reuse(self):
        good = self.tile()
        entry = {"filename": "download.tif", "url": "https://data.geo.admin.ch/example.tif"}
        cache = self.folder / "cache"
        cache.mkdir()
        (cache / entry["filename"]).write_bytes(b"incomplete")
        with patch.object(d.requests, "Session") as session:
            response = session.return_value.__enter__.return_value.get.return_value.__enter__.return_value
            response.iter_content.return_value = [good.read_bytes()]
            paths = d.download_tiles([entry], cache, progress=lambda _: None)
            self.assertTrue(d.valid_tile(paths[0]))
            self.assertFalse(list(cache.glob("*.part")))
        with patch.object(d.requests, "Session") as session:
            d.download_tiles([entry], cache, progress=lambda _: None)
            session.return_value.__enter__.return_value.get.assert_not_called()

    def test_interrupted_download_is_not_cached(self):
        entry = {"filename": "broken.tif", "url": "https://data.geo.admin.ch/example.tif"}
        def interrupted(_):
            yield b"partial download"
            raise d.requests.ConnectionError("interrupted")
        with patch.object(d.requests, "Session") as session:
            response = session.return_value.__enter__.return_value.get.return_value.__enter__.return_value
            response.iter_content.side_effect = interrupted
            with self.assertRaisesRegex(RuntimeError, "Could not download"):
                d.download_tiles([entry], self.folder, progress=lambda _: None)
        self.assertFalse((self.folder / "broken.tif").exists())
        self.assertFalse((self.folder / "broken.part").exists())

    def test_grid_averaging_at_tile_boundary(self):
        a = self.tile("a.tif", data=np.full((50, 50), 1000), x=2614000, top=1089100)
        b = self.tile("b.tif", data=np.full((50, 50), 2000), x=2614100, top=1089100)
        dem, transform = d.create_grid([a, b], (2614000, 1089000, 2614200, 1089100))
        self.assertEqual(dem.shape, (4, 8))
        np.testing.assert_allclose(dem[:, :4], 1000)
        np.testing.assert_allclose(dem[:, 4:], 2000)
        self.assertEqual((transform.a, transform.e), (25, -25))

    def test_unaligned_rectangle_keeps_real_coverage(self):
        path = self.tile()
        dem, transform = d.create_grid([path], (2614001, 1089001, 2614101, 1089101))
        self.assertEqual(dem.shape, (5, 5))
        np.testing.assert_allclose(dem, 2500)
        self.assertEqual(tuple(rasterio.transform.array_bounds(*dem.shape, transform)),
                         (2614000, 1089000, 2614125, 1089125))

    def test_empty_and_missing_elevations(self):
        with self.assertRaises(ValueError):
            d.create_grid([], d.CERVIN)
        path = self.tile(data=np.full((100, 100), d.NODATA))
        with self.assertRaisesRegex(ValueError, "No valid"):
            d.create_grid([path], (2614000, 1089000, 2614200, 1089200))
        with self.assertRaises(ValueError):
            d.grid_info(np.full((4, 4), np.nan), from_origin(0, 100, 25, 25))

    def test_hillshade_lighting_and_gaps(self):
        rows, cols = np.indices((8, 8))
        transform = from_origin(2614000, 1089200, 25, 25)
        dem = (2500 + cols * 25 - rows * 50).astype(float)
        rgba = d.hillshade(dem, transform)
        # z = x + 2*y: surface normal (-1,-2,1), NW sunlight (-0.5,0.5,sqrt(.5)).
        expected = max(0, (0.5 - 1 + np.sqrt(0.5)) / np.sqrt(6))
        np.testing.assert_allclose(rgba[:, :, 0], expected)
        np.testing.assert_equal(rgba[:, :, 3], 1)
        flat = d.hillshade(np.full((8, 8), 2500), transform)
        np.testing.assert_allclose(flat[:, :, 0], np.sqrt(0.5))
        dem[3:5, 3:5] = d.NODATA
        rgba = d.hillshade(dem, transform)
        self.assertEqual(rgba[3, 3, 3], 0)
        self.assertEqual(rgba[2, 3, 3], 0)
        self.assertEqual(rgba[0, 0, 3], 1)

    def test_export_roundtrip_and_zip(self):
        dem = np.arange(64, dtype="float32").reshape(8, 8) + 2500.123
        dem[2:4, 2:4] = d.NODATA
        transform = from_origin(2614000, 1089200, 25, 25)
        (self.folder / "old.tif").write_text("old output")
        info, rgba, archive = d.export_outputs(dem, transform, self.folder)
        with rasterio.open(self.folder / "dem_25m_lv95.asc") as src:
            np.testing.assert_allclose(src.read(1), dem, atol=0.001, rtol=0)
            self.assertEqual(src.transform, transform)
            self.assertEqual(src.crs.to_epsg(), 2056)
            self.assertEqual(src.nodata, d.NODATA)
        self.assertLessEqual(info["ZMin"], info["elevation_min"] - 100)
        self.assertGreaterEqual(info["ZMax"], info["elevation_max"] + 100)
        self.assertEqual(info["coverage_percent"], 93.75)
        png = plt.imread(self.folder / "hillshade_25m_lv95.png")
        np.testing.assert_allclose(png, rgba, atol=1/255)
        self.assertEqual(png.shape, (8, 8, 4))
        world = list(map(float, (self.folder / "hillshade_25m_lv95.pgw").read_text().splitlines()))
        self.assertEqual(world, [25, 0, 0, -25, 2614012.5, 1089187.5])
        with ZipFile(archive) as zipped:
            self.assertEqual(len(zipped.namelist()), 6)
            self.assertFalse(any(name.endswith(".tif") for name in zipped.namelist()))
        self.assertFalse((self.folder / "dem_25m_lv95.tif").exists())

    def ga25_section(self, feature_id="GA25_107_Plate_II_Section_4",
                     host="api3.geo.admin.ch", geometry=None):
        match = re.search(r"Plate_(\w+?)_Section_(\d+)", feature_id)
        plate, number = match.groups() if match else ("I", "1")
        return {"id": feature_id, "geometry": geometry or {"type": "MultiLineString",
                "coordinates": [[[2614050, 1089050], [2614150, 1089150]]]},
                "properties": {"ga25_no": 107, "ga25_name": "Matterhorn (LK 1347)", "plate_no": plate,
                               "section_no": number, "section_type_en": "Geological", "vert_exag": 1.0,
                               "author": "K. Bucher", "ga25_edition": 2004,
                               "link_original": f"https://{host}/profile/{feature_id}.pdf"}}

    def response(self, content=b"", content_type="application/json", payload=None):
        return SimpleNamespace(content=content, text=content.decode("latin-1"),
                               headers={"Content-Type": content_type},
                               raise_for_status=lambda: None, json=lambda: payload)

    def test_ga25_map_on_terrain_edges(self):
        from PIL import Image
        stream = io.BytesIO()
        Image.new("RGBA", (40, 40), (10, 20, 30, 255)).save(stream, "PNG")
        aoi = (2614001, 1089001, 2614200, 1089200)
        with patch.object(d.requests, "Session") as session:
            get = session.return_value.__enter__.return_value.get
            get.return_value = self.response(stream.getvalue(), "image/png")
            rgba, transform = d.get_ga25_map(aoi)
            params = get.call_args.kwargs["params"]
            self.assertEqual(params["LAYERS"], "ch.swisstopo.geologie-geologischer_atlas")
            self.assertEqual(params["BBOX"], "2614000,1089000,2614200,1089200")
            self.assertEqual((params["WIDTH"], params["HEIGHT"]), (40, 40))
            self.assertEqual(rgba.shape, (40, 40, 4))
            self.assertEqual(transform, from_origin(2614000, 1089200, 5, 5))
            get.return_value = self.response(b"<ServiceException/>", "text/xml")
            with self.assertRaisesRegex(RuntimeError, "did not return an image"):
                d.get_ga25_map(aoi)
            get.return_value = self.response(stream.getvalue(), "image/png")
            with self.assertRaisesRegex(RuntimeError, "unexpected size"):
                d.get_ga25_map((2614000, 1089000, 2614300, 1089200))
            get.side_effect = d.requests.ConnectionError()
            with self.assertRaisesRegex(RuntimeError, "Cannot download the GA25"):
                d.get_ga25_map(aoi)
            get.side_effect = None
            d.get_geocover_map(aoi)
            self.assertEqual(get.call_args.kwargs["params"]["LAYERS"], "ch.swisstopo.geologie-geocover")

    def test_find_sections_filters_and_sorts(self):
        results = [self.ga25_section(),
                   self.ga25_section("GA25_107_Plate_I_Section_1",
                                     geometry={"type": "LineString",
                                               "coordinates": [[2614000, 1089000], [2614100, 1089100]]}),
                   self.ga25_section("GA25_1_Plate_I_Section_9", host="example.com"),
                   self.ga25_section("../escape"),
                   self.ga25_section("GA25_2_Plate_I_Section_2",
                                     geometry={"type": "Point", "coordinates": [2614000, 1089000]})]
        with patch.object(d.requests, "Session") as session:
            get = session.return_value.__enter__.return_value.get
            get.return_value = self.response(payload={"results": results})
            sections = d.find_sections(d.CERVIN)
            self.assertEqual(get.call_args.kwargs["params"]["layers"],
                             "all:ch.swisstopo.geologie-geologischer_atlas_profile")
            self.assertEqual([s["id"] for s in sections],
                             ["GA25_107_Plate_I_Section_1", "GA25_107_Plate_II_Section_4"])
            self.assertEqual(sections[0]["lines"], [[(2614000.0, 1089000.0), (2614100.0, 1089100.0)]])
            with self.assertRaisesRegex(RuntimeError, "Too many"):
                d.find_sections(d.CERVIN, limit=2)
            get.side_effect = d.requests.ConnectionError()
            with self.assertRaisesRegex(RuntimeError, "sections service"):
                d.find_sections(d.CERVIN)

    def test_section_pdf_download_and_reuse(self):
        section = d._section(self.ga25_section())
        pdf = b"%PDF-1.4 plate %%EOF\n"
        cache = self.folder / "sections"
        cache.mkdir()
        (cache / f"{section['id']}.pdf").write_bytes(b"%PDF truncated")
        with patch.object(d.requests, "Session") as session:
            response = session.return_value.__enter__.return_value.get.return_value.__enter__.return_value
            response.iter_content.return_value = [pdf]
            d.download_sections([section], cache, progress=lambda _: None)
        self.assertEqual(section["path"].read_bytes(), pdf)
        with patch.object(d.requests, "Session") as session:
            d.download_sections([section], cache, progress=lambda _: None)
            session.return_value.__enter__.return_value.get.assert_not_called()
        with patch.object(d.requests, "Session") as session:
            response = session.return_value.__enter__.return_value.get.return_value.__enter__.return_value
            response.iter_content.return_value = [b"<html>not found</html>"]
            other = d._section(self.ga25_section("GA25_107_Plate_II_Section_5"))
            with self.assertRaisesRegex(RuntimeError, "Could not download"):
                d.download_sections([other], cache, progress=lambda _: None)
        self.assertFalse(list(cache.glob("*.part")))
        self.assertFalse((cache / "GA25_107_Plate_II_Section_5.pdf").exists())

    def test_export_with_ga25_map_and_sections(self):
        dem = np.full((8, 8), 2500, dtype="float32")
        transform = from_origin(2614000, 1089200, 25, 25)
        geology = (np.full((40, 40, 4), 200, dtype=np.uint8), from_origin(2614000, 1089200, 5, 5))
        section = d._section(self.ga25_section())
        section["path"] = self.folder / "plate.pdf"
        section["path"].write_bytes(b"%PDF %%EOF")
        _, _, archive = d.export_outputs(dem, transform, self.folder / "out", geology=geology,
                                         sections=[section], geocover=geology)
        out = self.folder / "out"
        world = list(map(float, (out / "geology_ga25_5m_lv95.pgw").read_text().splitlines()))
        self.assertEqual(world, [5, 0, 0, -5, 2614002.5, 1089197.5])
        self.assertEqual(plt.imread(out / "geology_ga25_5m_lv95.png").shape, (40, 40, 4))
        rows = (out / "sections_ga25_lv95.csv").read_text().splitlines()
        self.assertEqual(rows[1], "GA25_107_Plate_II_Section_4,II-4,0,0,2614050.00,1089050.00")
        geojson = d.json.loads((out / "sections_ga25_lv95.geojson").read_text())
        self.assertEqual(geojson["features"][0]["geometry"]["type"], "MultiLineString")
        notes = (out / "project_info.txt").read_text()
        self.assertIn("II-4  GA25_107_Plate_II_Section_4", notes)
        with ZipFile(archive) as zipped:
            names = set(zipped.namelist())
        self.assertIn("layers=ch.swisstopo.geologie-geocover", notes)
        self.assertLessEqual({"geology_geocover_5m_lv95.png", "geology_geocover_5m_lv95.prj",
                              "geology_ga25_5m_lv95.png", "geology_ga25_5m_lv95.pgw",
                              "sections_ga25_lv95.csv", "sections_ga25_lv95.geojson",
                              "sections/GA25_107_Plate_II_Section_4.pdf"}, names)
        self.assertEqual(len(names), 15)
        with patch.object(plt, "show"):
            for maps in [(geology, None), (None, geology), (geology, geology), (None, None)]:
                d.preview(dem, np.ones((8, 8, 4)), transform, maps[0], [section], maps[1])

    def test_notebook_structure_and_offline_execution(self):
        notebook = nbformat.read(ROOT / "notebooks/get_dem_25m.ipynb", as_version=4)
        nbformat.validate(notebook)
        codes = [c.source for c in notebook.cells if c.cell_type == "code"]
        self.assertEqual(sum("AOI_LV95 =" in s for s in codes), 1)
        for c in notebook.cells:
            if c.cell_type == "code":
                self.assertEqual(c.outputs, [])
                compile(c.source, "notebook", "exec")
        path = self.tile()
        for ga25, geocover in [(True, True), (False, True), (True, False), (False, False)]:
            ns = {}
            with self.subTest(ga25=ga25, geocover=geocover), redirect_stdout(io.StringIO()), patch.object(plt, "show"):
                exec(codes[0], ns)
                self.assertEqual(ns["AOI_LV95"], d.CERVIN)
                self.assertIs(ns["GA25"], True)
                self.assertIs(ns["GEOCOVER"], True)
                ns.update(AOI_LV95=(2614000, 1089000, 2614200, 1089200), GA25=ga25, GEOCOVER=geocover)
                exec(codes[1], ns)
                ns["OUTPUT"] = self.folder / f"{ga25}_{geocover}"
                ns["find_tiles"] = lambda _: [{"url": "synthetic test"}]
                ns["download_tiles"] = lambda *_: [path]
                ns["get_ga25_map"] = lambda _: (np.zeros((40, 40, 4), np.uint8),
                                                from_origin(2614000, 1089200, 5, 5))
                ns["get_geocover_map"] = ns["get_ga25_map"]
                ns["find_sections"] = lambda _: [d._section(self.ga25_section())]
                ns["download_sections"] = lambda sections, _: sections
                for source in codes[2:]:
                    exec(source, ns)
                self.assertTrue(ns["archive"].exists())
                self.assertEqual((ns["OUTPUT"] / "sections_ga25_lv95.csv").exists(), ga25)
                self.assertEqual((ns["OUTPUT"] / "geology_geocover_5m_lv95.png").exists(), geocover)

if __name__ == "__main__":
    unittest.main()
