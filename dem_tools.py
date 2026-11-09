"""Small, testable helpers for the teaching notebook.

swissALTI3D -> 25 m terrain, plus the GA25 and GeoCover geological maps and GA25 cross-sections.
"""

from contextlib import ExitStack
import csv
import hashlib
from io import BytesIO
import json
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
from rasterio.transform import array_bounds, from_origin
from rasterio.warp import reproject, Resampling, transform_bounds
from pystac_client import Client
from pystac_client.stac_api_io import StacApiIO
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

RESOLUTION = 25
NODATA = -9999
EPSG = 2056
CERVIN = (2_614_000, 1_089_000, 2_620_000, 1_095_000)
CATALOG = "https://data.geo.admin.ch/api/stac/v1"
COLLECTION = "ch.swisstopo.swissalti3d"
WMS = "https://wms.geo.admin.ch/"
IDENTIFY = "https://api3.geo.admin.ch/rest/services/api/MapServer/identify"
GA25_MAP = "ch.swisstopo.geologie-geologischer_atlas"            # Geological Atlas 1:25 000
GEOCOVER_MAP = "ch.swisstopo.geologie-geocover"                  # GeoCover vector data
MAP_NAMES = {GA25_MAP: "GA25", GEOCOVER_MAP: "GeoCover"}
GA25_SECTIONS = "ch.swisstopo.geologie-geologischer_atlas_profile"  # its cross-sections
GEOLOGY_PIXEL = 5  # metres; divides the 25 m cells, so map and terrain share outer edges.


def validate_aoi(aoi):
    """Reject malformed, non-LV95 or oversized rectangles before network access."""
    if not isinstance(aoi, (tuple, list)) or len(aoi) != 4:
        raise ValueError("AOI_LV95 must be (xmin, ymin, xmax, ymax).")
    try:
        finite = all(not isinstance(v, bool) and isinstance(v, Real) and math.isfinite(v) for v in aoi)
    except OverflowError:
        finite = False
    if not finite:
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


def find_tiles(aoi):
    """Query swisstopo. Fail explicitly rather than silently truncate a search."""
    bounds = aligned_bounds(aoi)
    bbox = transform_bounds(EPSG, 4326, *bounds, densify_pts=21)
    try:
        catalog = Client.open(CATALOG, stac_io=StacApiIO(timeout=60, max_retries=3))
        items = list(catalog.search(collections=[COLLECTION], bbox=bbox, max_items=1001).items())
    except Exception as exc:
        raise RuntimeError("Cannot reach the swisstopo catalog. Check internet access and retry.") from exc
    if len(items) > 1000:
        raise RuntimeError("Catalog result is too large. Reduce the study area.")
    tiles = select_tiles(items, bounds)
    if not tiles:
        raise ValueError("No 2 m swissALTI3D tiles found. Check the area and Swiss data coverage.")
    if len(tiles) > 200:
        raise ValueError("More than 200 tiles requested. Reduce the study area.")
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


def get_ga25_map(aoi, pixel=GEOLOGY_PIXEL):
    """GA25 Geological Atlas 1:25 000 (scanned sheets, with symbols and labels)."""
    return get_geology_map(aoi, GA25_MAP, pixel)


def get_geocover_map(aoi, pixel=GEOLOGY_PIXEL):
    """GeoCover (harmonised vector geology, clean colours, wider coverage)."""
    return get_geology_map(aoi, GEOCOVER_MAP, pixel)


def get_geology_map(aoi, layer, pixel=GEOLOGY_PIXEL):
    """Download a geological map as RGBA, on the terrain's exact outer edges.

    Areas the map does not cover yet are transparent.
    """
    from PIL import Image

    name = MAP_NAMES[layer]
    left, bottom, right, top = aligned_bounds(aoi)
    width, height = round((right - left) / pixel), round((top - bottom) / pixel)
    params = {"SERVICE": "WMS", "REQUEST": "GetMap", "VERSION": "1.3.0", "LAYERS": layer,
              "STYLES": "", "CRS": f"EPSG:{EPSG}", "BBOX": f"{left},{bottom},{right},{top}",
              "WIDTH": width, "HEIGHT": height, "FORMAT": "image/png", "TRANSPARENT": "true"}
    try:
        with _session() as session:
            response = session.get(WMS, params=params, timeout=(30, 180))
            response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Cannot download the {name} geological map. Check internet access and retry.") from exc
    # The WMS reports errors as XML with status 200, so check the content type.
    if not response.headers.get("Content-Type", "").startswith("image/png"):
        raise RuntimeError("map.geo.admin.ch did not return an image: " + response.text[:300])
    with Image.open(BytesIO(response.content)) as image:
        rgba = np.asarray(image.convert("RGBA"))
    if rgba.shape[:2] != (height, width):
        raise RuntimeError(f"The {name} map has an unexpected size. Retry later.")
    return rgba, from_origin(left, top, pixel, pixel)


