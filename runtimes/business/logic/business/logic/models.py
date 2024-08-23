from typing import Optional

from geoalchemy2 import Geometry
from geojson_pydantic import Feature
from geojson_pydantic.geometries import Geometry as GeojsonGeometry
from pydantic import BaseModel
from sqlalchemy.orm.events import event
from sqlmodel import Column, Field, SQLModel, UniqueConstraint, text
from starlette.responses import JSONResponse


class LandCoverClass(SQLModel, table=True):
    value: int = Field(primary_key=True)
    description: str


class ParcelCreate(BaseModel):
    id: str = Field(primary_key=True)
    geometry: str = Field(
        default=None, sa_column=Column(Geometry("MULTIPOLYGON", srid=4326))
    )


class Parcel(ParcelCreate, SQLModel, table=True):
    area_sq_m: Optional[float] = Field(default=0)

    __table_args__ = (UniqueConstraint("geometry", name="uq_geometry"),)


@event.listens_for(Parcel, "before_insert")
def calculate_area(mapper, connection, target):
    if target.geometry:
        result = connection.execute(
            text("""
                SELECT ST_Area(CAST(:geom AS geography)) AS area
            """),
            {"geom": target.geometry},
        )
        target.area_sq_m = result.scalar()


class ParcelRead(BaseModel):
    id: str
    area_sq_m: float

    model_config = {"extra": "allow"}


ParcelGeojson = Feature[GeojsonGeometry, ParcelRead]


class ParcelLandCoverCreate(BaseModel):
    parcel_id: str = Field(foreign_key="parcel.id")
    value: int = Field(index=True, foreign_key="landcoverclass.value")

    year: int = Field(index=True)
    area_sq_m: float


class ParcelLandCover(ParcelLandCoverCreate, SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    __table_args__ = (UniqueConstraint("parcel_id", "year", "value"),)


class GeoJSONResponse(JSONResponse):
    """GeoJSON Response"""

    media_type = "application/geo+json"
