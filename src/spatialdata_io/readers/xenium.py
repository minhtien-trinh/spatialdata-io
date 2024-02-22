from __future__ import annotations

import json
import os
import re
import tempfile
import warnings
import zipfile
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Optional

import dask.array as da
import numpy as np
import packaging.version
import pandas as pd
import pyarrow.parquet as pq
import tifffile
import zarr
from anndata import AnnData
from dask.dataframe import read_parquet
from dask_image.imread import imread
from geopandas import GeoDataFrame
from joblib import Parallel, delayed
from multiscale_spatial_image.multiscale_spatial_image import MultiscaleSpatialImage
from pyarrow import Table
from shapely import Polygon
from spatial_image import SpatialImage
from spatialdata import SpatialData
from spatialdata._types import ArrayLike
from spatialdata.models import (
    Image2DModel,
    Labels2DModel,
    PointsModel,
    ShapesModel,
    TableModel,
)
from spatialdata.transformations.transformations import Affine, Identity, Scale

from spatialdata_io._constants._constants import XeniumKeys
from spatialdata_io._docs import inject_docs
from spatialdata_io._utils import deprecation_alias
from spatialdata_io.readers._utils._read_10x_h5 import _read_10x_h5

__all__ = ["xenium", "xenium_aligned_image", "xenium_explorer_selection"]