def _section(feature):
    """Keep what students need from one GA25 cross-section feature."""
    p, geometry = feature["properties"], feature["geometry"]
    lines = {"LineString": [geometry["coordinates"]],
             "MultiLineString": geometry["coordinates"]}.get(geometry["type"])
    url = urlparse(p.get("link_original") or "")
    if (lines is None or not re.fullmatch(r"[A-Za-z0-9_.-]+", str(feature["id"]))
            or url.scheme != "https" or url.hostname != "api3.geo.admin.ch"):
        return None
    return {"id": str(feature["id"]), "sheet": f"GA25 {p.get('ga25_no')} {p.get('ga25_name')}",
            "plate": p.get("plate_no"), "section": p.get("section_no"),
            "type": p.get("section_type_en"), "vert_exag": p.get("vert_exag"),
            "author": p.get("author"), "edition": p.get("ga25_edition"), "url": url.geturl(),
            "lines": [[(float(v[0]), float(v[1])) for v in line] for line in lines]}


def find_sections(aoi, limit=200):
    """GA25 cross-sections whose trace crosses the rectangle (may extend beyond it)."""
    left, bottom, right, top = aligned_bounds(aoi)
    params = {"geometryType": "esriGeometryEnvelope", "geometry": f"{left},{bottom},{right},{top}",
              "layers": f"all:{GA25_SECTIONS}", "sr": EPSG, "tolerance": 0,
              "returnGeometry": "true", "geometryFormat": "geojson", "lang": "fr", "limit": limit}
    try:
        with _session() as session:
            response = session.get(IDENTIFY, params=params, timeout=(30, 120))
            response.raise_for_status()
            results = response.json()["results"]
    except (requests.RequestException, ValueError, KeyError) as exc:
        raise RuntimeError("Cannot reach the GA25 sections service. Check internet access and retry.") from exc
    if len(results) >= limit:
        raise RuntimeError("Too many GA25 sections in this area. Reduce the study area.")
    sections = {}
    for feature in results:
        section = _section(feature)
        if section is not None:
            sections[section["id"]] = section
    return sorted(sections.values(), key=_section_order)


def _section_order(section):
    """Sheet, then plate (I, II, III...), then section number."""
    roman = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10}
    number = str(section["section"])
    return (section["sheet"], roman.get(str(section["plate"]), 99),
            int(number) if number.isdigit() else 0, section["id"])


def valid_pdf(path):
    """A complete PDF starts with %PDF and ends with an %%EOF marker."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return False
    return data.startswith(b"%PDF") and b"%%EOF" in data[-2048:]


def download_sections(sections, cache, progress=print):
    """Download each section's original PDF plate once; adds a "path" to each section."""
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    with _session() as session:
        for index, section in enumerate(sections, 1):
            path = cache / f"{section['id']}.pdf"
            if not valid_pdf(path):
                partial = path.with_suffix(".part")
                try:
                    with session.get(section["url"], stream=True, timeout=(30, 120)) as response:
                        response.raise_for_status()
                        with partial.open("wb") as stream:
                            for chunk in response.iter_content(1024 * 1024):
                                stream.write(chunk)
                    if not valid_pdf(partial):
                        raise ValueError("File is not a complete PDF.")
                    partial.replace(path)
                except (requests.RequestException, OSError, ValueError) as exc:
                    partial.unlink(missing_ok=True)
                    raise RuntimeError(f"Could not download {path.name}. Check network/disk space and rerun.") from exc
            section["path"] = path
            progress(f"Section {index}/{len(sections)} ready: {path.name}")
    return sections


