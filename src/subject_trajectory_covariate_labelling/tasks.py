"""Covariate-labeling tasks: label a (geo)dataframe with Google Earth Engine covariates.

Registered tasks:

- ``label_with_covariates``   -> label a frame with a USER-DEFINED LIST of
                                 covariates. Each covariate is either a single
                                 GEE *image* (e.g. elevation) or a GEE *image
                                 collection* sampled at the time closest to each
                                 segment (e.g. NDVI). The list is variable length
                                 and each entry exposes only the params its kind
                                 needs (a discriminated union -> conditional form).
- ``plot_covariate_timeseries`` -> a time-series figure (covariate value vs
                                 segment time) with a dropdown to choose which
                                 covariate is on the y-axis and the observations
                                 drawn as a rug along the x-axis.
- ``export_labeled_table``    -> optionally export the labeled table as CSV,
                                 Parquet, or both (default both).

The two original single-covariate tasks (``label_with_static_image`` and
``label_with_temporal_image_collection``) are kept for backwards compatibility.

Both labeling paths route through ``eetools.chunk_gdf``, which batches features so
large jobs stay within Earth Engine's per-request quota/memory limits. Heavy GEE
imports happen inside the task bodies so importing this module for the registry
scan stays light.
"""

from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from ecoscope.platform.annotations import AnyGeoDataFrame
from ecoscope.platform.connections import EarthEngineClient
from pydantic import BaseModel, ConfigDict, Field
from wt_registry import register

# Region reducers offered to the run form. 'mean' gives one scalar per feature
# (best for line/polygon segments); 'toList' returns the raw pixel value(s).
_ALLOWED_REDUCERS = {
    "mean",
    "median",
    "mode",
    "min",
    "max",
    "sum",
    "stdDev",
    "first",
    "toList",
}


def _build_reducer(name: str):
    """Turn a reducer name (e.g. 'mean') into a validated ee.Reducer."""
    import ee

    if name not in _ALLOWED_REDUCERS:
        raise ValueError(
            f"reducer must be one of {sorted(_ALLOWED_REDUCERS)}, got {name!r}"
        )
    return getattr(ee.Reducer, name)()


def _label_gdf_with_img_robust(gdf, img, region_reducer, scale=500.0, buffer_m=0.0):
    """Sample a static image per feature, keeping EVERY input row.

    ecoscope's ``label_gdf_with_img`` uses ``reduceRegions``, which silently
    drops features that do not overlap the image and then rebuilds a frame on
    the original index -- a length mismatch when any feature is over no-data
    (e.g. line segments crossing water on a DEM). This maps ``reduceRegion``
    over the features 1:1, so a feature with no data becomes NaN instead of
    vanishing.

    ``buffer_m`` buffers each feature SERVER-SIDE (inside Earth Engine) so the
    uploaded geometry stays the small original line -- buffering client-side
    turns every segment into a many-vertex polygon and the request payload
    blows past Earth Engine's 10 MB limit.
    """
    import ee
    import pandas as pd

    in_fc = ee.FeatureCollection(gdf[["geometry"]].__geo_interface__)
    buffer_m = float(buffer_m or 0.0)

    def feat_func(feat):
        geom = feat.geometry()
        if buffer_m > 0:
            geom = geom.buffer(buffer_m)
        return feat.set(
            "img_vals",
            img.reduceRegion(reducer=region_reducer, geometry=geom, scale=scale),
        )

    out_fc = in_fc.map(feat_func)
    raw = out_fc.reduceColumns(ee.Reducer.toList(1), ["img_vals"]).get("list").getInfo()

    def _norm(r):
        if isinstance(r, list):
            return r[0] if r else {}
        return r if isinstance(r, dict) else {}

    dicts = [_norm(r) for r in (raw or [])]
    return pd.DataFrame(dicts, index=gdf.index)


