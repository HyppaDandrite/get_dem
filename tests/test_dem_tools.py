"""Offline regression tests: run with python -m unittest discover -s tests -v."""

from contextlib import redirect_stdout
import io
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
        ns = {}
        with redirect_stdout(io.StringIO()), patch.object(plt, "show"):
            exec(codes[0], ns)
            self.assertEqual(ns["AOI_LV95"], d.CERVIN)
            ns["AOI_LV95"] = (2614000, 1089000, 2614200, 1089200)
            exec(codes[1], ns)
            ns["OUTPUT"] = self.folder
            ns["find_tiles"] = lambda _: [{"url": "synthetic test"}]
            ns["download_tiles"] = lambda *_: [path]
            for source in codes[2:]:
                exec(source, ns)
            self.assertTrue(ns["archive"].exists())


if __name__ == "__main__":
    unittest.main()
