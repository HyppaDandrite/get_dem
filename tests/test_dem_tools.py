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
        dem = (2500 + cols * 25 - rows * 50).astype(float)
        rgba = d.hillshade(dem)
        # z = x + 2*y: surface normal (-1,-2,1), NW sunlight (-0.5,0.5,sqrt(.5)).
        expected = max(0, (0.5 - 1 + np.sqrt(0.5)) / np.sqrt(6))
        np.testing.assert_allclose(rgba[:, :, 0], expected)
        np.testing.assert_equal(rgba[:, :, 3], 1)
        flat = d.hillshade(np.full((8, 8), 2500))
        np.testing.assert_allclose(flat[:, :, 0], np.sqrt(0.5))
        dem[3:5, 3:5] = d.NODATA
        rgba = d.hillshade(dem)
        self.assertEqual(rgba[3, 3, 3], 0)
        self.assertEqual(rgba[2, 3, 3], 0)
        self.assertEqual(rgba[0, 0, 3], 1)

    def test_export_roundtrip_and_zip(self):
        dem = np.arange(64, dtype="float32").reshape(8, 8) + 2500.123
        dem[2:4, 2:4] = d.NODATA
        transform = from_origin(2614000, 1089200, 25, 25)
        (self.folder / "old.tif").write_text("old output")
        info, rgba, archive = d.export_outputs(dem, (2614000, 1089000, 2614200, 1089200), self.folder)
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

    def test_invalid_rotated_areas(self):
        good = (d.CERVIN_CENTER, 6000, 30)
        cases = [(None, 6000, 0), ((2617000,), 6000, 0), ((2617000, "1092000"), 6000, 0),
                 ((2617000, float("nan")), 6000, 0), ((7.6, 46.0), 6000, 0),
                 (d.CERVIN_CENTER, 99, 0), (d.CERVIN_CENTER, 10001, 0),
                 (d.CERVIN_CENTER, (6000, 50), 0), (d.CERVIN_CENTER, (1, 2, 3), 0),
                 (d.CERVIN_CENTER, True, 0), (d.CERVIN_CENTER, 6000, float("inf")),
                 (d.CERVIN_CENTER, 6000, "north"),
                 # Fits north-up, but its rotated corners leave the LV95 range.
                 ((2_404_000, 1_200_000), 8000, 45)]
        d.make_area(*good)
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                d.make_area(*args)

    def test_area_geometry(self):
        area = d.make_area(d.CERVIN_CENTER, (1010, 490), -330)
        self.assertEqual((area.width, area.height, area.azimuth), (1025, 500, 30))
        self.assertTrue(d.make_area(d.CERVIN_CENTER, 6000, 360).north_up)
        corners = area.corners()
        # The top edge faces N30E, so walking along it from upper left to upper right heads N120E.
        (x1, y1), (x2, y2) = corners["upper left"], corners["upper right"]
        self.assertAlmostEqual(np.degrees(np.arctan2(x2 - x1, y2 - y1)) % 360, 120)
        self.assertAlmostEqual(np.hypot(x2 - x1, y2 - y1), 1025)
        np.testing.assert_allclose(np.mean(list(corners.values()), axis=0), d.CERVIN_CENTER)
        u, v = area.to_local(*corners["upper right"])
        np.testing.assert_allclose((u, v), (1025, 500), atol=1e-6)
        np.testing.assert_allclose(area.to_lv95(*area.to_local(2617100, 1092200)),
                                   (2617100, 1092200))
        # The pixel transform maps the outer pixel corners onto the area corners.
        t = area.transform(25)
        np.testing.assert_allclose(d.apply(t, 0, 0), corners["upper left"])
        np.testing.assert_allclose(d.apply(t, 41, 20), corners["lower right"])
        north = d.make_area(d.CERVIN_CENTER, 6000)
        self.assertEqual(north.transform(25), from_origin(2614000, 1095000, 25, 25))
        self.assertEqual(north.envelope(), d.CERVIN)
        self.assertEqual(d.area_from_bounds(d.CERVIN), north)

    def plane_tile(self, width=600):
        rows, cols = np.indices((width, width))
        x, y = 2614000 + 2 * cols + 1, 1090200 - 2 * rows - 1
        return self.tile("plane.tif", data=(x - 2614000) + 2 * (y - 1089000), top=1090200)

    def test_rotated_grid_averages_a_plane_exactly(self):
        path = self.plane_tile()  # z = (x - x0) + 2 (y - y0) over 1.2 x 1.2 km.
        area = d.make_area((2614600, 1089600), 600, 30)
        dem, transform = d.create_grid([path], area)
        self.assertEqual(dem.shape, (24, 24))
        self.assertEqual(transform, from_origin(0, 600, 25, 25))
        col, row = np.meshgrid(np.arange(24) + 0.5, np.arange(24) + 0.5)
        x, y = d.apply(area.transform(25), col, row)
        # 1 m sub-samples approximate the oblique cell edges: within 0.15 m on this 66 degree slope.
        np.testing.assert_allclose(dem, (x - 2614000) + 2 * (y - 1089000), atol=0.15)

    def test_quarter_turn_matches_north_up_grid(self):
        path = self.plane_tile()
        data = np.random.default_rng(1).normal(2500, 300, (600, 600))
        with rasterio.open(path, "r+") as dst:
            dst.write(data.astype("float32"), 1)
        north, _ = d.create_grid([path], d.make_area((2614600, 1089600), 500, 0))
        east, _ = d.create_grid([path], d.make_area((2614600, 1089600), 500, 90))
        np.testing.assert_allclose(np.rot90(north), east, atol=1e-3)
        np.testing.assert_allclose(np.rot90(d.hillshade(north)), d.hillshade(east, 90), atol=1e-9)

    def test_hillshade_lighting_independent_of_rotation(self):
        # Same LV95 plane (z rises northwards); the shade must not depend on the grid turn.
        for azimuth in [0, 30, 90, 200]:
            area = d.make_area(d.CERVIN_CENTER, 200, azimuth)
            col, row = np.meshgrid(np.arange(8) + 0.5, np.arange(8) + 0.5)
            _, y = d.apply(area.transform(25), col, row)
            rgba = d.hillshade(y * 0.5, azimuth)
            expected = (-0.5 * np.cos(np.radians(315)) * np.cos(np.radians(45))
                        + np.sin(np.radians(45))) / np.sqrt(1.25)
            with self.subTest(azimuth=azimuth):
                np.testing.assert_allclose(rgba[:, :, 0], expected)

    def fake_wms(self, params):
        # Colour encodes the LV95 pixel position, to check where each pixel came from.
        self.assertEqual(params["CRS"], "EPSG:2056")
        height, width = int(params["HEIGHT"]), int(params["WIDTH"])
        image = np.zeros((height, width, 4), dtype=np.uint8)
        image[..., 0] = np.arange(width)[None, :] % 256
        image[..., 1] = np.arange(height)[:, None] % 256
        image[..., 3] = 255
        return image

    def test_geology_north_up_and_rotated(self):
        self.assertIsNone(d.get_geology(d.CERVIN, None))
        with self.assertRaisesRegex(ValueError, "GEOLOGY"):
            d.get_geology(d.CERVIN, "geology")
        with self.assertRaisesRegex(ValueError, "divide"):
            d.get_geology(d.CERVIN, "geocover", pixel=7)
        with patch.object(d, "_fetch_png", side_effect=self.fake_wms) as fetch:
            north = d.get_geology(d.make_area(d.CERVIN_CENTER, 1000), "atlas")
            params = fetch.call_args.args[0]
            self.assertEqual(params["LAYERS"], "ch.swisstopo.geologie-geologischer_atlas")
            self.assertEqual(params["BBOX"], "2616500,1091500,2617500,1092500")
            np.testing.assert_array_equal(north["rgba"], north["source"])
            area = d.make_area(d.CERVIN_CENTER, 1000, 45)
            turned = d.get_geology(area, "geocover")
        self.assertEqual(turned["rgba"].shape, (200, 200, 4))
        self.assertTrue((turned["rgba"][..., 3] == 255).all())
        # Upper-left output pixel comes from the source pixel under its LV95 centre.
        x, y = d.apply(area.transform(5), 0.5, 0.5)
        col, row = d.apply(~turned["source_transform"], x, y)
        np.testing.assert_array_equal(turned["rgba"][0, 0, :2], [int(col) % 256, int(row) % 256])

    def test_geology_service_errors(self):
        response = SimpleNamespace(headers={"Content-Type": "text/xml"}, text="<ServiceException/>",
                                   raise_for_status=lambda: None)
        with patch.object(d.requests, "Session") as session:
            session.return_value.__enter__.return_value.get.return_value = response
            with self.assertRaisesRegex(RuntimeError, "did not return an image"):
                d.get_geology(d.make_area(d.CERVIN_CENTER, 1000), "geocover")
            session.return_value.__enter__.return_value.get.side_effect = d.requests.ConnectionError()
            with self.assertRaisesRegex(RuntimeError, "Cannot download the geological map"):
                d.get_geology(d.make_area(d.CERVIN_CENTER, 1000), "geocover")

    def test_rotated_export_uses_local_frame(self):
        area = d.make_area(d.CERVIN_CENTER, 200, 30)
        dem = np.arange(64, dtype="float32").reshape(8, 8) + 2500
        with patch.object(d, "_fetch_png", side_effect=self.fake_wms):
            geology = d.get_geology(area, "geocover")
        info, rgba, archive = d.export_outputs(dem, area, self.folder, geology=geology)
        self.assertEqual((info["XMin"], info["XMax"], info["YMin"], info["YMax"]), (0, 200, 0, 200))
        with rasterio.open(self.folder / "dem_25m_local.asc") as src:
            np.testing.assert_allclose(src.read(1), dem)
            self.assertIsNone(src.crs)
        world = list(map(float, (self.folder / "geology_5m_local.pgw").read_text().splitlines()))
        self.assertEqual(world, [5, 0, 0, -5, 2.5, 197.5])
        self.assertEqual(plt.imread(self.folder / "geology_5m_local.png").shape, (40, 40, 4))
        with ZipFile(archive) as zipped:
            names = set(zipped.namelist())
        self.assertEqual(names, {"dem_25m_local.asc", "hillshade_25m_local.png",
                                 "hillshade_25m_local.pgw", "geology_5m_local.png",
                                 "geology_5m_local.pgw", "project_info.txt"})
        notes = (self.folder / "project_info.txt").read_text()
        self.assertIn("azimuth 30 degrees", notes)
        self.assertIn("ch.swisstopo.geologie-geocover", notes)
        # The written formula maps the LV95 upper-right corner onto local (200, 200).
        formula = next(line for line in notes.splitlines() if line.strip().startswith("X = ("))
        x0, c, y0, s = map(float, re.findall(r"[-\d.]+(?=\))|(?<=\* )[-\d.]+", formula))
        e, n = area.corners()["upper right"]
        self.assertAlmostEqual((e - x0) * c - (n - y0) * s, 200, places=3)

    def test_notebook_structure_and_offline_execution(self):
        notebook = nbformat.read(ROOT / "notebooks/get_dem_25m.ipynb", as_version=4)
        nbformat.validate(notebook)
        codes = [c.source for c in notebook.cells if c.cell_type == "code"]
        for name in ["CENTER_LV95 =", "SIDE_M =", "AZIMUTH_DEG =", "GEOLOGY ="]:
            self.assertEqual(sum(name in s for s in codes), 1)
        for c in notebook.cells:
            if c.cell_type == "code":
                self.assertEqual(c.outputs, [])
                compile(c.source, "notebook", "exec")
        path = self.tile()
        for azimuth in [0, 30]:
            ns = {}
            with self.subTest(azimuth=azimuth), redirect_stdout(io.StringIO()), \
                    patch.object(plt, "show"), patch.object(d, "_fetch_png", side_effect=self.fake_wms):
                exec(codes[0], ns)
                self.assertEqual(d.make_area(ns["CENTER_LV95"], ns["SIDE_M"], ns["AZIMUTH_DEG"]).envelope(),
                                 d.CERVIN)
                self.assertEqual(ns["GEOLOGY"], "geocover")
                ns.update(CENTER_LV95=(2614100, 1089100), SIDE_M=200, AZIMUTH_DEG=azimuth)
                exec(codes[1], ns)
                ns["OUTPUT"] = self.folder / str(azimuth)
                ns["find_tiles"] = lambda _: [{"url": "synthetic test"}]
                ns["download_tiles"] = lambda *_: [path]
                for source in codes[2:]:
                    exec(source, ns)
                self.assertTrue(ns["archive"].exists())
                frame = "lv95" if azimuth == 0 else "local"
                self.assertTrue((ns["OUTPUT"] / f"geology_5m_{frame}.png").exists())


if __name__ == "__main__":
    unittest.main()