def _label_temporal_by_feature_robust(
    gdf, img_coll, time_col_name, region_reducer, scale=500.0, buffer_m=0.0
):
    """Time-match one image per feature and sample it, buffering SERVER-SIDE.

    Mirrors ``ecoscope.io.eetools.label_gdf_with_temporal_image_collection_by_feature``
    (nearest single image per feature, 1:1 rows, an ``img_date`` column), but
    buffers each segment inside Earth Engine rather than uploading buffered
    polygons -- keeping the request payload small. A masked/no-data pixel still
    comes back as NaN (its ``img_date`` is set).
    """
    import ee
    import pandas as pd
    from ecoscope.io.eetools import _match_gdf_to_img_coll_ids

    buffer_m = float(buffer_m or 0.0)
    _match_gdf_to_img_coll_ids(
        gdf=gdf,
        time_col=time_col_name,
        img_coll=img_coll,
        output_col_name="img_ids",
        n_before=0,
        n_after=0,
        n="images",
    )

    in_fc = ee.FeatureCollection(gdf[["geometry", "img_ids"]].__geo_interface__)

    def feat_func(feat):
        tmp_coll = img_coll.filter(
            ee.Filter.inList("system:index", feat.get("img_ids"))
        )

        def region_reduc(img):
            geom = feat.geometry()
            if buffer_m > 0:
                geom = geom.buffer(buffer_m)
            return img.set(
                {
                    "img_date": img.date().format(),
                    "img_vals": img.reduceRegion(
                        reducer=region_reducer, geometry=geom, scale=scale
                    ),
                }
            )

        return feat.set(
            "values",
            tmp_coll.map(region_reduc)
            .reduceColumns(ee.Reducer.toList(2), ["img_date", "img_vals"])
            .values()
            .get(0),
        )

    out_fc = in_fc.map(feat_func, True)
    result = pd.DataFrame(
        out_fc.select("values")
        .reduceColumns(ee.Reducer.toList(1), ["values"])
        .get("list")
        .getInfo(),
        columns=["values"],
        index=gdf.index,
    ).apply(pd.Series.explode)
    result["img_date"] = result["values"].str[0]
    result["values"] = result["values"].str[1]
    result = pd.concat(
        [result.drop(columns=["values"]), result["values"].apply(pd.Series)],
        ignore_index=False,
        axis=1,
    )
    return result


# ---------------------------------------------------------------------------
# Covariate specs (discriminated union -> conditional run form)
# ---------------------------------------------------------------------------
class StaticImageCovariate(BaseModel):
    """One covariate sampled from a single GEE image (e.g. elevation)."""

    model_config = ConfigDict(title="Image")
    kind: Annotated[Literal["image"], Field(exclude=True)] = "image"
    name: Annotated[
        str,
        Field(
            description="Name for this covariate's column in the output, e.g. 'elevation'."
        ),
    ]
    image_id: Annotated[
        str,
        Field(
            description="Full Earth Engine image asset ID (a path, not a band name), e.g. 'USGS/SRTMGL1_003'."
        ),
    ] = "USGS/SRTMGL1_003"
    band: Annotated[
        str,
        Field(description="Band to sample from the image, e.g. 'elevation'."),
    ] = "elevation"
    reducer: Annotated[
        str,
        Field(
            description="Region reducer per segment (mean, median, mode, min, max, sum, stdDev, first)."
        ),
    ] = "mean"
    scale: Annotated[
        float,
        Field(
            description="Sampling scale in metres; the image's native resolution is a good default (SRTM is 30 m)."
        ),
    ] = 30.0
    buffer_m: Annotated[
        float,
        Field(
            description="Buffer each segment by this many metres before sampling, to recover values for very short/stationary segments. 0 disables."
        ),
    ] = 30.0
    scale_factor: Annotated[
        float,
        Field(
            description="Multiply sampled values by this constant (e.g. to apply a product's scale). 1.0 keeps raw values."
        ),
    ] = 1.0


class ImageCollectionCovariate(BaseModel):
    """One covariate sampled from a GEE image collection at the time closest to each segment (e.g. NDVI)."""

    model_config = ConfigDict(title="Image Collection")
    kind: Annotated[Literal["image_collection"], Field(exclude=True)] = (
        "image_collection"
    )
    name: Annotated[
        str,
        Field(
            description="Name for this covariate's column in the output, e.g. 'ndvi'."
        ),
    ]
    image_collection_id: Annotated[
        str,
        Field(
            description="Full Earth Engine ImageCollection asset ID (a path, NOT a band name), e.g. 'MODIS/061/MYD13A1'."
        ),
    ] = "MODIS/061/MYD13A1"
    band: Annotated[
        str,
        Field(description="Band to sample from the collection, e.g. 'NDVI'."),
    ] = "NDVI"
    time_column: Annotated[
        str,
        Field(
            description="Datetime column used to pick the temporally closest image, e.g. 'segment_start'."
        ),
    ] = "segment_start"
    reducer: Annotated[
        str,
        Field(
            description="Region reducer per segment (mean, median, mode, min, max, sum, stdDev, first)."
        ),
    ] = "mean"
    scale: Annotated[
        float,
        Field(description="Sampling scale in metres (MODIS MYD13A1 is 500 m)."),
    ] = 500.0
    buffer_m: Annotated[
        float,
        Field(
            description="Buffer each segment by this many metres before sampling, to recover NDVI for very short/stationary segments and masked edges. 0 disables."
        ),
    ] = 250.0
    scale_factor: Annotated[
        float,
        Field(
            description="Multiply sampled values by this constant, e.g. 0.0001 to turn raw MODIS NDVI into true NDVI (0-1). 1.0 keeps raw values."
        ),
    ] = 1.0


