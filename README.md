# A terrain model for GeoModeller

Follow these steps to prepare the **Cervin / Matterhorn terrain at 25 m resolution** using [EPFL NOTO](https://noto.epfl.ch).

## 1. Open the project in NOTO

1. Open [NOTO](https://noto.epfl.ch) and sign in.
2. Click **File → New → Terminal**.
3. Copy and paste the following lines into the terminal, then press **Enter**:

```bash
git clone https://github.com/HyppaDandrite/get_dem.git
cd get_dem
my_venvs_create swiss-dem
my_venvs_activate swiss-dem
pip install -r requirements.txt
my_kernels_create swiss-dem "Python (swiss-dem)"
```

Wait until the commands finish. **Do this step only the first time.** On subsequent visits, start at step 2 below.

## 2. Open the notebook

1. In the file browser on the left, open **get_dem → notebooks**.
2. Double-click **get_dem_25m.ipynb**.
3. Click the kernel name at the top right and select **Python (swiss-dem)**.

## 3. Generate the terrain

1. Keep the first code cell unchanged to use the Cervin example.
2. Click **Run → Run All Cells**.
3. Wait until the terrain and hillshade previews appear at the bottom.

To use another area, change only the rectangle in the cell labelled **EDIT HERE**, then run all cells again. Enter `(xmin, ymin, xmax, ymax)` in LV95 metres, with each side between 100 m and 10 km.

## 4. Download your files

1. In NOTO's file browser, open **get_dem → output**.
2. Right-click **geomodeller_dem.zip** and select **Download**.
3. Extract the ZIP on the computer where you will use GeoModeller.

Use these three files for the following steps:

| File | What to do with it |
| --- | --- |
| `project_info.txt` | Open it to read the project limits and image coordinates. |
| `dem_25m_lv95.asc` | Import it as the terrain. |
| `hillshade_25m_lv95.png` | Add it as the shaded relief image. |

## 5. Import the terrain into GeoModeller

1. Create a project and choose **CH1903+ / LV95 (EPSG:2056)** with units in **metres**.
2. Open **project_info.txt**. Copy its **XMin, XMax, YMin, YMax, ZMin and ZMax** into the project's bounding box.
3. Open **Section → Topography → Load from DTM**.
4. Select **dem_25m_lv95.asc**, using the **Arc Grid** format.
5. Check that the cell size is **25 m** and retain the full grid when choosing subsampling.
6. Confirm the import, enable **SurfaceTopography**, and fit the 3D view to the terrain.

## 6. Add the shaded relief image

1. Open **Image Manager** for **SurfaceTopography**.
2. Add **hillshade_25m_lv95.png**.
3. Position the image using the coordinates in **project_info.txt**: use **outer edges** for an image extent, or **corner pixel centres** for control points.
4. Enable the image display.

If a control or coordinate-system option differs in your GeoModeller version, ask the teaching staff before continuing.

## Using your own setup

You may use another Python/Jupyter setup. The dependencies are listed in `requirements.txt`; a Conda configuration is provided in `environment.yml`.

[Notes for teaching staff](docs/NOTES.md)

Data: **Source: Federal Office of Topography swisstopo**, [swissALTI3D](https://www.swisstopo.admin.ch/en/height-model-swissalti3d).
