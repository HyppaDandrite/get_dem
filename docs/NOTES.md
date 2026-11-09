# Technical notes for teaching staff

## Optional georeferencing files

The export also includes PRJ files (coordinate-system descriptions) and a PGW file (PNG pixel placement). They are optional companions for software that supports them. Their automatic use by GeoModeller has not been confirmed and the student workflow does not rely on it.

The ASCII header already contains the grid origin, cell size and dimensions. Select LV95 in the GeoModeller project. Register the PNG manually using the coordinates in `project_info.txt` when prompted. That file distinguishes outer image edges from corner pixel centres (zero-based indices).

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `pip` installation is refused | Activate your NOTO virtual environment first; installation into the shared environment is restricted. |
| Missing `swiss-dem` kernel | Register it in the activated environment, refresh JupyterLab, and select it. |
| `ModuleNotFoundError` for a library | Wrong kernel or incomplete installation. Activate the environment and rerun `pip install -r requirements.txt`. |
| Cannot find `dem_tools` / project folder | Keep the complete repository together; open the notebook inside its `notebooks` folder. |
| Invalid area | Use a finite LV95 centre `(x, y)`, sides between 100 m and 10 km, and a finite azimuth. The whole turned area must stay in the LV95 range. Degrees, LV03, text, NaN and infinity are rejected. |
| Geology download fails | The map.geo.admin.ch WMS must be reachable. A service error is reported with its message. Set `GEOLOGY = None` to build the terrain alone. |
| Rotated model does not match other data | The files of a rotated area are in local metres. Convert LV95 data with the formulas in `project_info.txt`, or with `area.to_local(E, N)` in Python. |
| No tiles / no valid elevations | The rectangle must overlap swissALTI3D coverage. LV95-looking coordinates alone do not guarantee coverage. |
| Catalog or download failure | Check NOTO's internet access and disk space; rerun. Completed files are validated and reused; partial files are never accepted as complete tiles. |
| Kernel stops / storage warning | Use a smaller area, close unused notebooks, and check NOTO resource usage. Cached source tiles can accumulate across areas. |
| Empty parts of the terrain | Read the coverage percentage. NoData at coverage boundaries is expected and stays empty. |
| DEM does not intersect project | Check all project X/Y bounds and CRS; remove default 0–1000 limits. |
| Terrain invisible / too coarse | Check Z bounds, surface visibility, view extent, vertical scale and import subsampling. The PNG alone does not fix the geometry. |
| Image misplaced | Check its registration; distinguish outer edges from pixel centres. The PNG top is the area's top edge, which is north only when `AZIMUTH_DEG = 0`. |
| Files missing on your computer | NOTO files stay on NOTO until you download `output/geomodeller_dem.zip`. |

A cached tile with the wrong resolution/CRS, a damaged raster, or a mismatching published SHA-256 checksum is downloaded again. For an interrupted run, simply rerun the notebook. Do not delete the Python environment to repair a data download.

## Other ways to run it

Students may use their own JupyterLab, VS Code or another Python setup. Use **Python 3.11 or 3.12**, install `requirements.txt` in an isolated environment, and select that environment's notebook kernel. Install JupyterLab separately if your setup does not already provide it. For Conda users, `environment.yml` provides the same project dependencies plus JupyterLab (`conda env create -f environment.yml`). These are optional alternatives; the tutorial above uses NOTO only.

## For teaching and maintenance

NOTO documents a personal storage allowance of **2 GB** and up to **4 GB RAM / 2 CPUs** per user; check its [current resource guidance](https://www.epfl.ch/education/educational-initiatives/cede/teaching-interactively/jupyter-notebooks-for-education/getting-started-using-jupyter-notebooks-in-your-classroom/resources-on-noto/). Source downloads are limited to 10 km sides and at most 300 tiles: a 10 km square turned 45° needs every tile under its 14 km north-up envelope (about 4 s and 0.7 GB RAM for the averaging). For a whole class, EPFL can assess a common environment or shared data; library requests should arrive at least two weeks ahead. Test the notebook in the teaching account before the session.

The code is in `dem_tools.py`; the notebook presents the student workflow. Run the offline checks from the project root:

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

Tests cover invalid inputs, rotated geometry, tile selection, interrupted downloads, corrupt caches, grid averaging (north-up and rotated), NoData, coordinates, shading independent of rotation, geology sampling and service errors, and the export ZIP. GitHub Actions runs them on Linux and Windows. Live swisstopo access and GeoModeller's GUI are separate integration checks; automated tests do not certify a particular GeoModeller installation.

## Data and limitations

This is an average of **swissALTI3D at 2 m**, not the legacy DHM25 product. Averaging can soften narrow ridges. Each 2 m cell is split into four 1 m samples, which are assigned to the 25 m cell containing them: for a north-up area this equals an exact area-weighted average (identical to the previous GDAL version); for a rotated area, cells cut obliquely are approximated to 1 m (under 0.15 m on a 66° slope in tests). The latest available edition of each 1 km tile is selected, so acquisition years may differ across the area. NoData is **-9999**; the requested rectangle is not clipped to the national border. Hillshade uses northwest illumination at 45 degrees in true LV95 directions, whatever the rotation, without vertical exaggeration; undefined slopes beside data gaps are transparent.

The geological map is a rendered WMS image (`ch.swisstopo.geologie-geocover` or `ch.swisstopo.geologie-geologischer_atlas`), requested north-up at 5 m pixels over the area's envelope, then resampled by nearest neighbour onto the area, so colours stay exact. It is a picture for draping, not classified unit data; GeoCover does not cover every area and gaps are transparent. Read units and legend on map.geo.admin.ch (link in `project_info.txt`).

Data source: [swissALTI3D, swisstopo](https://www.swisstopo.admin.ch/en/height-model-swissalti3d). Attribution: **Source: Federal Office of Topography swisstopo**. The export records source URLs for traceability. Terrain data and generated outputs are excluded from Git.