# A plain union (not an explicit discriminator): pydantic selects the branch by the
# fields present, and rjsf renders a one-of selector using each model's title
# ("Image" / "Image Collection") -- the same pattern as ext_ste.flexible_previous_period.
Covariate = Union[StaticImageCovariate, ImageCollectionCovariate]


def _extract_temporal(labels, band: str):
    """Pull the band value and matched-image date out of the per-feature sample frame.

    With a single nearest image per feature the frame is already one row per
    feature (indexed like the input), so this just coerces the two columns. A
    masked/no-data pixel comes back as NaN (its ``img_date`` is still set).
    Returns ``(value_series, date_series)`` on the input index.
    """
    import numpy as np
    import pandas as pd

    idx = labels.index
    if band in labels.columns:
        value = pd.to_numeric(labels[band], errors="coerce")
    else:
        value = pd.Series(np.nan, index=idx)
    if "img_date" in labels.columns:
        date = pd.to_datetime(labels["img_date"], errors="coerce", utc=True)
    else:
        date = pd.Series(pd.NaT, index=idx)
    return value, date


@register(tags=["io"])
def label_with_covariates(
    client: EarthEngineClient,
    df: Annotated[
        AnyGeoDataFrame,
        Field(
            description="The (geo)dataframe to label. Must carry geometry (and the time column of any image-collection covariate).",
            exclude=True,
        ),
    ],
    covariates: Annotated[
        list[Covariate],
        Field(
            description="One entry per covariate to attach. Choose 'Image' for a single image (e.g. elevation) or 'Image Collection' for a time-matched covariate (e.g. NDVI); add as many as you need."
        ),
    ] = [],  # noqa: B006
    df_chunk_size: Annotated[
        int,
        Field(
            description="Features sampled per Earth Engine request. Lower it if you hit GEE quota/memory limits."
        ),
    ] = 5000,
) -> AnyGeoDataFrame:
    """Attach each covariate in ``covariates`` to ``df`` as a new column named by its ``name``.

    Image covariates add one column; image-collection covariates add that column
    plus ``<name>_img_date`` (the date of the matched image). Every input row is
    preserved -- a segment with no valid pixel under it becomes NaN.
    """
    import ee
    from ecoscope.io import eetools

    out = df
    for cov in covariates:
        if isinstance(cov, StaticImageCovariate):
            img = ee.Image(cov.image_id).select([cov.band])
            labels = eetools.chunk_gdf(
                gdf=df[["geometry"]],
                label_func=_label_gdf_with_img_robust,
                label_func_kwargs={
                    "img": img,
                    "region_reducer": _build_reducer(cov.reducer),
                    "scale": cov.scale,
                    "buffer_m": cov.buffer_m,
                },
                df_chunk_size=df_chunk_size,
                max_workers=1,
            )
            if labels is None or labels.shape[1] == 0:
                continue
            import pandas as pd

            value = pd.to_numeric(labels.iloc[:, 0], errors="coerce")
            if cov.scale_factor != 1.0:
                value = value * cov.scale_factor
            out = out.join(value.rename(cov.name))
        else:
            coll = ee.ImageCollection(cov.image_collection_id).select([cov.band])
            labels = eetools.chunk_gdf(
                gdf=df[[cov.time_column, "geometry"]],
                label_func=_label_temporal_by_feature_robust,
                label_func_kwargs={
                    "img_coll": coll,
                    "time_col_name": cov.time_column,
                    "region_reducer": _build_reducer(cov.reducer),
                    "scale": cov.scale,
                    "buffer_m": cov.buffer_m,
                },
                df_chunk_size=df_chunk_size,
                max_workers=1,
            )
            if labels is None:
                continue
            value, date = _extract_temporal(labels, cov.band)
            if cov.scale_factor != 1.0:
                value = value * cov.scale_factor
            out = out.join(value.rename(cov.name))
            out = out.join(date.rename(f"{cov.name}_img_date"))
    return out