@deprecation_alias(cells_as_shapes="cells_as_circles")
@inject_docs(xx=XeniumKeys)
def xenium(
    path: str | Path,
    n_jobs: int = 1,
    cells_as_circles: bool = True,
    cell_boundaries: bool = True,
    nucleus_boundaries: bool = True,
    cell_labels: bool = True,
    nucleus_labels: bool = True,
    transcripts: bool = True,
    morphology_mip: bool = True,
    morphology_focus: bool = True,
    imread_kwargs: Mapping[str, Any] = MappingProxyType({}),
    image_models_kwargs: Mapping[str, Any] = MappingProxyType({}),
    labels_models_kwargs: Mapping[str, Any] = MappingProxyType({}),
) -> SpatialData:
    """
    Read a *10X Genomics Xenium* dataset into a SpatialData object.

    This function reads the following files:

        - ``{xx.XENIUM_SPECS!r}``: File containing specifications.
        - ``{xx.NUCLEUS_BOUNDARIES_FILE!r}``: Polygons of nucleus boundaries.
        - ``{xx.CELL_BOUNDARIES_FILE!r}``: Polygons of cell boundaries.
        - ``{xx.TRANSCRIPTS_FILE!r}``: File containing transcripts.
        - ``{xx.CELL_FEATURE_MATRIX_FILE!r}``: File containing cell feature matrix.
        - ``{xx.CELL_METADATA_FILE!r}``: File containing cell metadata.
        - ``{xx.MORPHOLOGY_MIP_FILE!r}``: File containing morphology mip.
        - ``{xx.MORPHOLOGY_FOCUS_FILE!r}``: File containing morphology focus.

    .. seealso::

        - `10X Genomics Xenium file format  <https://cf.10xgenomics.com/supp/xenium/xenium_documentation.html>`_.

    Parameters
    ----------
    path
        Path to the dataset.
    n_jobs
        Number of jobs to use for parallel processing.
    cells_as_circles
        Whether to read cells also as circles. Useful for performant visualization.
    cell_boundaries
        Whether to read cell boundaries (polygons).
    nucleus_boundaries
        Whether to read nucleus boundaries (polygons).
    cell_labels
        Whether to read cell labels (raster). The polygonal version of the cell labels are simplified
        for visualization purposes, and using the raster version is recommended for analysis.
    nucleus_labels
        Whether to read nucleus labels (raster). The polygonal version of the nucleus labels are simplified
        for visualization purposes, and using the raster version is recommended for analysis.
    transcripts
        Whether to read transcripts.
    morphology_mip
        Whether to read morphology mip.
    morphology_focus
        Whether to read morphology focus.
    imread_kwargs
        Keyword arguments to pass to the image reader.
    image_models_kwargs
        Keyword arguments to pass to the image models.
    labels_models_kwargs
        Keyword arguments to pass to the labels models.

    Returns
    -------
    :class:`spatialdata.SpatialData`
    """
    image_models_kwargs = dict(image_models_kwargs)
    if "chunks" not in image_models_kwargs:
        image_models_kwargs["chunks"] = (1, 4096, 4096)
    if "scale_factors" not in image_models_kwargs:
        image_models_kwargs["scale_factors"] = [2, 2, 2, 2]

    labels_models_kwargs = dict(labels_models_kwargs)
    if "chunks" not in labels_models_kwargs:
        labels_models_kwargs["chunks"] = (4096, 4096)
    if "scale_factors" not in labels_models_kwargs:
        labels_models_kwargs["scale_factors"] = [2, 2, 2, 2]

    path = Path(path)
    with open(path / XeniumKeys.XENIUM_SPECS) as f:
        specs = json.load(f)
    # to trigger the warning if the version cannot be parsed
    version = _parse_version_of_xenium_analyzer(specs, hide_warning=False)

    specs["region"] = "cell_circles" if cells_as_circles else "cell_boundaries"

    return_values = _get_tables_and_circles(path, cells_as_circles, specs)
    if cells_as_circles:
        table, circles = return_values
    else:
        table = return_values
    polygons = {}
    labels = {}

    if nucleus_labels:
        labels["nucleus_labels"] = _get_labels(
            path,
            XeniumKeys.CELLS_ZARR,
            specs,
            mask_index=0,
            idx=table.obs[str(XeniumKeys.CELL_ID)].copy(),
            labels_models_kwargs=labels_models_kwargs,
        )
    if cell_labels:
        labels["cell_labels"] = _get_labels(
            path,
            XeniumKeys.CELLS_ZARR,
            specs,
            mask_index=1,
            idx=table.obs[str(XeniumKeys.CELL_ID)].copy(),
            labels_models_kwargs=labels_models_kwargs,
        )

    if nucleus_boundaries:
        polygons["nucleus_boundaries"] = _get_polygons(
            path,
            XeniumKeys.NUCLEUS_BOUNDARIES_FILE,
            specs,
            n_jobs,
            idx=table.obs[str(XeniumKeys.CELL_ID)].copy(),
        )

    if cell_boundaries:
        polygons["cell_boundaries"] = _get_polygons(
            path,
            XeniumKeys.CELL_BOUNDARIES_FILE,
            specs,
            n_jobs,
            idx=table.obs[str(XeniumKeys.CELL_ID)].copy(),
        )

    points = {}
    if transcripts:
        points["transcripts"] = _get_points(path, specs)

    images = {}
    if version < packaging.version.parse("2.0.0"):
        if morphology_mip:
            images["morphology_mip"] = _get_images(
                path,
                XeniumKeys.MORPHOLOGY_MIP_FILE,
                imread_kwargs,
                image_models_kwargs,
            )
        if morphology_focus:
            images["morphology_focus"] = _get_images(
                path,
                XeniumKeys.MORPHOLOGY_FOCUS_FILE,
                imread_kwargs,
                image_models_kwargs,
            )
    else:
        if morphology_focus:
            morphology_focus_dir = path / XeniumKeys.MORPHOLOGY_FOCUS_DIR
            files = {f for f in os.listdir(morphology_focus_dir) if f.endswith(".ome.tif")}
            assert files == {XeniumKeys.MORPHOLOGY_FOCUS_CHANNEL_IMAGE.format(i) for i in range(4)}  # type: ignore[str-format]
            # the 'dummy' channel is a temporary workaround, see _get_images() for more details
            channel_names = {
                0: XeniumKeys.MORPHOLOGY_FOCUS_CHANNEL_0,
                1: XeniumKeys.MORPHOLOGY_FOCUS_CHANNEL_1,
                2: XeniumKeys.MORPHOLOGY_FOCUS_CHANNEL_2,
                3: XeniumKeys.MORPHOLOGY_FOCUS_CHANNEL_3,
                4: "dummy",
            }
            # this reads the scale 0 for all the 4 channels (the other files are parsed automatically)
            # dask.image.imread will call tifffile.imread which will give a warning saying that reading multi-file pyramids
            # is not supported; since we are reading the full scale image and reconstructing the pyramid, we can ignore this
            import logging

            class IgnoreSpecificMessage(logging.Filter):
                def filter(self, record: logging.LogRecord) -> bool:
                    # Ignore specific log message
                    if "OME series cannot read multi-file pyramids" in record.getMessage():
                        return False
                    return True

            logger = tifffile.logger()
            logger.addFilter(IgnoreSpecificMessage())
            image_models_kwargs = dict(image_models_kwargs)
            assert (
                "c_coords" not in image_models_kwargs
            ), "The channel names for the morphology focus images are handled internally"
            image_models_kwargs["c_coords"] = list(channel_names.values())
            images["morphology_focus"] = _get_images(
                morphology_focus_dir,
                XeniumKeys.MORPHOLOGY_FOCUS_CHANNEL_IMAGE.format(0),  # type: ignore[str-format]
                imread_kwargs,
                image_models_kwargs,
            )
            del image_models_kwargs["c_coords"]
            logger.removeFilter(IgnoreSpecificMessage())

    elements_dict = {"images": images, "labels": labels, "points": points, "table": table, "shapes": polygons}
    if cells_as_circles:
        elements_dict["shapes"][specs["region"]] = circles
    sdata = SpatialData(**elements_dict)

    # find and add additional aligned images
    aligned_images = _add_aligned_images(path, imread_kwargs, image_models_kwargs)
    for key, value in aligned_images.items():
        sdata.images[key] = value

    return sdata


