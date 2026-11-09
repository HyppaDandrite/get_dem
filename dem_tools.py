"""Small, testable helpers for the teaching notebook.

swissALTI3D -> 25 m terrain, plus an optional geological map from map.geo.admin.ch,
for a square or rectangle that may be rotated to any orientation.
"""

from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
from io import BytesIO
import math
from numbers import Real
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlparse
from zipfile import ZipFile, ZIP_DEFLATED

# Conda on Windows may launch a notebook without activating its native libraries.
# Leave NOTO / Linux and pip wheels to locate their own GDAL and PROJ data.
if os.name == "nt" and (Path(sys.prefix) / "conda-meta").is_dir():
    _library = Path(sys.prefix) / "Library"
    _dll_handle = os.add_dll_directory(str(_library / "bin"))
    os.environ["PATH"] = str(_library / "bin") + os.pathsep + os.environ.get("PATH", "")
    for _variable, _folder in [("GDAL_DATA", "gdal"), ("PROJ_DATA", "proj")]:
        if (_library / "share" / _folder).is_dir():
            os.environ[_variable] = str(_library / "share" / _folder)
    if "PROJ_DATA" in os.environ:
        os.environ["PROJ_LIB"] = os.environ["PROJ_DATA"]

import numpy as np
import requests
import rasterio
from rasterio.crs import CRS
from rasterio.merge import merge
from rasterio.transform import Affine, array_bounds, from_origin
from rasterio.warp import transform_bounds
from pystac_client import Client
from pystac_client.stac_api_io import StacApiIO
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

RESOLUTION = 25
NODATA = -9999
EPSG = 2056
CERVIN = (2_614_000, 1_089_000, 2_620_000, 1_095_000)
CERVIN_CENTER = (2_617_000, 1_092_000)
MAX_TILES = 300  # A rotated 10 km square spans up to 15 x 15 km of 1 km tiles.
CATALOG = "https://data.geo.admin.ch/api/stac/v1"
COLLECTION = "ch.swisstopo.swissalti3d"
WMS = "https://wms.geo.admin.ch/"
WMS_MAX_PIXELS = 10_000  # Advertised MaxWidth / MaxHeight of the geo.admin.ch WMS.
GEOLOGY_LAYERS = {
    "geocover": "ch.swisstopo.geologie-geocover",            # GeoCover, vector data
    "atlas": "ch.swisstopo.geologie-geologischer_atlas",     # Geological Atlas 1:25 000
}


def apply(transform, x, y):
    """Apply an affine transform to coordinates or arrays (works with any affine version)."""
    return (transform.a * x + transform.b * y + transform.c,
            transform.d * x + transform.e * y + transform.f)


def _finite_numbers(values):
    try:
        return all(not isinstance(v, bool) and isinstance(v, Real) and math.isfinite(v)
                   for v in values)
    except OverflowError:
        return False


