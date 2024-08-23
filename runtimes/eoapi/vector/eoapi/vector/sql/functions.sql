-- function to return parcel that experienced a loss of forest
CREATE OR REPLACE FUNCTION pg_temp.forest_disturbance(
    IN start_year int,
    IN end_year int,
    IN area_threshold int DEFAULT 200000, -- 20 hectares
    IN bounds geometry DEFAULT 'srid=4326;POLYGON((-180 -90,-180 90,180 90,180 -90,-180 -90))'::geometry,
    OUT id text,
    OUT geom geometry,
    OUT content jsonb
) RETURNS SETOF RECORD AS $$
DECLARE
    intersections RECORD;
    _start_year int := start_year;
    _end_year int := end_year;
    _area_threshold integer := area_threshold;
    _scanlimit int := 10000; -- remove if add params back in
    fields jsonb := '{}'::jsonb; -- remove if add params back in
    curs refcursor;
    query text;
    iter_record RECORD;
    remaining_limit int := _scanlimit;

BEGIN
    SET search_path TO business, public;

    query := format('
    WITH start_landcover as (
        SELECT
            parcel_id,
            SUM(area_sq_m) as start_forest_area
        FROM parcellandcover
        WHERE
            year = %L AND value in (2, 4)
        GROUP BY parcel_id
    ),
    end_landcover as (
        SELECT
            parcel_id,
            SUM(area_sq_m) as end_forest_area
        FROM parcellandcover
        WHERE
            year = %L AND value in (2, 4)
        GROUP BY parcel_id
    )

    SELECT 
        parcel.id as parcel_id,
        start_forest_area,
        end_forest_area,
        geometry
    FROM parcel
    JOIN start_landcover
    ON
      parcel.id = start_landcover.parcel_id
    JOIN end_landcover
    ON
      parcel.id = end_landcover.parcel_id
    WHERE
        ST_Intersects(parcel.geometry, %L::geometry) AND
        (start_landcover.start_forest_area - end_landcover.end_forest_area) >= %L
    ', _start_year, _end_year, bounds, _area_threshold);

    IF st_srid(bounds) != 4326 THEN
        bounds := ST_Transform(bounds, 4326);
    END IF;


    OPEN curs FOR EXECUTE query;
    LOOP
        FETCH curs INTO iter_record;
        EXIT WHEN NOT FOUND;

        id := iter_record.parcel_id;
        geom := iter_record.geometry;
        content := to_json(iter_record)::jsonb;
        RETURN NEXT;

    END LOOP;
    CLOSE curs;

    RETURN;
END;
$$ LANGUAGE PLPGSQL;