def _decode_cell_id_column(cell_id_column: pd.Series) -> pd.Series:
    if isinstance(cell_id_column.iloc[0], bytes):
        return cell_id_column.apply(lambda x: x.decode("utf-8"))
    return cell_id_column


def _get_polygons(
    path: Path, file: str, specs: dict[str, Any], n_jobs: int, idx: Optional[ArrayLike] = None
) -> GeoDataFrame:
    def _poly(arr: ArrayLike) -> Polygon:
        return Polygon(arr[:-1])

    # seems to be faster than pd.read_parquet
    df = pq.read_table(path / file).to_pandas()

    group_by = df.groupby(XeniumKeys.CELL_ID)
    index = pd.Series(group_by.indices.keys())
    index = _decode_cell_id_column(index)
    out = Parallel(n_jobs=n_jobs)(
        delayed(_poly)(i.to_numpy())
        for _, i in group_by[[XeniumKeys.BOUNDARIES_VERTEX_X, XeniumKeys.BOUNDARIES_VERTEX_Y]]
    )
    geo_df = GeoDataFrame({"geometry": out})
    version = _parse_version_of_xenium_analyzer(specs)
    if version is not None and version < packaging.version.parse("2.0.0"):
        assert idx is not None
        assert len(idx) == len(geo_df)
        assert np.unique(geo_df.index).size == len(geo_df)
        assert index.equals(idx)
        geo_df.index = idx
    else:
        geo_df.index = index
        if not np.unique(geo_df.index).size == len(geo_df):
            warnings.warn(
                "Found non-unique polygon indices, this will be addressed in a future version of the reader. For the "
                "time being please consider merging non-unique polygons into single multi-polygons.",
                stacklevel=2,
            )
    scale = Scale([1.0 / specs["pixel_size"], 1.0 / specs["pixel_size"]], axes=("x", "y"))
    return ShapesModel.parse(geo_df, transformations={"global": scale})


def _get_labels(
    path: Path,
    file: str,
    specs: dict[str, Any],
    mask_index: int,
    idx: Optional[ArrayLike] = None,
    labels_models_kwargs: Mapping[str, Any] = MappingProxyType({}),
) -> GeoDataFrame:
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_file = path / XeniumKeys.CELLS_ZARR
        with zipfile.ZipFile(zip_file, "r") as zip_ref:
            zip_ref.extractall(tmpdir)

        with zarr.open(str(tmpdir), mode="r") as z:
            masks = z["masks"][f"{mask_index}"][...]
            return Labels2DModel.parse(
                masks, dims=("y", "x"), transformations={"global": Identity()}, **labels_models_kwargs
            )

            # cells.zarr.zip/cells_summary/ are different from version 2.0.0
            # version = _parse_version_of_xenium_analyzer(specs)
            # if version is not None and version < packaging.version.parse("2.0.0"):
            #     pass
            # else:
            #     pass


def _get_points(path: Path, specs: dict[str, Any]) -> Table:
    table = read_parquet(path / XeniumKeys.TRANSCRIPTS_FILE)
    table["feature_name"] = table["feature_name"].apply(
        lambda x: x.decode("utf-8") if isinstance(x, bytes) else str(x), meta=("feature_name", "object")
    )

    transform = Scale([1.0 / specs["pixel_size"], 1.0 / specs["pixel_size"]], axes=("x", "y"))
    points = PointsModel.parse(
        table,
        coordinates={"x": XeniumKeys.TRANSCRIPTS_X, "y": XeniumKeys.TRANSCRIPTS_Y, "z": XeniumKeys.TRANSCRIPTS_Z},
        feature_key=XeniumKeys.FEATURE_NAME,
        instance_key=XeniumKeys.CELL_ID,
        transformations={"global": transform},
    )
    return points