# ---------------------------------------------------------------------------
# Time-series plot: covariate vs time, observation rug, covariate selector
# ---------------------------------------------------------------------------
# Columns produced by the trajectory pipeline (never covariates). Covariate columns
# are the ones `label_with_covariates` adds, named by the user; everything here is
# excluded so auto-detect can't mistake a trajectory stat (or a timestamp) for one.
_TRAJ_DENYLIST = {
    "geometry",
    "id",
    "groupby_col",
    "segment_start",
    "segment_end",
    "timespan_seconds",
    "dist_meters",
    "speed_kmhr",
    "heading",
    "junk_status",
    "nsd",
    "is_night",
    "extra__is_night",
    "subject_name",
    "subject_sex",
    "subject_subtype",
}


def _auto_covariate_columns(df, time_column: str):
    """Numeric covariate columns only.

    Excludes the trajectory schema, index/grouper helpers, ``*_img_date`` stamps,
    and -- importantly -- any datetime/timedelta column (a datetime survives
    ``pd.to_numeric`` as epoch nanoseconds, so it must be rejected by dtype first,
    or e.g. ``segment_end`` gets plotted as a bogus covariate).
    """
    import pandas as pd

    cols = []
    for c in df.columns:
        if c == time_column or c in _TRAJ_DENYLIST or c.endswith("_img_date"):
            continue
        if c.startswith(("extra__", "subject_", "TemporalGrouper", "SpatialGrouper")):
            continue
        s = df[c]
        if pd.api.types.is_datetime64_any_dtype(s) or pd.api.types.is_timedelta64_dtype(
            s
        ):
            continue
        if pd.to_numeric(s, errors="coerce").notna().any():
            cols.append(c)
    return cols


@register(tags=["results"])
def plot_covariate_timeseries(
    df: Annotated[
        AnyGeoDataFrame,
        Field(description="The labeled trajectory frame.", exclude=True),
    ],
    covariate_columns: Annotated[
        Optional[list[str]],
        Field(
            description="Covariate columns to offer on the y-axis. Leave empty to auto-detect the labeled columns."
        ),
    ] = None,
    time_column: Annotated[
        str,
        Field(description="Datetime column for the x-axis, e.g. 'segment_start'."),
    ] = "segment_start",
    title: Annotated[
        str,
        Field(description="Plot title."),
    ] = "Covariate time series",
) -> str:
    """Build an interactive time-series figure and return it as standalone HTML.

    One series per covariate (only the first shown); a dropdown switches which
    covariate is on the y-axis. Observation times are drawn as a rug along the
    bottom of the x-axis. Pair with ``persist_text`` + ``create_plot_widget_single_view``.
    """
    import pandas as pd
    import plotly.graph_objects as go

    ACCENT = "#2563eb"  # one accent: only one covariate is shown at a time
    RUG = "#94a3b8"

    # Match the Ecoscope dashboard contract: inline plotly.js (self-contained),
    # fill the widget, and post the "PlotLoaded" message the dashboard waits for.
    _POST_SCRIPT = (
        'window.parent.postMessage({ type: "PlotLoaded", widgetId: "{plot_id}" }, "*");'
    )

    def _render(figure):
        return figure.to_html(
            full_html=True,
            include_plotlyjs=True,
            default_height="100%",
            default_width="100%",
            post_script=_POST_SCRIPT,
            config={"responsive": True, "displayModeBar": False},
        )

    d = df.copy()
    t = pd.to_datetime(d[time_column], errors="coerce", utc=True)

    cols = [
        c for c in (covariate_columns or []) if c in d.columns
    ] or _auto_covariate_columns(d, time_column)

    fig = go.Figure()
    if not cols:
        fig.add_annotation(text="No covariate columns found to plot.", showarrow=False)
        return _render(fig)

    order = t.argsort()
    t_sorted = t.iloc[order]
    for i, c in enumerate(cols):
        y = pd.to_numeric(d[c], errors="coerce").iloc[order]
        fig.add_trace(
            go.Scattergl(
                x=t_sorted,
                y=y,
                mode="lines+markers",
                name=c,
                visible=(i == 0),
                line=dict(color=ACCENT, width=2),
                marker=dict(color=ACCENT, size=6),
                connectgaps=False,
                hovertemplate="%{x|%d %b %Y}<br>%{y:.4g}<extra></extra>",
            )
        )

    # Observation rug along the bottom (always visible), on its own thin axis.
    fig.add_trace(
        go.Scatter(
            x=t_sorted,
            y=[0] * len(t_sorted),
            yaxis="y2",
            mode="markers",
            marker=dict(symbol="line-ns-open", color=RUG, size=8, line=dict(width=1)),
            name="observations",
            hovertemplate="%{x|%d %b %Y}<extra>observation</extra>",
            showlegend=False,
        )
    )

    n = len(cols)
    buttons = [
        dict(
            label=c,
            method="update",
            args=[
                {"visible": [j == i for j in range(n)] + [True]},
                {"yaxis.title.text": c},
            ],
        )
        for i, c in enumerate(cols)
    ]

    fig.update_layout(
        title=title,
        template="simple_white",
        font=dict(size=12, color="#1f2937"),
        xaxis=dict(title="Time", domain=[0.0, 1.0], showgrid=False),
        yaxis=dict(title=cols[0], domain=[0.16, 1.0]),
        yaxis2=dict(
            domain=[0.0, 0.08],
            showticklabels=False,
            showgrid=False,
            zeroline=False,
            fixedrange=True,
        ),
        updatemenus=[
            dict(
                buttons=buttons,
                x=1.0,
                xanchor="right",
                y=1.14,
                yanchor="top",
                showactive=True,
                pad=dict(r=4, t=4),
            )
        ],
        margin=dict(l=60, r=20, t=70, b=50),
        showlegend=False,
    )
    fig.add_annotation(
        text="covariate:",
        showarrow=False,
        x=0.80,
        xref="paper",
        y=1.17,
        yref="paper",
        xanchor="right",
        yanchor="top",
        font=dict(size=11, color="#6b7280"),
    )
    return _render(fig)


