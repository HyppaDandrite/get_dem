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
| Invalid rectangle | Use four finite LV95 numbers, min < max, with sides between 100 m and 10 km. Degrees, LV03, text, NaN and infinity are rejected. |
| No tiles / no valid elevations | The rectangle must overlap swissALTI3D coverage. LV95-looking coordinates alone do not guarantee coverage. |
| Catalog or download failure | Check NOTO's internet access and disk space; rerun. Completed files are validated and reused; partial files are never accepted as complete tiles. |
| Kernel stops / storage warning | Use a smaller area, close unused notebooks, and check NOTO resource usage. Cached source tiles can accumulate across areas. |
| Empty parts of the terrain | Read the coverage percentage. NoData at coverage boundaries is expected and stays empty. |
| DEM does not intersect project | Check all project X/Y bounds and CRS; remove default 0–1000 limits. |
| Terrain invisible / too coarse | Check Z bounds, surface visibility, view extent, vertical scale and import subsampling. The PNG alone does not fix the geometry. |
| Image misplaced | Check its registration and north-up orientation; distinguish outer edges from pixel centres. |
| Files missing on your computer | NOTO files stay on NOTO until you download `output/geomodeller_dem.zip`. |

A cached tile with the wrong resolution/CRS, a damaged raster, or a mismatching published SHA-256 checksum is downloaded again. For an interrupted run, simply rerun the notebook. Do not delete the Python environment to repair a data download.

## Other ways to run it

Students may use their own JupyterLab, VS Code or another Python setup. Use **Python 3.11 or 3.12**, install `requirements.txt` in an isolated environment, and select that environment's notebook kernel. Install JupyterLab separately if your setup does not already provide it. For Conda users, `environment.yml` provides the same project dependencies plus JupyterLab (`conda env create -f environment.yml`). These are optional alternatives; the tutorial above uses NOTO only.

## For teaching and maintenance

NOTO documents a personal storage allowance of **2 GB** and up to **4 GB RAM / 2 CPUs** per user; check its [current resource guidance](https://www.epfl.ch/education/educational-initiatives/cede/teaching-interactively/jupyter-notebooks-for-education/getting-started-using-jupyter-notebooks-in-your-classroom/resources-on-noto/). Source downloads are limited to a 10 km rectangle and at most 200 tiles. For a whole class, EPFL can assess a common environment or shared data; library requests should arrive at least two weeks ahead. Test the notebook in the teaching account before the session.

The code is in `dem_tools.py`; the notebook presents the student workflow. Run the offline checks from the project root:

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

Tests cover invalid inputs, tile selection, interrupted downloads, corrupt caches, grid averaging, NoData, coordinates, shading and the export ZIP. GitHub Actions runs them on Linux and Windows. Live swisstopo access and GeoModeller's GUI are separate integration checks; automated tests do not certify a particular GeoModeller installation.

## Data and limitations

This is an average of **swissALTI3D at 2 m**, not the legacy DHM25 product. Averaging can soften narrow ridges. The latest available edition of each 1 km tile is selected, so acquisition years may differ across the area. NoData is **-9999**; the requested rectangle is not clipped to the national border. Hillshade uses northwest illumination at 45 degrees, without vertical exaggeration; undefined slopes beside data gaps are transparent.

Data source: [swissALTI3D, swisstopo](https://www.swisstopo.admin.ch/en/height-model-swissalti3d). Attribution: **Source: Federal Office of Topography swisstopo**. The export records source URLs for traceability. Terrain data and generated outputs are excluded from Git.