def create_grid(paths, aoi):
    """Mosaic at native resolution, then area-average once onto 25 m cells."""
    left, bottom, right, top = aligned_bounds(aoi)
    if not paths:
        raise ValueError("No source tiles. Run the download step first.")
    # Keep the source mosaic aligned to its native 2 m grid, even for odd 25 m edges.
    native_bounds = (math.floor(left / 2) * 2, math.floor(bottom / 2) * 2,
                     math.ceil(right / 2) * 2, math.ceil(top / 2) * 2)
    with ExitStack() as stack, rasterio.Env(GDAL_CACHEMAX=64 * 1024 * 1024):
        sources = [stack.enter_context(rasterio.open(path)) for path in paths]
        for src in sources:
            if (src.crs != CRS.from_epsg(EPSG) or src.count != 1
                    or not np.allclose(src.res, (2, 2))
                    or src.transform.b != 0 or src.transform.d != 0
                    or src.transform.a <= 0 or src.transform.e >= 0):
                raise ValueError("Source tiles must be north-up 2 m LV95 elevation grids.")
        mosaic, source_transform = merge(sources, bounds=native_bounds, res=2,
                                          nodata=NODATA, dtype="float32")
    mosaic[~np.isfinite(mosaic)] = NODATA
    shape = (int((top - bottom) / RESOLUTION), int((right - left) / RESOLUTION))
    dem = np.full(shape, NODATA, dtype="float32")
    transform = from_origin(left, top, RESOLUTION, RESOLUTION)
    reproject(mosaic[0], dem, src_transform=source_transform, src_crs=EPSG,
              src_nodata=NODATA, dst_transform=transform, dst_crs=EPSG,
              dst_nodata=NODATA, resampling=Resampling.average)
    valid = np.isfinite(dem) & (dem != NODATA)
    dem[~valid] = NODATA
    if not valid.any():
        raise ValueError("No valid elevations in this rectangle. Check swissALTI3D coverage.")
    return dem, transform


def hillshade(dem, transform):
    """Northwest light at 45 degrees; transparent gaps and undefined slopes."""
    z = np.where(np.isfinite(dem) & (dem != NODATA), dem, np.nan).astype(float)
    # Image rows run southwards, so use negative Y spacing for northward slopes.
    dy, dx = np.gradient(z, transform.e, transform.a)
    azimuth, altitude = np.deg2rad([315, 45])
    sx, sy = np.sin(azimuth) * np.cos(altitude), np.cos(azimuth) * np.cos(altitude)
    light = (-dx * sx - dy * sy + np.sin(altitude)) / np.sqrt(1 + dx**2 + dy**2)
    alpha = np.isfinite(light) & np.isfinite(z)
    shade = np.clip(np.nan_to_num(light), 0, 1)
    return np.dstack((shade, shade, shade, alpha.astype(float)))


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
    ul_x = transform.c + 0.5 * transform.a
    ul_y = transform.f + 0.5 * transform.e
    world = (transform.a, transform.d, transform.b, transform.e, ul_x, ul_y)
    path.write_text("\n".join(f"{v:.10f}" for v in world) + "\n", encoding="ascii")


def _corner_centres(transform, width, height):
    lines = []
    for label, col, row in [("Upper left", 0, 0), ("Upper right", width-1, 0),
                            ("Lower left", 0, height-1), ("Lower right", width-1, height-1)]:
        x = transform.c + (col + 0.5) * transform.a
        y = transform.f + (row + 0.5) * transform.e
        lines.append(f"{label}: pixel ({col}, {row}), X={x:.2f}, Y={y:.2f}")
    return lines


def _section_label(section):
    return f"{section['plate']}-{section['section']}"