def _get_tables_and_circles(
    path: Path, cells_as_circles: bool, specs: dict[str, Any]
) -> AnnData | tuple[AnnData, AnnData]:
    adata = _read_10x_h5(path / XeniumKeys.CELL_FEATURE_MATRIX_FILE)
    metadata = pd.read_parquet(path / XeniumKeys.CELL_METADATA_FILE)
    np.testing.assert_array_equal(metadata.cell_id.astype(str), adata.obs_names.values)
    circ = metadata[[XeniumKeys.CELL_X, XeniumKeys.CELL_Y]].to_numpy()
    adata.obsm["spatial"] = circ
    metadata.drop([XeniumKeys.CELL_X, XeniumKeys.CELL_Y], axis=1, inplace=True)
    adata.obs = metadata
    adata.obs["region"] = specs["region"]
    adata.obs["region"] = adata.obs["region"].astype("category")
    adata.obs[XeniumKeys.CELL_ID] = _decode_cell_id_column(adata.obs[XeniumKeys.CELL_ID])
    table = TableModel.parse(adata, region=specs["region"], region_key="region", instance_key=str(XeniumKeys.CELL_ID))
    if cells_as_circles:
        transform = Scale([1.0 / specs["pixel_size"], 1.0 / specs["pixel_size"]], axes=("x", "y"))
        radii = np.sqrt(adata.obs[XeniumKeys.CELL_NUCLEUS_AREA].to_numpy() / np.pi)
        circles = ShapesModel.parse(
            circ,
            geometry=0,
            radius=radii,
            transformations={"global": transform},
            index=adata.obs[XeniumKeys.CELL_ID].copy(),
        )
        return table, circles
    return table


def _get_images(
    path: Path,
    file: str,
    imread_kwargs: Mapping[str, Any] = MappingProxyType({}),
    image_models_kwargs: Mapping[str, Any] = MappingProxyType({}),
) -> SpatialImage | MultiscaleSpatialImage:
    image = imread(path / file, **imread_kwargs)
    # Napari currently interprets 4 channel images as RGB; a series of PRs to fix this is almost ready but they will not
    # be merged soon.
    # Here, since the new data from the xenium analyzer version 2.0.0 gives 4-channel images that are not RGBA, let's
    # add a dummy channel as a temporary workaround.
    image = da.concatenate([image, da.zeros_like(image[0:1])], axis=0)
    return Image2DModel.parse(
        image, transformations={"global": Identity()}, dims=("c", "y", "x"), **image_models_kwargs
    )


def _add_aligned_images(
    path: Path,
    imread_kwargs: Mapping[str, Any] = MappingProxyType({}),
    image_models_kwargs: Mapping[str, Any] = MappingProxyType({}),
) -> dict[str, MultiscaleSpatialImage]:
    """Discover and parse aligned images."""
    images = {}
    ome_tif_files = list(path.glob("*.ome.tif"))
    csv_files = list(path.glob("*.csv"))
    for file in ome_tif_files:
        element_name = None
        for suffix in [XeniumKeys.ALIGNED_HE_IMAGE_SUFFIX, XeniumKeys.ALIGNED_IF_IMAGE_SUFFIX]:
            if file.name.endswith(suffix):
                element_name = suffix.replace(XeniumKeys.ALIGNMENT_FILE_SUFFIX_TO_REMOVE, "")
                break
        if element_name is not None:
            # check if an alignment file exists
            expected_filename = file.name.replace(
                XeniumKeys.ALIGNMENT_FILE_SUFFIX_TO_REMOVE, XeniumKeys.ALIGNMENT_FILE_SUFFIX_TO_ADD
            )
            alignment_files = [f for f in csv_files if f.name == expected_filename]
            assert len(alignment_files) <= 1, f"Found more than one alignment file for {file.name}."
            alignment_file = alignment_files[0] if alignment_files else None

            # parse the image
            image = xenium_aligned_image(file, alignment_file, imread_kwargs, image_models_kwargs)
            images[element_name] = image
    return images


