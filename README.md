# Swiss DEM for GeoModeller

Prepare a **25 m elevation grid** and a matching **hillshade PNG** from swisstopo's swissALTI3D data. The example covers the **Cervin / Matterhorn (6 x 6 km)**. Only one rectangle needs editing in the notebook.

The recommended workflow is **GitHub → EPFL NOTO → download the result → GeoModeller**. NOTO runs Python on its servers; nothing is installed on the GeoModeller computer.

## 1. Get the project into NOTO

Open [NOTO](https://noto.epfl.ch), sign in, and open a **Terminal** from the JupyterLab launcher. For a first copy, run:

```bash
git clone https://github.com/HyppaDandrite/get_dem.git
cd get_dem
```

This downloads the code directly onto NOTO. A public repository can be downloaded without a GitHub account. If `get_dem` already exists, use that folder instead of cloning over it. To update a clean Git copy later, run `git pull --ff-only` inside it; save your own notebook changes first. If Git reports conflicting local changes, keep a copy of your work and download a fresh project rather than discarding it.

**Without Git:** on the repository page, choose **Code → Download ZIP**. Upload the ZIP into NOTO using its file browser, then extract it in a NOTO terminal:

```bash
unzip get_dem-main.zip
cd get_dem-main
```

Both folder names work. Keep the complete project together, especially `dem_tools.py`, `requirements.txt` and `notebooks/get_dem_25m.ipynb`. Do not transfer a Python environment from Windows.

## 2. Create your NOTO environment once

In the NOTO terminal, from the project folder:

```bash
my_venvs_create swiss-dem
my_venvs_activate swiss-dem
pip install -r requirements.txt
my_kernels_create swiss-dem "Python (swiss-dem)"
```

If the environment already exists, skip the first line; if the kernel is already registered, skip the last line. Activate the environment before installing packages. NOTO already runs JupyterLab, so do not start another Jupyter server.

NOTO's current commands are documented in [Documentation/10_Envs_and_kernels.ipynb](https://noto.epfl.ch/user-redirect/lab/tree/Documentation/10_Envs_and_kernels.ipynb), available after sign-in. Follow that guide if a `my_...` command is unavailable. EPFL describes the environment → package installation → kernel workflow in its [library guide](https://www.epfl.ch/education/educational-initiatives/cede/teaching-interactively/jupyter-notebooks-for-education/getting-started-using-jupyter-notebooks-in-your-classroom/getting-the-best-performance-on-noto/libraries-on-noto/).

`requirements.txt` is the pip dependency list. **Do not give `environment.yml` to pip**: that optional file is for Conda. The notebook uses Rasterio's GDAL support; no separate `pip install gdal` is needed when using the [Rasterio binary wheels](https://rasterio.readthedocs.io/en/stable/installation.html).

## 3. Run the notebook

Open `notebooks/get_dem_25m.ipynb`. Select **Python (swiss-dem)** using the kernel selector at the top right. Activating an environment in a terminal does not select the notebook's kernel.

Keep the first code cell unchanged for the Cervin example, or replace its rectangle:

```python
# xmin, ymin, xmax, ymax — LV95 metres
AOI_LV95 = (2_614_000, 1_089_000, 2_620_000, 1_095_000)
```

Each side must be **100 m to 10 km**. Coordinates must be finite numbers, ordered minimum then maximum, in **CH1903+ / LV95 (EPSG:2056)**. The output resolution stays at **25 m**. The rectangle expands outward to whole 25 m cells when needed.

Run all cells from top to bottom. They validate the rectangle, select and download 2 m tiles, average them to 25 m, export the terrain and hillshade, print the six project limits, and show a preview. Only the area cell needs editing. If you change the area, run all cells again. If you update `dem_tools.py`, restart the kernel first so it loads the new code.

For the default area, expect **240 columns × 240 rows**, each cell 25 m across. The PNG is a view from above, not a perspective view of the summit. Source coverage may be incomplete near the Italian border; missing elevations remain NoData rather than being filled with invented terrain.

## 4. Download the result to the GeoModeller computer

On NOTO, outputs are stored **on the server**, in the project's `output` folder. Right-click **`geomodeller_dem.zip` → Download**, then extract the ZIP on your computer. It contains:

| File | Use |
| --- | --- |
| `dem_25m_lv95.asc` | Elevations to import as Digital Terrain Model / Arc Grid. |
| `hillshade_25m_lv95.png` | Optional shaded relief image for Image Manager. |
| `.prj` files and `.pgw` | Coordinate-system and image-placement companions; keep beside their main files. |
| `project_info.txt` | Six project limits, grid size, coverage, image registration coordinates and source URLs. |

No final TIFF is produced. The source TIFFs in `data/swissalti3d_tiles` are cached on NOTO and do not need to be downloaded to GeoModeller. The ZIP contains only the current export files. Each successful run replaces the named output files; save a previous result before changing the area.

## 5. Create the GeoModeller project

1. Select **CH1903+ / LV95 (EPSG:2056)**, also called **MN95**, with metre units. Do not substitute LV03 / EPSG:21781 or longitude/latitude. Elevations use **LN02**, which is separate from the horizontal CRS.
2. Copy **all six limits** printed in notebook section 5 or saved in `project_info.txt`. These are absolute coordinates, not widths. For the Cervin: XMin **2614000**, XMax **2620000**, YMin **1089000**, YMax **1095000**. Copy the computed ZMin/ZMax; they include at least 100 m of vertical margin. Lower ZMin for a deeper geological model.
3. Load `dem_25m_lv95.asc` as **Digital Terrain Model / topography**, using **Arc Grid**. Check that project and terrain overlap and that the project Z range contains the terrain.
4. Check the import's cell size and subsampling. The input should be 25 m; retain the full grid for this test. A reduced surface mesh can look much coarser than the source DEM.
5. Enable the topography surface (usually `SurfaceTopography`) and fit the 3D view to the terrain. Check the vertical display scale if it looks flattened.

Changing the CRS label does not move or reproject the coordinates. A default project between 0 and 1000 cannot overlap this terrain. If your GeoModeller version lacks LV95, consult its help/support for defining that CRS. A `local` Cartesian project can only be used consistently if every dataset retains the same LV95 numerical coordinates and metre units, with no automatic coordinate conversion; prefer an explicit LV95 declaration for exchange.

To add the hillshade, import the PNG through **Image Manager** on `SurfaceTopography`. The PNG contains shading, not elevations, and does not replace the terrain import. Keep its PGW and PRJ alongside it, but check placement: automatic reading depends on the software. For manual registration, `project_info.txt` gives both **outer image edges** and **corner pixel centres** (zero-based indices). Use the type requested by the dialog. See the [vendor image-import guide](https://support.intrepid-geophysics.com/support/solutions/articles/43000043768-import-2d-data-grids-in-geomodeller-and-digitize-interfaces-structures).

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
