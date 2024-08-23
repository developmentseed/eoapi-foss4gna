import asyncio
import json
from contextlib import asynccontextmanager
from typing import Annotated, List, Literal, Optional, Union

import folium
import httpx
import shapely.wkt
from fastapi import Depends, FastAPI
from fastapi.exceptions import HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from folium_vectortilelayer import VectorTileLayer
from geojson_pydantic import Feature, FeatureCollection
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, and_, select
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.status import HTTP_400_BAD_REQUEST

from business.logic import __version__ as version
from business.logic.config import Settings
from business.logic.models import (
    LandCoverClass,
    Parcel,
    ParcelGeojson,
    ParcelLandCover,
    ParcelLandCoverCreate,
    ParcelRead,
)
from business.logic.session import engine, get_session

DESCRIPTION = """An application for identifying parcels that have experienced forest
disturbance in recent years. This application uses
[Impact Observatory's Maps for Good](https://www.impactobservatory.com/maps-for-good/)
annual landcover dataset to estimate the proportion of a parcel's area that is covered
by each of the nine landcover classes in each year from 2017 to 2023.\n\n
Users can POST GeoJSON features to the `/parcels` endpoint to load a parcel into the
Postgres database. As a parcel is loaded, the application will use the `/statistics`
endpoint from titiler-pgstac to get area estimates by landcover class for each year and
the results are stored in the `parcellandcover` table.\n\n
The `/map` endpoint returns an interactive leaflet map with vector tiles of parcel 
features served from the `vector` service by `tipg` and raster tiles of the IO landcover
data served from the `raster` service by `titiler-pgstac`.
Users can apply a filter to the `/map` to identify parcels that have undergone 
reductions in forest cover during a specified period, or they can focus the map on a 
specific parcel ID.
"""


IO_LANDCOVER_COLLECTION_ID = "io-10m-annual-lulc"
LANDCOVER = "landcover"
LANDCOVER_ASSET = "supercell"
LANDCOVER_FIRST_YEAR = 2017
LANDCOVER_LAST_YEAR = 2023
SQ_M_AREA_CONVERSION_FACTORS = {"acres": 1 / 4_046.86, "hectares": 1 / 10_000}

IO_LANDCOVER_CLASSIFICATION_VALUES = {
    0: "no data",
    1: "water",
    2: "trees",
    4: "flooded vegetation",
    5: "crops",
    7: "built area",
    8: "bare ground",
    9: "snow/ice",
    10: "clouds",
    11: "rangeland",
}

PARCEL_OPS = "Parcel Operations"
MAPPING = "Mapping"

settings = Settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    yield

    # shutdown


Session = Annotated[AsyncSession, Depends(get_session)]

app = FastAPI(
    title="Business Logic",
    description=DESCRIPTION,
    version=version,
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def read_root():
    return RedirectResponse(url="/docs")


@app.post("/bootstrap-data", include_in_schema=False)
async def bootstrap_data(session: Session) -> List[LandCoverClass]:
    entries = []
    for value, description in IO_LANDCOVER_CLASSIFICATION_VALUES.items():
        landcoverclass = LandCoverClass(value=value, description=description)
        entries.append(landcoverclass)

    session.add_all(entries)

    await session.commit()

    return entries


def register_landcover_stac_search(year: int):
    register_search_request = httpx.post(
        f"{settings.raster_endpoint}/searches/register",
        json={
            "collections": [IO_LANDCOVER_COLLECTION_ID],
            "datetime": f"{year}-01-01T00:00:00Z/{year}-12-31T23:59:59Z",
        },
    )
    register_search_request.raise_for_status()

    return register_search_request.json()["id"]


async def summarize_land_cover(
    parcel: Parcel, year: int
) -> List[ParcelLandCoverCreate]:
    if not parcel.id and parcel.area_sq_m:
        raise ValueError("this parcel does not have an id and positive area!")

    parcel_geojson = ParcelGeojson(
        type="Feature",
        geometry=shapely.wkt.loads(parcel.geometry),
        properties=ParcelRead(id=parcel.id, area_sq_m=parcel.area_sq_m),
    )

    search_id = register_landcover_stac_search(year=year)

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{settings.raster_endpoint}/searches/{search_id}/statistics",
            json=parcel_geojson.model_dump(),
            params={
                "assets": LANDCOVER_ASSET,
                "categorical": True,
            },
        )

    response.raise_for_status()
    statistics_report = response.json()

    statistics = statistics_report["properties"]["statistics"][f"{LANDCOVER_ASSET}_b1"]

    summaries = []
    for count, value in zip(statistics["histogram"][0], statistics["histogram"][1]):
        value_area = parcel.area_sq_m * count / statistics["valid_pixels"]
        summaries.append(
            ParcelLandCoverCreate(
                parcel_id=parcel.id,
                value=value,
                year=year,
                area_sq_m=value_area,
            )
        )

    return summaries