def _write_sections(sections, csv_path, geojson_path):
    """Section traces as CSV vertices (for GeoModeller) and GeoJSON (for GIS)."""
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["section_id", "label", "part", "vertex", "X", "Y"])
        for s in sections:
            for part, line in enumerate(s["lines"]):
                for vertex, (x, y) in enumerate(line):
                    writer.writerow([s["id"], _section_label(s), part, vertex, f"{x:.2f}", f"{y:.2f}"])
    features = [{"type": "Feature", "id": s["id"],
                 "properties": {k: s[k] for k in ("sheet", "plate", "section", "type", "vert_exag",
                                                  "author", "edition", "url")},
                 "geometry": {"type": "MultiLineString", "coordinates": s["lines"]}}
                for s in sections]
    geojson_path.write_text(json.dumps({"type": "FeatureCollection", "features": features,
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::2056"}}}, indent=1),
        encoding="utf-8")


def _export_map(output, layer, geology, crs_wkt, info):
    """Write one geological map PNG with world/PRJ files; return files and notes."""
    import matplotlib.pyplot as plt

    rgba, transform = geology
    name = MAP_NAMES[layer]
    png = output / f"geology_{name.lower()}_{transform.a:g}m_lv95.png"
    plt.imsave(png, rgba, origin="upper")
    _write_world(png.with_suffix(".pgw"), transform)
    png.with_suffix(".prj").write_text(crs_wkt, encoding="utf-8")
    height, width = rgba.shape[:2]
    lines = ["", f"{name} geological map: {png.name}, {transform.a:g} m pixels.",
             f"Transparent pixels: not covered by {name}.",
             "Legend: click the map at https://map.geo.admin.ch/#/map?lang=fr"
             f"&center={(info['XMin'] + info['XMax']) / 2:.0f},{(info['YMin'] + info['YMax']) / 2:.0f}"
             f"&z=6&layers={layer}",
             "Corner pixel CENTRES:"]
    lines += _corner_centres(transform, width, height)
    return [png, png.with_suffix(".pgw"), png.with_suffix(".prj")], lines


def export_outputs(dem, transform, output, tiles=(), geology=None, sections=(), geocover=None):
    """Write ASCII, PNGs, registration files, sections, project notes and one download ZIP.

    `geology` and `geocover` are (rgba, transform) pairs from get_ga25_map and
    get_geocover_map; `sections` the list from find_sections / download_sections.
    """
    import matplotlib.pyplot as plt

    info = grid_info(dem, transform)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    asc = output / "dem_25m_lv95.asc"
    png = output / "hillshade_25m_lv95.png"
    notes = output / "project_info.txt"
    height, width = dem.shape
    # Explicit ASCII writing avoids driver-dependent sidecars and centre/corner ambiguity.
    header = (f"ncols {width}\nnrows {height}\nxllcorner {info['XMin']:.2f}\n"
              f"yllcorner {info['YMin']:.2f}\ncellsize {RESOLUTION}\nNODATA_value {NODATA}")
    np.savetxt(asc, np.where(np.isfinite(dem), dem, NODATA), fmt="%.3f", header=header, comments="")
    crs_wkt = CRS.from_epsg(EPSG).to_wkt(version="WKT1_ESRI")
    asc.with_suffix(".prj").write_text(crs_wkt, encoding="utf-8")
    rgba = hillshade(dem, transform)
    plt.imsave(png, rgba, origin="upper")  # Raw pixels; no plot axes or margins.
    _write_world(png.with_suffix(".pgw"), transform)
    png.with_suffix(".prj").write_text(crs_wkt, encoding="utf-8")
    files = [asc, asc.with_suffix(".prj"), png, png.with_suffix(".pgw"), png.with_suffix(".prj")]
    map_notes = []
    for layer, geo in [(GA25_MAP, geology), (GEOCOVER_MAP, geocover)]:
        if geo is not None:
            map_files, map_lines = _export_map(output, layer, geo, crs_wkt, info)
            files += map_files
            map_notes += map_lines
    if sections:
        traces = output / "sections_ga25_lv95.csv"
        _write_sections(sections, traces, traces.with_suffix(".geojson"))
        files += [traces, traces.with_suffix(".geojson")]
    lines = ["GeoModeller project: CH1903+ / LV95 (EPSG:2056), metres; heights LN02.",
             f"Grid: {width} columns x {height} rows, 25 m cells."]
    lines += [f"{key} = {info[key]:.2f}" for key in ("XMin", "XMax", "YMin", "YMax", "ZMin", "ZMax")]
    lines += [f"Terrain elevations: {info['elevation_min']:.2f} to {info['elevation_max']:.2f} m",
              f"Valid terrain coverage: {info['coverage_percent']:.1f}%",
              "Z limits include at least 100 m margin; lower ZMin for deeper geology.",
              "X/Y bounds are image OUTER EDGES (same for every PNG). PNG north is at the top.",
              "For pixel control points, use these CENTRES (zero-based column,row):"]
    lines += _corner_centres(transform, width, height)
    lines += map_notes
    if sections:
        lines += ["", f"GA25 cross-sections crossing the area: {len(sections)}",
                  "Traces (LV95 vertices): sections_ga25_lv95.csv / .geojson. They may extend",
                  "beyond the area. Original plates: sections/<id>.pdf in the ZIP."]
        for s in sections:
            (x0, y0), (x1, y1) = s["lines"][0][0], s["lines"][-1][-1]
            lines.append(f"{_section_label(s)}  {s['id']}  ({s['sheet']}, {s['edition']}; "
                         f"vertical exaggeration {s['vert_exag']}; {s['author']})")
            lines.append(f"    from X={x0:.1f}, Y={y0:.1f} to X={x1:.1f}, Y={y1:.1f}")
    elif geology is not None:
        lines += ["", "No GA25 cross-section crosses this area."]
    lines += ["", "Source: Federal Office of Topography swisstopo", "Source tile URLs:"]
    lines += [tile["url"] for tile in tiles]
    notes.write_text("\n".join(lines) + "\n", encoding="utf-8")
    files.append(notes)
    # Include only files from this run, never an obsolete TIFF or arbitrary output files.
    archive = output / "geomodeller_dem.zip"
    with ZipFile(archive, "w", ZIP_DEFLATED) as zipped:
        for path in files:
            zipped.write(path, path.name)
        for s in sections:
            if s.get("path"):
                zipped.write(s["path"], f"sections/{s['id']}.pdf")
    return info, rgba, archive


def _geology_panel(ax, rgba, extent, geology, sections, title):
    """Hillshade, an optional map on top, and labelled cross-section traces."""
    from matplotlib import patheffects

    left, right, bottom, top = extent
    ax.imshow(rgba, extent=extent, origin="upper", interpolation="nearest")
    if geology is not None:
        ax.imshow(geology[0], extent=extent, origin="upper", alpha=0.8)
    for s in sections:
        for line in s["lines"]:
            xs, ys = zip(*line)
            ax.plot(xs, ys, color="red", lw=1.5)
        # Label at the first vertex inside the map, else the one nearest its centre.
        vertices = [v for line in s["lines"] for v in line]
        inside = [v for v in vertices if left <= v[0] <= right and bottom <= v[1] <= top]
        cx, cy = (left + right) / 2, (bottom + top) / 2
        x, y = inside[0] if inside else min(
            vertices, key=lambda v: (v[0] - cx) ** 2 + (v[1] - cy) ** 2)
        ax.annotate(_section_label(s), (np.clip(x, left, right), np.clip(y, bottom, top)),
                    color="red", fontweight="bold",
                    path_effects=[patheffects.withStroke(linewidth=3, foreground="white")])
    ax.set_xlim(left, right)
    ax.set_ylim(bottom, top)
    ax.set_title(f"{title}, {len(sections)} GA25 cross-section(s) in red")


def preview(dem, rgba, transform, geology=None, sections=(), geocover=None):
    """Show elevation, shading, GA25 and GeoCover maps with section traces; exports are untouched."""
    import matplotlib.pyplot as plt

    left, bottom, right, top = array_bounds(*dem.shape, transform)
    extent = (left, right, bottom, top)
    maps = [(title, geo) for title, geo in [("GA25", geology), ("GeoCover", geocover)]
            if geo is not None]
    if not maps and sections:
        maps = [("Hillshade", None)]
    if len(maps) == 2:
        fig, axes = plt.subplots(2, 2, figsize=(12, 10), constrained_layout=True)
        axes = axes.flatten()
    else:
        panels = 2 + len(maps)
        fig, axes = plt.subplots(1, panels, figsize=(6 * panels, 5), constrained_layout=True)
    terrain = axes[0].imshow(np.ma.masked_equal(dem, NODATA), extent=extent,
                             origin="upper", cmap="terrain", interpolation="nearest")
    fig.colorbar(terrain, ax=axes[0], label="Elevation (m, LN02)", shrink=0.8)
    axes[0].set_title("DEM: 25 m cells")
    axes[1].imshow(rgba, extent=extent, origin="upper", interpolation="nearest")
    axes[1].set_title("Hillshade: northwest light")
    for ax, (title, geo) in zip(axes[2:], maps):
        _geology_panel(ax, rgba, extent, geo, sections, title)
    for ax in axes:
        ax.set_xlabel("LV95 easting (m)")
        ax.set_ylabel("LV95 northing (m)")
        ax.ticklabel_format(useOffset=False, style="plain")
        ax.xaxis.set_major_locator(plt.MaxNLocator(4))
    plt.show()