@dataclass(frozen=True)
class Area:
    """A rectangle in LV95 around a centre, possibly rotated.

    `azimuth` is the direction the top edge faces, in degrees clockwise from
    north (0 = north-up). Local coordinates (u, v) are metres from the
    lower-left corner, along the bottom edge (u) and the left edge (v).
    """
    x: float
    y: float
    width: float
    height: float
    azimuth: float = 0.0

    @property
    def north_up(self):
        return self.azimuth == 0

    @property
    def frame(self):
        """Coordinates written to the exported files: LV95, or local metres if rotated."""
        return "lv95" if self.north_up else "local"

    def axes(self):
        """LV95 unit vectors of the local u (right) and v (up) axes."""
        if self.north_up:
            return (1.0, 0.0), (0.0, 1.0)
        a = math.radians(self.azimuth)
        return (math.cos(a), -math.sin(a)), (math.sin(a), math.cos(a))

    def origin(self):
        """LV95 position of the lower-left corner, the local origin."""
        (ux, uy), (vx, vy) = self.axes()
        return (self.x - self.width / 2 * ux - self.height / 2 * vx,
                self.y - self.width / 2 * uy - self.height / 2 * vy)

    def to_lv95(self, u, v):
        (ux, uy), (vx, vy) = self.axes()
        x0, y0 = self.origin()
        return x0 + np.multiply(u, ux) + np.multiply(v, vx), y0 + np.multiply(u, uy) + np.multiply(v, vy)

    def to_local(self, x, y):
        (ux, uy), (vx, vy) = self.axes()
        x0, y0 = self.origin()
        dx, dy = np.subtract(x, x0), np.subtract(y, y0)
        return dx * ux + dy * uy, dx * vx + dy * vy

    def corners(self):
        w, h = self.width, self.height
        return {name: tuple(float(c) for c in self.to_lv95(u, v)) for name, u, v in
                [("lower left", 0, 0), ("lower right", w, 0),
                 ("upper right", w, h), ("upper left", 0, h)]}

    def envelope(self):
        """Smallest north-up LV95 rectangle containing the area."""
        xs, ys = zip(*self.corners().values())
        # Rounding removes sin/cos noise such as 1e-10 m at 90 degrees.
        return tuple(round(v, 6) for v in (min(xs), min(ys), max(xs), max(ys)))

    def shape(self, cell):
        return round(self.height / cell), round(self.width / cell)

    def transform(self, cell):
        """Pixel -> LV95 affine transform; rotated when the area is rotated."""
        (ux, uy), (vx, vy) = self.axes()
        left, top = self.to_lv95(0, self.height)
        # "+ 0.0" turns -0.0 into 0.0 in world files and printed transforms.
        return Affine(cell * ux + 0.0, -cell * vx + 0.0, float(left),
                      cell * uy + 0.0, -cell * vy + 0.0, float(top))

    def project_transform(self, cell):
        """Pixel -> coordinates used in the exported files (always north-up in that frame)."""
        if self.north_up:
            return self.transform(cell)
        return from_origin(0, self.height, cell, cell)


def validate_aoi(aoi):
    """Reject malformed, non-LV95 or oversized rectangles before network access."""
    if not isinstance(aoi, (tuple, list)) or len(aoi) != 4:
        raise ValueError("AOI_LV95 must be (xmin, ymin, xmax, ymax).")
    if not _finite_numbers(aoi):
        raise ValueError("AOI_LV95 must contain four finite numbers, in LV95 metres.")
    xmin, ymin, xmax, ymax = map(float, aoi)
    if not (xmin < xmax and ymin < ymax):
        raise ValueError("xmin must be smaller than xmax, and ymin smaller than ymax.")
    if not (2_400_000 <= xmin < xmax <= 2_900_000 and 1_000_000 <= ymin < ymax <= 1_400_000):
        raise ValueError("Use Swiss LV95 metre coordinates, not LV03 or longitude/latitude.")
    if not (100 <= xmax - xmin <= 10_000 and 100 <= ymax - ymin <= 10_000):
        raise ValueError("Each side must be between 100 m and 10 km.")
    return xmin, ymin, xmax, ymax


def aligned_bounds(aoi):
    """Expand to whole 25 m cells; use the same bounds for search and output."""
    xmin, ymin, xmax, ymax = validate_aoi(aoi)
    return (math.floor(xmin / RESOLUTION) * RESOLUTION,
            math.floor(ymin / RESOLUTION) * RESOLUTION,
            math.ceil(xmax / RESOLUTION) * RESOLUTION,
            math.ceil(ymax / RESOLUTION) * RESOLUTION)


def area_from_bounds(aoi):
    """North-up Area from (xmin, ymin, xmax, ymax), expanded to whole 25 m cells."""
    xmin, ymin, xmax, ymax = aligned_bounds(aoi)
    return Area((xmin + xmax) / 2, (ymin + ymax) / 2, xmax - xmin, ymax - ymin)


