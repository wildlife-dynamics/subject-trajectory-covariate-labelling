# Subject Trajectory Covariate Labelling — User Guide

This workflow builds a subject **trajectory** from EarthRanger and labels every
trajectory segment with one or more **Google Earth Engine (GEE) covariates** — a static
image (e.g. elevation) or a time-matched image collection (e.g. NDVI). It then shows a
covariate time-series plot and exports the labelled table for downstream analysis.

```
EarthRanger observations -> relocations -> trajectory segments
   -> label each segment with your covariates (GEE)
   -> time-series plot (per subject) + CSV / Parquet export
```

---

## Prerequisites

Two data-source connections, attached when you configure a run:

- An **EarthRanger** connection (the subjects and their observations).
- A **Google Earth Engine** connection (the covariate imagery).

---

## Step 1 — Add the template

In Ecoscope, open **Workflow Templates → Add Workflow Template**, paste the repository
URL, and click **Add**. The card may show *Initializing…* while it compiles. It then
appears as **Subject Trajectory Covariate Labelling**; click it to open the run form.

## Step 2 — Fill the run form

**Workflow details** — a name and optional description for this run.

**Data Source (EarthRanger)** — pick your EarthRanger connection.

**Earth Engine Connection** — pick your GEE connection.

**Time Range** — the reporting window (Since / Until) and timezone. Observations are
pulled for this window.

**Subject Group** — the EarthRanger subject group to analyse. A group may contain **one or
many** subjects (see *Working with several subjects* below).

**Configure grouping strategy** — add groupers to split the dashboard into views. The usual
choice is **Category → Subject Name**; you can also group by Subject Subtype, Subject Sex,
or Time (Year, Month, …). This is what lets you page through subjects in the results
(below).

**Covariates** — the heart of the workflow. Click **Add** for each covariate and choose its
type:

- **Image** — one GEE image. Fields:
  - *name* — the output column name (e.g. `elevation`).
  - *image ID* — the GEE asset path (e.g. `USGS/SRTMGL1_003`).
  - *band* — the band to sample (e.g. `elevation`).
  - *reducer* — how to reduce pixels under each segment (default `mean`).
  - *scale* — sampling scale in metres (SRTM is 30 m).
  - *buffer* — metres to buffer each segment before sampling (see *Null values*).
  - *scale factor* — multiply the sampled value by a constant (default `1.0`).

- **Image Collection** — a time-matched collection. Same fields as Image, plus:
  - *collection ID* — the GEE asset path (e.g. `MODIS/061/MYD13A1`).
  - *time column* — the segment time used to pick the closest image (`segment_start`).
  - For each segment, the image **closest in time** is sampled.

  IDs are Earth Engine **asset paths**, not band names — entering `NDVI` as a collection
  fails, because `NDVI` is a band, not a collection.

Each covariate adds one column named by its *name*; an Image Collection also adds
`<name>_img_date` (the date of the matched image).

**Export** — `both` (default), `csv`, `parquet`, or `none`. Parquet keeps the geometry;
CSV is a clean tabular file. Both carry all the covariate columns.

---

## Working with several subjects

The group can hold many subjects (e.g. 10). To navigate them:

- **In the dashboard:** add **Subject Name** under *Configure grouping strategy*. The
  time-series plot is then built **per subject** and the results view shows a **subject
  selector** — pick a subject and its plot is shown. (Without a grouper, all segments are
  drawn in a single plot.)
- **In the export:** the CSV / Parquet includes a `subject_name` column, so you can filter
  or group by subject in R / pandas / a spreadsheet.

---

## Results view

A **covariate time-series** plot: the covariate value on the y-axis against segment time on
the x-axis, with the **observations drawn as a rug** along the bottom. A **dropdown** switches
which covariate is on the y-axis, and — when grouped — the **subject selector** switches
subjects.

## Output files

Written to the workflow results directory:

- `trajectory_covariates.parquet` — the full labelled table, with geometry.
- `trajectory_covariates.csv` — the same table, tabular (geometry dropped).
- `covariate_timeseries.html` — the rendered plot(s).

---

## Notes

- **NDVI (and other scaled products).** MODIS NDVI is stored as an integer scaled by
  `0.0001` (e.g. `1792` = `0.1792`). To read and plot **true NDVI (0–1)**, set the NDVI
  covariate's **scale factor** to `0.0001`. Leave it `1.0` for unscaled bands like elevation.
- **Null covariate values.** A segment is null for a covariate only when there is genuinely
  no valid pixel under it — open water, or a persistently cloud-masked NDVI composite. The
  per-covariate **buffer** (default 250 m for collections, 30 m for images) samples a small
  neighbourhood so short / near-stationary segments and masked edges still return a value;
  set it to `0` for strict single-point sampling (which reintroduces those gaps).
- **One image per segment.** Image-Collection covariates sample the single closest-in-time
  image, so there is exactly one value (and one `img_date`) per segment.
