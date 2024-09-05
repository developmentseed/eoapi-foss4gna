"""
Custom resource lambda handler to bootstrap Postgres db.
Source: https://github.com/developmentseed/eoAPI/blob/master/deployment/handlers/db_handler.py
"""

import json
import logging

import boto3
import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

logger = logging.getLogger("eoapi-bootstrap")


def send(
    event,
    context,
    responseStatus,
    responseData,
    physicalResourceId=None,
    noEcho=False,
):
    """
    Copyright 2016 Amazon Web Services, Inc. or its affiliates. All Rights Reserved.
    This file is licensed to you under the AWS Customer Agreement (the "License").
    You may not use this file except in compliance with the License.
    A copy of the License is located at http://aws.amazon.com/agreement/ .
    This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, express or implied.
    See the License for the specific language governing permissions and limitations under the License.

    Send response from AWS Lambda.

    Note: The cfnresponse module is available only when you use the ZipFile property to write your source code.
    It isn't available for source code that's stored in Amazon S3 buckets.
    For code in buckets, you must write your own functions to send responses.
    """
    responseBody = {}
    responseBody["Status"] = responseStatus
    responseBody["Reason"] = (
        "See the details in CloudWatch Log Stream: " + context.log_stream_name
    )
    responseBody["PhysicalResourceId"] = physicalResourceId or context.log_stream_name
    responseBody["StackId"] = event["StackId"]
    responseBody["RequestId"] = event["RequestId"]
    responseBody["LogicalResourceId"] = event["LogicalResourceId"]
    responseBody["NoEcho"] = noEcho
    responseBody["Data"] = responseData

    json_responseBody = json.dumps(responseBody)
    print("Response body:\n     " + json_responseBody)

    try:
        response = httpx.put(
            event["ResponseURL"],
            data=json_responseBody,
            headers={"content-type": "", "content-length": str(len(json_responseBody))},
            timeout=30,
        )
        print("Status code: ", response.status_code)
        logger.debug(f"OK - Status code: {response.status_code}")

    except Exception as e:
        print("send(..) failed executing httpx.put(..): " + str(e))
        logger.debug(f"NOK - failed executing PUT requests:  {e}")


def get_secret(secret_name):
    """Get Secrets from secret manager."""
    print(f"Fetching {secret_name}")
    client = boto3.client(service_name="secretsmanager")
    response = client.get_secret_value(SecretId=secret_name)
    return json.loads(response["SecretString"])


def create_db(cursor, db_name: str) -> None:
    """Create DB."""
    cursor.execute(
        sql.SQL("SELECT 1 FROM pg_catalog.pg_database " "WHERE datname = %s"), [db_name]
    )
    if cursor.fetchone():
        print(f"    database {db_name} exists, not creating DB")
    else:
        print(f"    database {db_name} not found, creating...")
        cursor.execute(
            sql.SQL("CREATE DATABASE {db_name}").format(db_name=sql.Identifier(db_name))
        )


def create_user(cursor, username: str, password: str) -> None:
    """Create User."""
    cursor.execute(
        sql.SQL(
            "DO $$ "
            "BEGIN "
            "  IF NOT EXISTS ( "
            "       SELECT 1 FROM pg_roles "
            "       WHERE rolname = {user}) "
            "  THEN "
            "    CREATE USER {username} "
            "    WITH PASSWORD {password}; "
            "  ELSE "
            "    ALTER USER {username} "
            "    WITH PASSWORD {password}; "
            "  END IF; "
            "END "
            "$$; "
        ).format(username=sql.Identifier(username), password=password, user=username)
    )