def make_area(center, size, azimuth=0):
    """Validate a square (size) or rectangle ((width, height)) centred on an LV95 point.

    Sides are rounded up to whole 25 m cells. `azimuth` is the direction the
    top edge faces, in degrees clockwise from north.
    """
    if not isinstance(center, (tuple, list)) or len(center) != 2 or not _finite_numbers(center):
        raise ValueError("CENTER_LV95 must be (x, y): two finite LV95 numbers, in metres.")
    sides = tuple(size) if isinstance(size, (tuple, list)) else (size, size)
    if len(sides) != 2 or not _finite_numbers(sides):
        raise ValueError("SIDE_M must be a number, or (width, height), in metres.")
    if not all(100 <= s <= 10_000 for s in sides):
        raise ValueError("Each side must be between 100 m and 10 km.")
    if not _finite_numbers([azimuth]):
        raise ValueError("AZIMUTH_DEG must be a finite number of degrees.")
    width, height = (math.ceil(float(s) / RESOLUTION) * RESOLUTION for s in sides)
    area = Area(float(center[0]), float(center[1]), width, height, float(azimuth) % 360 + 0.0)
    xmin, ymin, xmax, ymax = area.envelope()
    if not (2_400_000 <= xmin and xmax <= 2_900_000 and 1_000_000 <= ymin and ymax <= 1_400_000):
        raise ValueError("Use Swiss LV95 metre coordinates, not LV03 or longitude/latitude.")
    return area


def as_area(area):
    """Accept an Area, or a north-up (xmin, ymin, xmax, ymax) rectangle."""
    return area if isinstance(area, Area) else area_from_bounds(area)


def source_bounds(area):
    """North-up LV95 bounds on the native 2 m grid that contain the whole area."""
    xmin, ymin, xmax, ymax = as_area(area).envelope()
    return (math.floor(xmin / 2) * 2, math.floor(ymin / 2) * 2,
            math.ceil(xmax / 2) * 2, math.ceil(ymax / 2) * 2)


def select_tiles(items, bounds):
    """Select 2 m LV95/LN02 assets, newest edition per 1 km tile.

    Asset order is not reliable. The official filename encodes resolution,
    horizontal CRS (2056), and height reference (5728 / LN02).
    """
    selected = {}
    for item in items:
        match = re.fullmatch(r"swissalti3d_(\d{4})_(\d{4})-(\d{4})", item.id)
        if not match:
            continue
        year, tx, ty = map(int, match.groups())
        x, y = tx * 1000, ty * 1000
        if x >= bounds[2] or x + 1000 <= bounds[0] or y >= bounds[3] or y + 1000 <= bounds[1]:
            continue
        for asset in item.assets.values():
            url = urlparse(asset.href)
            filename = Path(url.path).name
            if filename != f"{item.id}_2_2056_5728.tif":
                continue
            if url.scheme != "https" or url.hostname != "data.geo.admin.ch":
                continue
            # The filename is authoritative when optional STAC gsd is absent/null.
            gsd = asset.extra_fields.get("gsd")
            if gsd is not None and gsd != 2:
                continue
            key = (tx, ty)
            if key not in selected or year > selected[key][0]:
                selected[key] = (year, {"url": asset.href, "filename": filename,
                    "checksum": asset.extra_fields.get("file:checksum")})
    return [selected[key][1] for key in sorted(selected)]


def find_tiles(area):
    """Query swisstopo. Fail explicitly rather than silently truncate a search."""
    bounds = source_bounds(area)
    bbox = transform_bounds(EPSG, 4326, *bounds, densify_pts=21)
    try:
        catalog = Client.open(CATALOG, stac_io=StacApiIO(timeout=60, max_retries=3))
        items = list(catalog.search(collections=[COLLECTION], bbox=bbox, max_items=2001).items())
    except Exception as exc:
        raise RuntimeError("Cannot reach the swisstopo catalog. Check internet access and retry.") from exc
    if len(items) > 2000:
        raise RuntimeError("Catalog result is too large. Reduce the study area.")
    tiles = select_tiles(items, bounds)
    if not tiles:
        raise ValueError("No 2 m swissALTI3D tiles found. Check the area and Swiss data coverage.")
    if len(tiles) > MAX_TILES:
        raise ValueError(f"More than {MAX_TILES} tiles requested. Reduce the study area.")
    return tiles