# ---------------------------------------------------------------------------
# Optional export
# ---------------------------------------------------------------------------
@register(tags=["io"])
def export_labeled_table(
    df: Annotated[
        AnyGeoDataFrame,
        Field(description="The labeled table to export.", exclude=True),
    ],
    export_format: Annotated[
        Literal["both", "csv", "parquet", "none"],
        Field(
            description="Which file(s) to write with all covariate labels. 'both' (default) writes CSV + Parquet; 'none' skips export."
        ),
    ] = "both",
    filename: Annotated[
        str,
        Field(description="Base filename (no extension) for the exported table."),
    ] = "trajectory_covariates",
    root_path: Annotated[
        str,
        Field(description="Directory to write to (the workflow results directory)."),
    ] = "",
) -> AnyGeoDataFrame:
    """Write the labeled table as CSV and/or Parquet, then pass the frame through.

    Parquet keeps the geometry; CSV drops it for a clean tabular file. ``none``
    writes nothing. Returns ``df`` unchanged so it can still feed other widgets.

    Writes go through Ecoscope's own persist helpers (serialize to a buffer, then
    write) so the ``file://`` results-dir URI and cloud paths are handled the same
    way ``persist_df`` handles them -- writing the path directly makes pyarrow raise
    ``ArrowInvalid: Expected a local filesystem path, got a URI``.
    """
    import io

    import geopandas as gpd
    from ecoscope.platform.serde import _persist_bytes, _persist_text

    if export_format in ("parquet", "both"):
        buffer = io.BytesIO()
        has_geom = any(
            isinstance(df[c].dtype, gpd.array.GeometryDtype) for c in df.columns
        )
        if has_geom:
            gpd.GeoDataFrame(df).to_parquet(buffer, index=False)
        else:
            df.to_parquet(buffer, index=False)
        _persist_bytes(buffer.getvalue(), root_path, f"{filename}.parquet")
    if export_format in ("csv", "both"):
        tabular = df.drop(columns=[c for c in ["geometry"] if c in df.columns])
        csv_buffer = io.StringIO()
        tabular.to_csv(csv_buffer)
        _persist_text(csv_buffer.getvalue(), root_path, f"{filename}.csv")
    return df


