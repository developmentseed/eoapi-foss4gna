
<p align="center">
  <img width="500" alt="eoapi-devseed" src="https://github.com/developmentseed/eoapi-devseed/assets/10407788/fc69e5ae-4ab7-491f-8c20-6b9e1372b4c6">
  <p align="center">Example of eoAPI customization prepared for FOSS4GNA 2024.</p>
</p>

---

**Documentation**: <a href="https://eoapi.dev/customization/" target="_blank">https://eoapi.dev/customization/</a>

**Source Code**: <a href="https://github.com/developmentseed/eoapi-foss4gna" target="_blank">https://github.com/developmentseed/eoapi-foss4gna</a>

---

This repository shows an example of how users can build a business application based on eoAPI services, starting from [eoapi-devseed](https://github.com/developmentseed/eoapi-devseed).

## Custom

### Runtimes

#### business.logic

A FastAPI application backed by a Postgres database with functions for finding suitable parcels for natural capital projects, like forested areas that have experienced disturbance in recent years (e.g. wildfire, timber harvesting, etc).

- `/parcels`: `POST` GeoJSON features to the `parcel` table in the database
- `/parcels/{id}/landcover_summary`: Return the area covered by each landcover class in a given year for a parcel
- `/map`: Load a `leaflet` map with vector tile features from the `parcel` table. Optionally filter down to features that have experienced disturbance in forested areas over a specified time period.

#### eoapi.stac

Built on [stac-fastapi.pgstac](https://github.com/stac-utils/stac-fastapi-pgstac) application, adding a **`TiTilerExtension`** and a simple **`Search Viewer`**.

When the `EOAPI_STAC_TITILER_ENDPOINT` environment variable is set (pointing to the `raster` application) and `titiler` extension is enabled, additional endpoints will be added to the stac-fastapi application (see: [stac/extension.py](https://github.com/developmentseed/eoapi-devseed/blob/main/runtimes/eoapi/stac/eoapi/stac/extension.py)):

- `/collections/{collectionId}/items/{itemId}/tilejson.json`: Return the `raster` tilejson for an item
- `/collections/{collectionId}/items/{itemId}/viewer`: Redirect to the `raster` viewer

#### eoapi.raster

The dynamic tiler deployed within `eoapi-devseed` is built on top of [titiler-pgstac](https://github.com/stac-utils/titiler-pgstac) and [pgstac](https://github.com/stac-utils/pgstac). It enables large-scale mosaic based on the results of STAC search queries.

The service includes all the default endpoints from **titiler-pgstac** application and:

- `/`: a custom landing page with links to the different endpoints
- `/mosaic/builder`: a virtual mosaic builder UI that helps create and register STAC Search queries
- `/collections`: a secret (not in OpenAPI documentation) endpoint used in the mosaic-builder page
- `/collections/{collection_id}/items/{item_id}/viewer`: a simple STAC Item viewer

#### eoapi.vector

OGC Features and Tiles API built on top of [tipg](https://github.com/developmentseed/tipg).

The API will look for tables in the database's `public` schema by default. We've also added three functions that connect to the pgSTAC schema:

- **pg_temp.pgstac_collections_view**: Simple function which returns PgSTAC Collections
- **pg_temp.pgstac_hash**: Return features for a specific `searchId` (hash)
- **pg_temp.pgstac_hash_count**: Return the number of items per geometry for a specific `searchId` (hash)

### Infrastructure

The CDK code is almost similar to the one found in [eoapi-template](https://github.com/developmentseed/eoapi-template). We just added some configurations for our custom runtimes.

### Local testing

Before deploying the application on the cloud, you can start by exploring it with a local *Docker* deployment

```
docker compose up
```

Once the applications are *up*, you'll need to add STAC **Collections** and **Items** to the PgSTAC database.

To load the Impact Observatory Landcover STAC collection and items to the local database:

```shell
# set env vars to point at the database in the docker network
export BUSINESS_API_ENDPOINT=http://localhost:8084
export PGUSER=username
export PGPASSWORD=password
export PGDATABASE=postgis
export PGHOST=localhost
export PGPORT=5439
```

After the STAC metadata are loaded up it is time to add some parcels to the database:

```shell
# bootstrap the database with some landcover class definitions
curl -X POST ${BUSINESS_API_ENDPOINT}/bootstrap-data | jq

# load a sample of parcel data from two counties in the US
scripts/load-parcel-data siskiyou 0-200
scripts/load-parcel-data st_louis 0-200
```

## Deployment

### Requirements

- python >=3.9
- docker
- node >=14
- AWS credentials environment variables configured to point to an account.
- **Optional** a `config.yaml` file to override the default deployment settings defined in `config.py`.

### Installation

Install python dependencies with

```
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

And node dependencies with

```
npm install
```

Verify that the `cdk` CLI is available. Since `aws-cdk` is installed as a local dependency, you can use the `npx` node package runner tool, that comes with `npm`.

```
npx cdk --version
```

First, synthesize the app

```
npx cdk synth --all
```

Then, deploy

```
npx cdk deploy --all --require-approval never
```

After deployment, follow the same types of steps to seed the database with STAC metadata and/or parcel records.

## Development

```shell
source .venv/bin/activate

python -m pip install -e \
  'runtimes/business/logic' \
  'runtimes/eoapi/raster' \
  'runtimes/eoapi/stac' \
  'runtimes/eoapi/vector'

```