def valid_tile(path, checksum=None):
    """Read the full small tile to detect truncated files, and check the grid."""
    try:
        if checksum and checksum.startswith("1220"):
            if hashlib.sha256(Path(path).read_bytes()).hexdigest() != checksum[4:].lower():
                return False
        with rasterio.open(path) as src:
            if (src.count != 1 or src.crs != CRS.from_epsg(EPSG)
                    or not np.allclose(src.res, (2, 2))
                    or src.transform.b != 0 or src.transform.d != 0
                    or src.transform.a <= 0 or src.transform.e >= 0):
                return False
            src.read(1)  # Header-only checks miss damaged raster blocks.
        return True
    except (OSError, ValueError, rasterio.errors.RasterioError):
        return False


def _session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def download_tiles(tiles, cache, progress=print):
    """Reuse validated files; publish a download only after it is complete."""
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    paths = []
    with _session() as session:
        for index, tile in enumerate(tiles, 1):
            path = cache / tile["filename"]
            checksum = tile.get("checksum")
            if not path.exists() or not valid_tile(path, checksum):
                partial = path.with_suffix(".part")
                try:
                    with session.get(tile["url"], stream=True, timeout=(30, 120)) as response:
                        response.raise_for_status()
                        with partial.open("wb") as stream:
                            for chunk in response.iter_content(1024 * 1024):
                                stream.write(chunk)
                    if not valid_tile(partial, checksum):
                        raise ValueError("File is damaged or is not a 2 m LV95 tile.")
                    partial.replace(path)
                except (requests.RequestException, OSError, ValueError) as exc:
                    partial.unlink(missing_ok=True)
                    raise RuntimeError(f"Could not download {path.name}. Check network/disk space and rerun.") from exc
            paths.append(path)
            progress(f"Tile {index}/{len(tiles)} ready: {path.name}")
    return paths


def _cell_average(source, source_transform, area, chunk=512):
    """Area-weighted average of 2 m source cells onto (possibly rotated) 25 m cells.

    Each source cell is split into 2 x 2 samples of 1 m. Sample centres never
    sit on a 25 m cell edge, and a source cell cut by an edge counts by area.
    """
    rows, cols = area.shape(RESOLUTION)
    to_cell = ~area.transform(RESOLUTION)
    total = np.zeros(rows * cols)
    count = np.zeros(rows * cols)
    for start in range(0, source.shape[0], chunk):  # Chunks keep memory low on NOTO.
        block = source[start:start + chunk]
        valid = np.isfinite(block) & (block != NODATA)
        for dc in (0.25, 0.75):
            for dr in (0.25, 0.75):
                col, row = np.meshgrid(np.arange(source.shape[1]) + dc,
                                       np.arange(start, start + len(block)) + dr)
                c, r = apply(to_cell, *apply(source_transform, col, row))
                c, r = np.floor(c).astype(np.int64), np.floor(r).astype(np.int64)
                keep = valid & (c >= 0) & (c < cols) & (r >= 0) & (r < rows)
                index = r[keep] * cols + c[keep]
                total += np.bincount(index, weights=block[keep], minlength=rows * cols)
                count += np.bincount(index, minlength=rows * cols)
    dem = np.full(rows * cols, NODATA, dtype="float32")
    dem[count > 0] = total[count > 0] / count[count > 0]
    return dem.reshape(rows, cols)