@app.post("/parcels", tags=[PARCEL_OPS])
async def create_parcel(
    session: Session, geojson: Union[Feature, FeatureCollection]
) -> List[str]:
    if isinstance(geojson, Feature):
        geojson = FeatureCollection(
            type="FeatureCollection",
            features=[geojson],
        )

    ids = []
    try:
        for feature in geojson.features:
            parcel = Parcel(
                id=feature.properties["id"],
                geometry=feature.geometry.wkt,
            )
            session.add(parcel)
            await session.flush()  # Ensure parcel.id is populated
            ids.append(parcel.id)

            summarization_tasks = [
                summarize_land_cover(parcel=parcel, year=year)
                for year in range(LANDCOVER_FIRST_YEAR, LANDCOVER_LAST_YEAR + 1)
            ]

            results = await asyncio.gather(*summarization_tasks)

            for summaries in results:
                for summary in summaries:
                    session.add(ParcelLandCover(**summary.model_dump()))

        await session.commit()

    except IntegrityError as e:
        await session.rollback()  # Rollback the transaction on error

        if "uq_geometry" in str(e.orig):
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail="parcel with this geometry already exists.",
            )
        else:
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail="Unexpected error occurred - detailed description here: "
                + str(e.orig),
            )

    return ids


@app.get("/parcels/{id}/landcover_summary", tags=[PARCEL_OPS])
async def get_landcover_summary(
    session: Session,
    id: str,
    year: int = 2023,
    units: Literal["acres", "hectares"] = "acres",
) -> dict[str, float]:
    unit_multiplier = SQ_M_AREA_CONVERSION_FACTORS.get(units)
    statement = (
        select(LandCoverClass.description, ParcelLandCover.area_sq_m * unit_multiplier)
        .join(LandCoverClass, LandCoverClass.value == ParcelLandCover.value)
        .where(and_(ParcelLandCover.parcel_id == id, ParcelLandCover.year == year))
    )
    result = await session.exec(statement)

    return {description: round(area, 2) for description, area in result.all()}