def xenium_aligned_image(
    image_path: str | Path,
    alignment_file: str | Path | None,
    imread_kwargs: Mapping[str, Any] = MappingProxyType({}),
    image_models_kwargs: Mapping[str, Any] = MappingProxyType({}),
) -> MultiscaleSpatialImage:
    """
    Read an image aligned to a Xenium dataset, with an optional alignment file.

    Parameters
    ----------
    image_path
        Path to the image.
    alignment_file
        Path to the alignment file, if not passed it is assumed that the image is aligned.
    image_models_kwargs
        Keyword arguments to pass to the image models.

    Returns
    -------
    The single-scale or multi-scale aligned image element.
    """
    image_path = Path(image_path)
    assert image_path.exists(), f"File {image_path} does not exist."
    image = imread(image_path, **imread_kwargs)

    # Depending on the version of pipeline that was used, some images have shape (1, y, x, 3) and others (3, y, x) or
    # (4, y, x).
    # since y and x are always different from 1, let's differentiate from the two cases here, independently of the
    # pipeline version.
    # Note that a more robust approach is to look at the xml metadata in the ome.tif; we should use this in a future PR.
    # In fact, it could be that the len(image.shape) == 4 has actually dimes (1, x, y, c) and not (1, y, x, c). This is
    # not a problem because the transformation is constructed to be consistent, but if is the case, the data orientation
    # would be transposed compared to the original image, not ideal.
    # print(image.shape)
    if len(image.shape) == 4:
        assert image.shape[0] == 1
        assert image.shape[-1] == 3
        image = image.squeeze(0)
        dims = ("y", "x", "c")
    else:
        assert len(image.shape) == 3
        assert image.shape[0] in [3, 4]
        if image.shape[0] == 4:
            # as explained before in _get_images(), we need to add a dummy channel until we support 4-channel images as
            # non-RGBA images in napari
            image = da.concatenate([image, da.zeros_like(image[0:1])], axis=0)
        dims = ("c", "y", "x")

    if alignment_file is None:
        transformation = Identity()
    else:
        alignment_file = Path(alignment_file)
        assert alignment_file.exists(), f"File {alignment_file} does not exist."
        alignment = pd.read_csv(alignment_file, header=None).values
        transformation = Affine(alignment, input_axes=("x", "y"), output_axes=("x", "y"))

    return Image2DModel.parse(
        image,
        dims=dims,
        transformations={"global": transformation},
        **image_models_kwargs,
    )


def xenium_explorer_selection(path: str | Path, pixel_size: float = 0.2125) -> Polygon:
    """Read the coordinates of a selection `.csv` file exported from the `Xenium Explorer  <https://www.10xgenomics.com/support/software/xenium-explorer/latest>`_.

    This file can be generated by the "Freehand Selection" or the "Rectangular Selection".
    The output `Polygon` can be used for a polygon query on the pixel coordinate
    system (by default, this is the `"global"` coordinate system for Xenium data).
    If `spatialdata_xenium_explorer  <https://github.com/quentinblampey/spatialdata_xenium_explorer>`_ was used,
    the `pixel_size` argument must be set to the one used during conversion with `spatialdata_xenium_explorer`.

    Parameters
    ----------
    path
        Path to the `.csv` file containing the selection coordinates
    pixel_size
        Size of a pixel in microns. By default, the Xenium pixel size is used.

    Returns
    -------
    :class:`shapely.geometry.polygon.Polygon`
    """
    df = pd.read_csv(path, skiprows=2)
    return Polygon(df.values / pixel_size)


def _parse_version_of_xenium_analyzer(
    specs: dict[str, Any],
    hide_warning: bool = True,
) -> packaging.version.Version | None:
    string = specs[XeniumKeys.ANALYSIS_SW_VERSION]
    pattern = r"^xenium-(\d+\.\d+\.\d+(\.\d+-\d+)?)"

    result = re.search(pattern, string)
    # Example
    # Input: xenium-2.0.0.6-35-ga7e17149a
    # Output: 2.0.0.6-35

    warning_message = f"Could not parse the version of the Xenium Analyzer from the string: {string}. This may happen for experimental version of the data. Please report in GitHub https://github.com/scverse/spatialdata-io/issues.\nThe reader will continue assuming the latest version of the Xenium Analyzer."

    if result is None:
        if not hide_warning:
            warnings.warn(warning_message, stacklevel=2)
        return None

    group = result.groups()[0]
    try:
        return packaging.version.parse(group)
    except packaging.version.InvalidVersion:
        if not hide_warning:
            warnings.warn(warning_message, stacklevel=2)
        return None