def create_grid(paths, area):
    """Mosaic at native resolution, then average once onto (possibly rotated) 25 m cells.

    Returns the DEM and its transform in the export frame: LV95 if north-up,
    local metres from the lower-left corner if rotated.
    """
    area = as_area(area)
    if not paths:
        raise ValueError("No source tiles. Run the download step first.")
    with ExitStack() as stack, rasterio.Env(GDAL_CACHEMAX=64 * 1024 * 1024):
        sources = [stack.enter_context(rasterio.open(path)) for path in paths]
        for src in sources:
            if (src.crs != CRS.from_epsg(EPSG) or src.count != 1
                    or not np.allclose(src.res, (2, 2))
                    or src.transform.b != 0 or src.transform.d != 0
                    or src.transform.a <= 0 or src.transform.e >= 0):
                raise ValueError("Source tiles must be north-up 2 m LV95 elevation grids.")
        mosaic, source_transform = merge(sources, bounds=source_bounds(area), res=2,
                                         nodata=NODATA, dtype="float32")
    dem = _cell_average(mosaic[0], source_transform, area)
    if not (dem != NODATA).any():
        raise ValueError("No valid elevations in this area. Check swissALTI3D coverage.")
    return dem, area.project_transform(RESOLUTION)


def hillshade(dem, azimuth=0.0, cell=RESOLUTION):
    """Northwest light at 45 degrees on a grid whose top edge faces `azimuth`.

    Gaps and undefined slopes beside them are transparent.
    """
    z = np.where(np.isfinite(dem) & (dem != NODATA), dem, np.nan).astype(float)
    # Image rows run towards the bottom edge, so use negative spacing for "up" slopes.
    dv, du = np.gradient(z, -cell, cell)
    sun, altitude = np.deg2rad([315 - azimuth, 45])
    su, sv = np.sin(sun) * np.cos(altitude), np.cos(sun) * np.cos(altitude)
    light = (-du * su - dv * sv + np.sin(altitude)) / np.sqrt(1 + du**2 + dv**2)
    alpha = np.isfinite(light) & np.isfinite(z)
    shade = np.clip(np.nan_to_num(light), 0, 1)
    return np.dstack((shade, shade, shade, alpha.astype(float)))


def _fetch_png(params, timeout=(30, 180)):
    """GET one WMS image; the WMS answers errors with XML, so check the type."""
    from PIL import Image

    try:
        with _session() as session:
            response = session.get(WMS, params=params, timeout=timeout)
            response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError("Cannot download the geological map. Check internet access and retry.") from exc
    if not response.headers.get("Content-Type", "").startswith("image/png"):
        raise RuntimeError("map.geo.admin.ch did not return an image: " + response.text[:300])
    with Image.open(BytesIO(response.content)) as image:
        return np.asarray(image.convert("RGBA"))