@app.get("/map", tags=[MAPPING])
async def load_map(
    id: Optional[str] = None,
    start_year: Optional[int] = None,
    end_year: Optional[int] = None,
    area_threshold: Optional[int] = None,
):
    # get parcels extent
    parcels_collection_request = httpx.get(
        f"{settings.vector_endpoint}/collections/business.parcel", timeout=10
    )
    parcels_collection_request.raise_for_status()
    parcels_collection_info = parcels_collection_request.json()
    xmin, ymin, xmax, ymax = parcels_collection_info["extent"]["spatial"]["bbox"][0]

    # get vector tile info
    parcel_tilejson_params = {}

    # if specified, pull vector tiles for areas that experienced a forest disturbance
    if start_year and end_year and area_threshold:
        title = (
            f"parcels with forest disturbances over {area_threshold} square "
            f"meters between {start_year} and {end_year}"
        )

        parcel_tilejson_params["start_year"] = start_year
        parcel_tilejson_params["end_year"] = end_year
        parcel_tilejson_params["area_threshold"] = area_threshold
        parcel_tilejson_request = httpx.get(
            f"{settings.vector_endpoint}/collections/pg_temp.forest_disturbance/WebMercatorQuad/tilejson.json",
            params=parcel_tilejson_params,
        )

        parcel_tilejson_request.raise_for_status()
        parcel_tilejson = parcel_tilejson_request.json()

    # otherwise just pull tiles from a single parcel or all properties
    else:
        if id:
            title = f"parcel_id = {id}"
            parcel_tilejson_params["ids"] = [id]
        else:
            title = "all parcels"

        parcel_tilejson_request = httpx.get(
            f"{settings.vector_endpoint}/collections/business.parcel/WebMercatorQuad/tilejson.json",
            params=parcel_tilejson_params,
        )

    parcel_tilejson_request.raise_for_status()
    parcel_tilejson = parcel_tilejson_request.json()

    parcel_tilejson["tiles"][0] = parcel_tilejson["tiles"][0].replace(
        "http://vector:8083", "http://localhost:8083"
    )

    # set the bbox of the map using either the item's bbox or the bbox of all properties
    if id:
        feature_request = httpx.get(
            f"{settings.vector_endpoint}/collections/business.parcel/items/{id}?bbox-only=true&f=geojson"
        )
        feature_request.raise_for_status()
        feature_bbox = feature_request.json()

        bbox_coords = feature_bbox["geometry"]["coordinates"][0]
        xmin, ymin = bbox_coords[0]
        xmax, ymax = bbox_coords[2]

    map_bounds = [[ymin, xmin], [ymax, xmax]]

    # get raster tile info based on the renders params for the landcover STAC collection
    landcover_info_request = httpx.get(
        f"{settings.raster_endpoint}/collections/{IO_LANDCOVER_COLLECTION_ID}/info"
    )
    landcover_info_request.raise_for_status()
    landcover_info = landcover_info_request.json()

    landcover_viz_params = landcover_info["search"]["metadata"]["defaults"][LANDCOVER]

    # the colormap gets returned as a dictionary, need to provide it as a string
    landcover_viz_params["colormap"] = json.dumps(landcover_viz_params["colormap"])

    # generate tilejson.json for each year in the IO Landcover collection
    landcover_tilejsons = {}
    for year in range(LANDCOVER_FIRST_YEAR, LANDCOVER_LAST_YEAR + 1):
        search_id = register_landcover_stac_search(year=year)
        landcover_tilejson_request = httpx.get(
            f"{settings.raster_endpoint}/searches/{search_id}/WebMercatorQuad/tilejson.json",
            params=landcover_viz_params,
        )
        landcover_tilejson_request.raise_for_status()
        landcover_tilejson_response = landcover_tilejson_request.json()
        landcover_tilejson_response["tiles"][0] = landcover_tilejson_response["tiles"][
            0
        ].replace("http://raster:8082", "http://localhost:8082")
        landcover_tilejsons[year] = landcover_tilejson_response

    # build a map with folium!
    folium_map = folium.Map(location=[0, 0], zoom_start=2)
    folium_map.fit_bounds(bounds=map_bounds)

    # add NAIP imagery
    folium.TileLayer(
        tiles="https://gis.apfo.usda.gov/arcgis/rest/services/NAIP/USDA_CONUS_PRIME/ImageServer/tile/{z}/{y}/{x}",
        attr="USDA NAIP",
        overlay=False,
        name="USDA NAIP",
        show=False,
    ).add_to(folium_map)

    # add landcover layer for each year
    for year, tilejson in landcover_tilejsons.items():
        folium.TileLayer(
            tiles=tilejson["tiles"][0],
            attr="Impact Observatory Maps for Good",
            overlay=True,
            name=f"IO Landcover {year}",
            show=False,
        ).add_to(folium_map)

    parcel_vector_tiles = VectorTileLayer(
        url=parcel_tilejson["tiles"][0],
        name="parcel boundaries",
        options={
            "layers": ["default"],
            "minZoom": 8,
            "maxZoom": 22,
            "minDetailZoom": 10,
            "maxDetailZoom": 13,
            "vectorTileLayerStyles": {
                "default": {
                    "fill": True,
                    "fillOpacity": 0,
                    "weight": 2,
                    "color": "orange",
                    "opacity": 1,
                    "interactive": True,
                },
            },
        },
    )
    parcel_vector_tiles.add_to(folium_map)

    folium.LayerControl(collapsed=False).add_to(folium_map)
    title_html = f"""
    <h3 align="center" style="font-size:20px"><b>Business Map: {title}</b></h3>
    """
    folium_map.get_root().html.add_child(folium.Element(title_html))

    map_html = folium_map._repr_html_()

    return HTMLResponse(content=map_html)