# ---------------------------------------------------------------------------
# Backwards-compatible single-covariate tasks (unchanged behaviour)
# ---------------------------------------------------------------------------
@register(tags=["io"])
def label_with_static_image(
    client: EarthEngineClient,
    df: Annotated[
        AnyGeoDataFrame,
        Field(
            description="The (geo)dataframe to label. Must carry geometry.",
            exclude=True,
        ),
    ],
    image_id: Annotated[
        str,
        Field(
            description="Full Earth Engine image asset ID (a path, not a band name), e.g. 'USGS/SRTMGL1_003'."
        ),
    ] = "USGS/SRTMGL1_003",
    bands: Annotated[
        Optional[list[str]],
        Field(
            description="Image bands to sample, e.g. ['elevation']. Leave empty to use all bands."
        ),
    ] = None,
    reducer: Annotated[
        str,
        Field(
            description="Region reducer per feature (mean, median, mode, min, max, sum, stdDev, first, toList)."
        ),
    ] = "mean",
    scale: Annotated[
        float,
        Field(
            description="Sampling scale in metres; the image's native resolution is a good default."
        ),
    ] = 500.0,
    df_chunk_size: Annotated[
        int,
        Field(
            description="Features sampled per Earth Engine request. Lower it if you hit GEE quota/memory limits."
        ),
    ] = 5000,
    column_prefix: Annotated[
        Optional[str],
        Field(
            description="Optional prefix for the new covariate column(s), to avoid name clashes."
        ),
    ] = None,
) -> AnyGeoDataFrame:
    """Label each feature with values sampled from a single GEE image."""
    import ee
    from ecoscope.io import eetools

    img = ee.Image(image_id)
    if bands:
        img = img.select(bands)

    labels = eetools.chunk_gdf(
        gdf=df[["geometry"]],
        label_func=_label_gdf_with_img_robust,
        label_func_kwargs={
            "img": img,
            "region_reducer": _build_reducer(reducer),
            "scale": scale,
        },
        df_chunk_size=df_chunk_size,
        max_workers=1,
    )
    if labels is None:
        return df
    if column_prefix:
        labels = labels.add_prefix(column_prefix)
    return df.join(labels)


@register(tags=["io"])
def label_with_temporal_image_collection(
    client: EarthEngineClient,
    df: Annotated[
        AnyGeoDataFrame,
        Field(
            description="The (geo)dataframe to label. Must carry geometry and the time column below.",
            exclude=True,
        ),
    ],
    image_collection_id: Annotated[
        str,
        Field(
            description="Full Earth Engine ImageCollection asset ID (a path, NOT a band name), e.g. 'MODIS/061/MYD13A1'."
        ),
    ] = "MODIS/061/MYD13A1",
    time_column: Annotated[
        str,
        Field(
            description="Datetime column used to pick the temporally closest image(s), e.g. 'segment_start'."
        ),
    ] = "segment_start",
    bands: Annotated[
        Optional[list[str]],
        Field(
            description="Band name(s) within the collection to sample, e.g. ['NDVI']. Leave empty for all bands."
        ),
    ] = ["NDVI"],  # noqa: B006
    image_stack: Annotated[
        int,
        Field(
            description="How many extra images to pull on each side of the image closest in time to a segment. 0 (default) uses only that single closest image."
        ),
    ] = 0,
    reducer: Annotated[
        str,
        Field(
            description="Region reducer per feature (mean, median, mode, min, max, sum, stdDev, first, toList)."
        ),
    ] = "mean",
    scale: Annotated[
        float,
        Field(description="Sampling scale in metres."),
    ] = 500.0,
    df_chunk_size: Annotated[
        int,
        Field(
            description="Features sampled per Earth Engine request. Lower it if you hit GEE quota/memory limits."
        ),
    ] = 5000,
    column_prefix: Annotated[
        Optional[str],
        Field(description="Optional prefix for the new covariate column(s)."),
    ] = None,
) -> AnyGeoDataFrame:
    """Label each feature with a time-matched value from a GEE image collection."""
    import ee
    from ecoscope.io import eetools

    coll = ee.ImageCollection(image_collection_id)
    if bands:
        coll = coll.select(bands)

    labels = eetools.chunk_gdf(
        gdf=df[[time_column, "geometry"]],
        label_func=eetools.label_gdf_with_temporal_image_collection_by_feature,
        label_func_kwargs={
            "img_coll": coll,
            "time_col_name": time_column,
            "n_before": image_stack,
            "n_after": image_stack,
            "n": "images",
            "region_reducer": _build_reducer(reducer),
            "scale": scale,
        },
        df_chunk_size=df_chunk_size,
        max_workers=1,
    )
    if labels is None:
        return df
    if column_prefix:
        rename = {c: f"{column_prefix}{c}" for c in labels.columns if c != "img_date"}
        labels = labels.rename(columns=rename)
    return df.join(labels)