def get_geology(area, layer="geocover", pixel=5):
    """Download a geology layer from map.geo.admin.ch and resample it onto the area.

    `layer` is "geocover" (GeoCover vector data), "atlas" (Geological Atlas
    1:25 000) or None to skip. The image has `pixel`-metre pixels aligned with
    the terrain: same outer edges, same orientation.
    """
    if layer is None:
        return None
    if layer not in GEOLOGY_LAYERS:
        raise ValueError(f"GEOLOGY must be one of {sorted(GEOLOGY_LAYERS)} or None.")
    area = as_area(area)
    if not (_finite_numbers([pixel]) and pixel > 0):
        raise ValueError("The geology pixel size must be a positive number of metres.")
    rows, cols = area.shape(pixel)
    if not (math.isclose(rows * pixel, area.height) and math.isclose(cols * pixel, area.width)):
        raise ValueError(f"The geology pixel size must divide the sides ({area.width:g} x {area.height:g} m).")
    if max(rows, cols) > 5000:
        raise ValueError("The geology image would exceed 5000 pixels. Use a larger pixel size.")
    # Request the north-up envelope on a pixel-aligned grid, then sample the area from it.
    xmin, ymin, xmax, ymax = area.envelope()
    xmin, ymin = math.floor(xmin / pixel) * pixel, math.floor(ymin / pixel) * pixel
    xmax, ymax = math.ceil(xmax / pixel) * pixel, math.ceil(ymax / pixel) * pixel
    width, height = round((xmax - xmin) / pixel), round((ymax - ymin) / pixel)
    if max(width, height) > WMS_MAX_PIXELS:
        raise ValueError("The geology request is too large for the WMS. Use a larger pixel size.")
    params = {"SERVICE": "WMS", "REQUEST": "GetMap", "VERSION": "1.3.0",
              "LAYERS": GEOLOGY_LAYERS[layer], "STYLES": "", "CRS": f"EPSG:{EPSG}",
              "BBOX": f"{xmin},{ymin},{xmax},{ymax}", "WIDTH": width, "HEIGHT": height,
              "FORMAT": "image/png", "TRANSPARENT": "true"}
    source = _fetch_png(params)
    if source.shape[:2] != (height, width):
        raise RuntimeError("The geological map has an unexpected size. Retry later.")
    source_transform = from_origin(xmin, ymax, pixel, pixel)
    # Nearest neighbour keeps map colours exact; pixel centres coincide when north-up.
    col, row = np.meshgrid(np.arange(cols) + 0.5, np.arange(rows) + 0.5)
    sc, sr = apply(~source_transform, *apply(area.transform(pixel), col, row))
    sc, sr = np.floor(sc).astype(np.int64), np.floor(sr).astype(np.int64)
    inside = (sc >= 0) & (sc < width) & (sr >= 0) & (sr < height)
    rgba = np.zeros((rows, cols, 4), dtype=np.uint8)
    rgba[inside] = source[sr[inside], sc[inside]]
    url = requests.Request("GET", WMS, params=params).prepare().url
    return {"layer": layer, "name": GEOLOGY_LAYERS[layer], "pixel": pixel, "rgba": rgba,
            "source": source, "source_transform": source_transform, "url": url}


def map_link(area, layer_name):
    """map.geo.admin.ch view of the area, to read the legend and identify units."""
    return (f"https://map.geo.admin.ch/#/map?lang=fr&center={area.x:.0f},{area.y:.0f}"
            f"&z=6&layers={layer_name}")


def grid_info(dem, transform):
    """Return project bounds and diagnostic values, excluding NoData."""
    valid = np.isfinite(dem) & (dem != NODATA)
    if not valid.any():
        raise ValueError("No valid elevations to export.")
    low, high = float(dem[valid].min()), float(dem[valid].max())
    left, bottom, right, top = array_bounds(*dem.shape, transform)
    return {"XMin": left, "XMax": right, "YMin": bottom, "YMax": top,
            "ZMin": math.floor(low / 100) * 100 - 100,
            "ZMax": math.ceil(high / 100) * 100 + 100,
            "elevation_min": low, "elevation_max": high,
            "coverage_percent": float(valid.mean() * 100)}


def _write_world(path, transform):
    """World file: pixel size, rotation terms, then the upper-left pixel CENTRE."""
    ul_x, ul_y = apply(transform, 0.5, 0.5)
    world = (transform.a, transform.d, transform.b, transform.e, ul_x, ul_y)
    path.write_text("\n".join(f"{v:.10f}" for v in world) + "\n", encoding="ascii")


def _corner_centres(transform, width, height):
    lines = []
    for label, col, row in [("Upper left", 0, 0), ("Upper right", width - 1, 0),
                            ("Lower left", 0, height - 1), ("Lower right", width - 1, height - 1)]:
        x, y = apply(transform, col + 0.5, row + 0.5)
        lines.append(f"  {label}: pixel ({col}, {row}), X={x:.2f}, Y={y:.2f}")
    return lines