def update_user_permissions(cursor, db_name: str, username: str) -> None:
    """Update user permissions."""
    command = sql.SQL(
        """
        DO $$
        BEGIN
            -- Check if the 'business' role exists, if not, create it.
            IF NOT EXISTS (
                SELECT 1 FROM pg_roles 
                WHERE rolname = 'business') 
            THEN
                CREATE ROLE business;
                ALTER ROLE business SET search_path TO business, public;
            END IF;
        END
        $$;
        
        -- Grant the required permissions.
        GRANT business TO CURRENT_USER;
        GRANT CONNECT ON DATABASE {db_name} TO {username};
        GRANT CREATE ON DATABASE {db_name} TO {username};
        
        -- Create the 'business' schema if it does not exist.
        CREATE SCHEMA IF NOT EXISTS business AUTHORIZATION business;
        GRANT ALL PRIVILEGES ON SCHEMA business TO business;
        
        -- Assign the 'business' role to the user and set search_path directly for the user.
        GRANT business TO {username};
        ALTER ROLE {username} SET search_path TO business, public;

        REVOKE business FROM CURRENT_USER;
        """
    ).format(db_name=sql.Identifier(db_name), username=sql.Identifier(username))

    cursor.execute(command)


def register_extensions(cursor) -> None:
    """Add PostGIS extension."""
    cursor.execute(sql.SQL("CREATE EXTENSION IF NOT EXISTS postgis;"))


def handler(event, context):
    """Lambda Handler."""
    print(f"Handling {event}")

    if event["RequestType"] not in ["Create", "Update"]:
        return send(event, context, "SUCCESS", {"msg": "No action to be taken"})

    try:
        params = event["ResourceProperties"]

        # Admin (AWS RDS) user/password/dbname parameters
        admin_params = get_secret(params["conn_secret_arn"])

        # Custom business user/password/dbname parameters
        business_params = get_secret(params["new_user_secret_arn"])

        print("Connecting to RDS...")
        rds_conninfo = make_conninfo(
            dbname=admin_params.get("dbname", "postgres"),
            user=admin_params["username"],
            password=admin_params["password"],
            host=admin_params["host"],
            port=admin_params["port"],
        )
        with psycopg.connect(rds_conninfo, autocommit=True) as conn:
            with conn.cursor() as cur:
                print(f"Creating business '{business_params['dbname']}' database...")
                create_db(
                    cursor=cur,
                    db_name=business_params["dbname"],
                )

                print(f"Creating business '{business_params['username']}' db user...")
                create_user(
                    cursor=cur,
                    username=business_params["username"],
                    password=business_params["password"],
                )

        # Install postgis on the eoapi database
        print(f"Connecting to business '{business_params['dbname']}' database...")
        business_db_admin_conninfo = make_conninfo(
            dbname=business_params["dbname"],
            user=admin_params["username"],
            password=admin_params["password"],
            host=admin_params["host"],
            port=admin_params["port"],
        )
        with psycopg.connect(business_db_admin_conninfo, autocommit=True) as conn:
            with conn.cursor() as cur:
                print(
                    f"Registering Extension in '{business_params['dbname']}' database..."
                )
                register_extensions(cursor=cur)

        with psycopg.connect(
            business_db_admin_conninfo,
            autocommit=True,
            options="-c search_path=business,public",
        ) as conn:
            print("Customize database...")
            # Update permissions to business user to assume business roles
            with conn.cursor() as cur:
                print(f"Update '{business_params['username']}' permissions...")
                update_user_permissions(
                    cursor=cur,
                    db_name=business_params["dbname"],
                    username=business_params["username"],
                )

        # Make sure the user can access the database
        print("Checking business user access to the database...")
        business_db_user_conninfo = make_conninfo(
            dbname=business_params["dbname"],
            user=business_params["username"],
            password=business_params["password"],
            host=business_params["host"],
            port=business_params["port"],
        )
        with psycopg.connect(business_db_user_conninfo) as conn:
            with conn.cursor() as cur:
                pass

    except Exception as e:
        print(f"Unable to bootstrap database with exception={e}")
        send(event, context, "FAILED", {"message": str(e)})
        raise e

    print("Complete.")
    return send(event, context, "SUCCESS", {})