def export_outputs(dem, area, output, tiles=(), geology=None):
    """Write ASCII, PNGs, registration files, project notes and one download ZIP.

    Files use LV95 coordinates for a north-up area, and local metres (origin at
    the lower-left corner) for a rotated one; the file names say which.
    """
    import matplotlib.pyplot as plt

    area = as_area(area)
    if dem.shape != area.shape(RESOLUTION):
        raise ValueError("The terrain grid does not match the study area. Rerun all cells.")
    transform = area.project_transform(RESOLUTION)
    info = grid_info(dem, transform)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    frame = area.frame
    asc = output / f"dem_25m_{frame}.asc"
    png = output / f"hillshade_25m_{frame}.png"
    notes = output / "project_info.txt"
    height, width = dem.shape
    # Explicit ASCII writing avoids driver-dependent sidecars and centre/corner ambiguity.
    header = (f"ncols {width}\nnrows {height}\nxllcorner {info['XMin']:.2f}\n"
              f"yllcorner {info['YMin']:.2f}\ncellsize {RESOLUTION}\nNODATA_value {NODATA}")
    np.savetxt(asc, np.where(np.isfinite(dem), dem, NODATA), fmt="%.3f", header=header, comments="")
    rgba = hillshade(dem, area.azimuth)
    plt.imsave(png, rgba, origin="upper")  # Raw pixels; no plot axes or margins.
    _write_world(png.with_suffix(".pgw"), transform)
    files = [asc, png, png.with_suffix(".pgw")]
    images = [png]
    if geology is not None:
        geo_png = output / f"geology_{geology['pixel']:g}m_{frame}.png"
        plt.imsave(geo_png, geology["rgba"], origin="upper")
        _write_world(geo_png.with_suffix(".pgw"), area.project_transform(geology["pixel"]))
        files += [geo_png, geo_png.with_suffix(".pgw")]
        images.append(geo_png)
    if area.north_up:
        crs_wkt = CRS.from_epsg(EPSG).to_wkt(version="WKT1_ESRI")
        for path in [asc, *images]:
            path.with_suffix(".prj").write_text(crs_wkt, encoding="utf-8")
            files.append(path.with_suffix(".prj"))

    if area.north_up:
        lines = ["GeoModeller project: CH1903+ / LV95 (EPSG:2056), metres; heights LN02."]
    else:
        lines = ["GeoModeller project: LOCAL metres in a rotated frame (see below); heights LN02.",
                 "The files do not use LV95 coordinates. Do not select LV95 in GeoModeller."]
    lines += [f"Grid: {width} columns x {height} rows, 25 m cells."]
    lines += [f"{key} = {info[key]:.2f}" for key in ("XMin", "XMax", "YMin", "YMax", "ZMin", "ZMax")]
    lines += [f"Terrain elevations: {info['elevation_min']:.2f} to {info['elevation_max']:.2f} m",
              f"Valid terrain coverage: {info['coverage_percent']:.1f}%",
              "Z limits include at least 100 m margin; lower ZMin for deeper geology.", ""]
    if not area.north_up:
        (ux, uy), (vx, vy) = area.axes()
        x0, y0 = area.origin()
        cos, sin = ux, -uy
        lines += [f"Rotation: the top edge faces azimuth {area.azimuth:g} degrees (clockwise from north).",
                  "Local frame: origin (0, 0) at the lower-left corner; X along the bottom edge,",
                  "Y along the left edge, in metres. The images' top is the top edge, not north.",
                  "LV95 corners of the area:"]
        lines += [f"  {name}: E={x:.2f}, N={y:.2f}" for name, (x, y) in area.corners().items()]
        lines += ["LV95 (E, N) -> local (X, Y):",
                  f"  X = (E - {x0:.3f}) * {cos:.10f} - (N - {y0:.3f}) * {sin:.10f}",
                  f"  Y = (E - {x0:.3f}) * {sin:.10f} + (N - {y0:.3f}) * {cos:.10f}",
                  "Local (X, Y) -> LV95 (E, N):",
                  f"  E = {x0:.3f} + X * {cos:.10f} + Y * {sin:.10f}",
                  f"  N = {y0:.3f} - X * {sin:.10f} + Y * {cos:.10f}", ""]
    lines += ["X/Y bounds are image OUTER EDGES (same for every PNG). PNG top = top edge.",
              f"{png.name}: corner pixel CENTRES (zero-based column,row):"]
    lines += _corner_centres(transform, width, height)
    if geology is not None:
        geo_t = area.project_transform(geology["pixel"])
        rows, cols = geology["rgba"].shape[:2]
        lines += [f"{images[1].name}: {geology['pixel']:g} m pixels, corner pixel CENTRES:"]
        lines += _corner_centres(geo_t, cols, rows)
        lines += ["", f"Geology layer: {geology['name']} (map.geo.admin.ch WMS).",
                  "Legend and unit descriptions: click the map at",
                  f"  {map_link(area, geology['name'])}",
                  "Transparent pixels have no mapped geology."]
    lines += ["", "Source: Federal Office of Topography swisstopo", "Source tile URLs:"]
    lines += [tile["url"] for tile in tiles]
    if geology is not None:
        lines += ["Geology request:", geology["url"]]
    notes.write_text("\n".join(lines) + "\n", encoding="utf-8")
    files.append(notes)
    # Include only files from this run, never an obsolete output or arbitrary files.
    archive = output / "geomodeller_dem.zip"
    with ZipFile(archive, "w", ZIP_DEFLATED) as zipped:
        for path in files:
            zipped.write(path, path.name)
    return info, rgba, archive


def preview(dem, rgba, area, geology=None):
    """Show location, elevation, shading and geology; exported files are untouched."""
    import matplotlib.pyplot as plt

    area = as_area(area)
    left, bottom, right, top = array_bounds(*dem.shape, area.project_transform(RESOLUTION))
    extent = (left, right, bottom, top)
    fig, axes = plt.subplots(2, 2, figsize=(12, 10), constrained_layout=True)
    ax = axes[0, 0]
    if geology is not None:
        sl, sb, sr, st = array_bounds(*geology["source"].shape[:2], geology["source_transform"])
        ax.imshow(geology["source"], extent=(sl, sr, sb, st), origin="upper")
    corners = list(area.corners().values())
    xs, ys = zip(*(corners + corners[:1]))
    ax.plot(xs, ys, color="black", lw=1.5)
    (tx, ty), (ux, uy) = corners[3], corners[2]  # Upper left and upper right.
    ax.plot([tx, ux], [ty, uy], color="red", lw=3, label="top edge")
    ax.legend(loc="lower right")
    ax.set_aspect("equal")
    ax.xaxis.set_major_locator(plt.MaxNLocator(4))  # Seven-digit LV95 labels need room.
    ax.set_title(f"Location in LV95 (top edge faces {area.azimuth:g}°)")
    ax.set_xlabel("LV95 easting (m)")
    ax.set_ylabel("LV95 northing (m)")
    terrain = axes[0, 1].imshow(np.ma.masked_equal(dem, NODATA), extent=extent,
                                origin="upper", cmap="terrain", interpolation="nearest")
    fig.colorbar(terrain, ax=axes[0, 1], label="Elevation (m, LN02)", shrink=0.8)
    axes[0, 1].set_title("DEM: 25 m cells")
    axes[1, 0].imshow(rgba, extent=extent, origin="upper", interpolation="nearest")
    axes[1, 0].set_title("Hillshade: northwest light")
    axes[1, 1].imshow(rgba, extent=extent, origin="upper", interpolation="nearest")
    if geology is not None:
        axes[1, 1].imshow(geology["rgba"], extent=extent, origin="upper", alpha=0.75)
        axes[1, 1].set_title(f"Geology: {geology['name']}")
    else:
        axes[1, 1].set_title("Geology: not requested")
    label = "LV95" if area.north_up else "Local"
    for ax in axes.flat:
        ax.ticklabel_format(useOffset=False, style="plain")
    for ax in axes.flat[1:]:
        ax.set_xlabel(f"{label} X (m)")
        ax.set_ylabel(f"{label} Y (m)")
    plt.show()
