import json
import os
import uuid

import boto3
import yaml
from aws_cdk import (
    App,
    CfnOutput,
    CustomResource,
    Duration,
    RemovalPolicy,
    Stack,
    aws_certificatemanager,
    aws_ec2,
    aws_iam,
    aws_lambda,
    aws_logs,
    aws_rds,
    aws_s3,
    aws_secretsmanager,
)
from aws_cdk.aws_apigateway import DomainNameOptions
from aws_cdk.aws_apigatewayv2_alpha import DomainName, DomainMappingOptions, HttpApi
from aws_cdk.aws_apigatewayv2_integrations_alpha import HttpLambdaIntegration
from config import AppConfig
from constructs import Construct
from eoapi_cdk import (
    BastionHost,
    PgStacApiLambda,
    PgStacDatabase,
    StacBrowser,
    StacIngestor,
    TiPgApiLambda,
    TitilerPgstacApiLambda,
)

POSTGRES_PARAMETER_GROUP_SETTINGS = {
    "t3.micro": {
        "max_connections": str(200),
        "shared_buffers": str(int(256 * 1024 / 8)),  # 8 kB
        "effective_cache_size": str(int(768 * 1024 / 8)),  # 8 kB
        "maintenance_work_mem": str(64 * 1024),  # kB
        "checkpoint_completion_target": str(0.9),
        "wal_buffers": str(int(7864 / 8)),  # 8 kB
        "default_statistics_target": str(100),
        "random_page_cost": str(1.1),
        "effective_io_concurrency": str(200),  # number
        "work_mem": str(655),  # kB
        "huge_pages": "off",
        "min_wal_size": str(1 * 1024),  # MB
        "max_wal_size": str(4 * 1024),  # MB
    },
    "t3.small": {
        "max_connections": str(200),
        "shared_buffers": str(int(512 * 1024 / 8)),  # 8 kB
        "effective_cache_size": str(int(1536 * 1024 / 8)),  # 8 kB
        "maintenance_work_mem": str(128 * 1024),  # kB
        "checkpoint_completion_target": str(0.9),
        "wal_buffers": str(int(16 * 1024 / 8)),  # 8 kB
        "default_statistics_target": str(100),
        "random_page_cost": str(1.1),
        "effective_io_concurrency": str(200),  # number
        "work_mem": str(1310),  # kB
        "huge_pages": "off",
        "min_wal_size": str(1 * 1024),  # MB
        "max_wal_size": str(4 * 1024),  # MB
    },
}


class VpcStack(Stack):
    def __init__(
        self, scope: Construct, app_config: AppConfig, id: str, **kwargs
    ) -> None:
        super().__init__(scope, id=id, tags=app_config.tags, **kwargs)

        self.vpc = aws_ec2.Vpc(
            self,
            "vpc",
            subnet_configuration=[
                aws_ec2.SubnetConfiguration(
                    name="ingress", subnet_type=aws_ec2.SubnetType.PUBLIC, cidr_mask=24
                ),
                aws_ec2.SubnetConfiguration(
                    name="application",
                    subnet_type=aws_ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
                aws_ec2.SubnetConfiguration(
                    name="rds",
                    subnet_type=aws_ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24,
                ),
            ],
            nat_gateways=app_config.nat_gateway_count,
        )

        self.vpc.add_interface_endpoint(
            "SecretsManagerEndpoint",
            service=aws_ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
        )

        self.vpc.add_interface_endpoint(
            "CloudWatchEndpoint",
            service=aws_ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
        )

        self.vpc.add_gateway_endpoint(
            "S3", service=aws_ec2.GatewayVpcEndpointAwsService.S3
        )

        self.export_value(
            self.vpc.select_subnets(subnet_type=aws_ec2.SubnetType.PUBLIC)
            .subnets[0]
            .subnet_id
        )
        self.export_value(
            self.vpc.select_subnets(subnet_type=aws_ec2.SubnetType.PUBLIC)
            .subnets[1]
            .subnet_id
        )


class BootstrappedDb(Construct):
    """
    Given an RDS database, connect to DB and create a database, user, and
    password
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        db: aws_rds.DatabaseInstance,
        new_dbname: str,
        new_username: str,
        secrets_prefix: str,
        context_dir: str = "../../",
    ) -> None:
        """Update RDS database."""
        super().__init__(scope, id)

        deployment_version = str(uuid.uuid4())
        # TODO: Utilize a singleton function.
        handler = aws_lambda.Function(
            self,
            "database-bootstrapper",
            handler="handler.handler",
            runtime=aws_lambda.Runtime.PYTHON_3_11,
            code=aws_lambda.Code.from_docker_build(
                path=os.path.abspath(context_dir),
                file="infrastructure/dockerfiles/Dockerfile.bootstrap",
                build_args={"PYTHON_VERSION": "3.11"},
                platform="linux/amd64",
            ),
            timeout=Duration.minutes(2),
            vpc=db.vpc,
            allow_public_subnet=True,
            log_retention=aws_logs.RetentionDays.ONE_WEEK,
        )

        self.secret = aws_secretsmanager.Secret(
            self,
            id,
            secret_name=os.path.join(
                secrets_prefix, id.replace(" ", "_"), self.node.addr
            ),
            generate_secret_string=aws_secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps(
                    {
                        "dbname": new_dbname,
                        "engine": "postgres",
                        "port": 5432,
                        "host": db.instance_endpoint.hostname,
                        "username": new_username,
                    },
                ),
                generate_string_key="password",
                exclude_punctuation=True,
            ),
            description=f"Deployed by {Stack.of(self).stack_name}",
        )

        self.resource = CustomResource(
            scope=scope,
            id="bootstrapped-db-resource",
            service_token=handler.function_arn,
            properties={
                # By setting pgstac_version in the properties assures
                # that Create/Update events will be passed to the service token
                "conn_secret_arn": db.secret.secret_arn,
                "new_user_secret_arn": self.secret.secret_arn,
                "deployment_version": deployment_version,  # Ensures unique deployments
            },
            # We do not need to run the custom resource on STAC Delete
            # Custom Resource are not physical resources so it's OK to `Retain` it
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Allow lambda to...
        # read new user secret
        self.secret.grant_read(handler)
        # read database secret
        db.secret.grant_read(handler)
        # connect to database
        db.connections.allow_from(handler, port_range=aws_ec2.Port.tcp(5432))

    def is_required_by(self, construct: Construct):
        """Register required services."""
        return construct.node.add_dependency(self.resource)


class eoAPIStack(Stack):
    def __init__(
        self,
        scope: Construct,
        vpc: aws_ec2.Vpc,
        id: str,
        app_config: AppConfig,
        context_dir: str = "./",
        **kwargs,
    ) -> None:
        super().__init__(
            scope,
            id=id,
            tags=app_config.tags,
            **kwargs,
        )

        #######################################################################
        # PG database
        postgres_engine = aws_rds.DatabaseInstanceEngine.postgres(
            version=aws_rds.PostgresEngineVersion.VER_14
        )

        if parameter_group_values := POSTGRES_PARAMETER_GROUP_SETTINGS.get(
            app_config.db_instance_type
        ):
            parameter_group = aws_rds.ParameterGroup(
                self,
                "db-parameter-group",
                engine=postgres_engine,
                parameters=parameter_group_values,
            )
        else:
            parameter_group = None

        pgstac_db = PgStacDatabase(
            self,
            "pgstac-db",
            vpc=vpc,
            engine=postgres_engine,
            vpc_subnets=aws_ec2.SubnetSelection(
                subnet_type=(
                    aws_ec2.SubnetType.PUBLIC
                    if app_config.public_db_subnet
                    else aws_ec2.SubnetType.PRIVATE_ISOLATED
                )
            ),
            parameter_group=parameter_group,
            allocated_storage=app_config.db_allocated_storage,
            instance_type=aws_ec2.InstanceType(app_config.db_instance_type),
            removal_policy=RemovalPolicy.DESTROY,
            custom_resource_properties={
                "context": True,
                "mosaic_index": True,
            },
        )
        pgstac_db.db.connections.allow_default_port_from_any_ipv4()

        #######################################################################
        # Raster service
        raster = TitilerPgstacApiLambda(
            self,
            "raster-api",
            api_env={
                "EOAPI_RASTER_NAME": app_config.build_service_name("raster"),
                "description": f"{app_config.stage} Raster API",
                "POSTGRES_HOST": pgstac_db.pgstac_secret.secret_value_from_json(
                    "host"
                ).to_string(),
                "POSTGRES_DBNAME": pgstac_db.pgstac_secret.secret_value_from_json(
                    "dbname"
                ).to_string(),
                "POSTGRES_USER": pgstac_db.pgstac_secret.secret_value_from_json(
                    "username"
                ).to_string(),
                "POSTGRES_PASS": pgstac_db.pgstac_secret.secret_value_from_json(
                    "password"
                ).to_string(),
                "POSTGRES_PORT": pgstac_db.pgstac_secret.secret_value_from_json(
                    "port"
                ).to_string(),
            },
            db=pgstac_db.db,
            db_secret=pgstac_db.pgstac_secret,
            # If the db is not in the public subnet then we need to put
            # the lambda within the VPC
            vpc=vpc if not app_config.public_db_subnet else None,
            subnet_selection=aws_ec2.SubnetSelection(
                subnet_type=aws_ec2.SubnetType.PRIVATE_WITH_EGRESS
            )
            if not app_config.public_db_subnet
            else None,
            buckets=app_config.raster_buckets,
            titiler_pgstac_api_domain_name=(
                DomainName(
                    self,
                    "raster-api-domain-name",
                    domain_name=app_config.raster_api_custom_domain,
                    certificate=aws_certificatemanager.Certificate.from_certificate_arn(
                        self,
                        "raster-api-cdn-certificate",
                        certificate_arn=app_config.acm_certificate_arn,
                    ),
                )
                if app_config.raster_api_custom_domain
                else None
            ),
            lambda_function_options={
                "code": aws_lambda.Code.from_docker_build(
                    path=os.path.abspath(context_dir),
                    file="infrastructure/dockerfiles/Dockerfile.raster",
                    build_args={
                        "PYTHON_VERSION": "3.11",
                    },
                    platform="linux/amd64",
                ),
                "handler": "handler.handler",
                "runtime": aws_lambda.Runtime.PYTHON_3_11,
            },
        )

        #######################################################################
        # STAC API service
        stac = PgStacApiLambda(
            self,
            "stac-api",
            api_env={
                "EOAPI_STAC_NAME": app_config.build_service_name("stac"),
                "description": f"{app_config.stage} STAC API",
                "POSTGRES_HOST_READER": pgstac_db.pgstac_secret.secret_value_from_json(
                    "host"
                ).to_string(),
                "POSTGRES_HOST_WRITER": pgstac_db.pgstac_secret.secret_value_from_json(
                    "host"
                ).to_string(),
                "POSTGRES_DBNAME": pgstac_db.pgstac_secret.secret_value_from_json(
                    "dbname"
                ).to_string(),
                "POSTGRES_USER": pgstac_db.pgstac_secret.secret_value_from_json(
                    "username"
                ).to_string(),
                "POSTGRES_PASS": pgstac_db.pgstac_secret.secret_value_from_json(
                    "password"
                ).to_string(),
                "POSTGRES_PORT": pgstac_db.pgstac_secret.secret_value_from_json(
                    "port"
                ).to_string(),
                "EOAPI_STAC_TITILER_ENDPOINT": raster.url.strip("/"),
                "EOAPI_STAC_EXTENSIONS": '["filter", "query", "sort", "fields", "pagination", "titiler"]',
            },
            db=pgstac_db.db,
            db_secret=pgstac_db.pgstac_secret,
            # If the db is not in the public subnet then we need to put
            # the lambda within the VPC
            vpc=vpc if not app_config.public_db_subnet else None,
            subnet_selection=aws_ec2.SubnetSelection(
                subnet_type=aws_ec2.SubnetType.PRIVATE_WITH_EGRESS
            )
            if not app_config.public_db_subnet
            else None,
            stac_api_domain_name=(
                DomainName(
                    self,
                    "stac-api-domain-name",
                    domain_name=app_config.stac_api_custom_domain,
                    certificate=aws_certificatemanager.Certificate.from_certificate_arn(
                        self,
                        "stac-api-cdn-certificate",
                        certificate_arn=app_config.acm_certificate_arn,
                    ),
                )
                if app_config.stac_api_custom_domain
                else None
            ),
            lambda_function_options={
                "code": aws_lambda.Code.from_docker_build(
                    path=os.path.abspath(context_dir),
                    file="infrastructure/dockerfiles/Dockerfile.stac",
                    build_args={
                        "PYTHON_VERSION": "3.11",
                    },
                    platform="linux/amd64",
                ),
                "handler": "handler.handler",
                "runtime": aws_lambda.Runtime.PYTHON_3_11,
            },
        )

        if app_config.stac_ingestor:
            #######################################################################
            # STAC Ingestor Service
            if app_config.data_access_role_arn:
                # importing provided role from arn.
                # the stac ingestor will try to assume it when called,
                # so it must be listed in the data access role trust policy.
                data_access_role = aws_iam.Role.from_role_arn(
                    self,
                    "data-access-role",
                    role_arn=app_config.data_access_role_arn,
                )
            else:
                data_access_role = self._create_data_access_role()

            stac_ingestor_env = {"REQUESTER_PAYS": "True"}
            if app_config.auth_provider_jwks_url:
                stac_ingestor_env["JWKS_URL"] = app_config.auth_provider_jwks_url

            stac_ingestor = StacIngestor(
                self,
                "stac-ingestor",
                stac_url=stac.url,
                stage=app_config.stage,
                data_access_role=data_access_role,
                stac_db_secret=pgstac_db.pgstac_secret,
                stac_db_security_group=pgstac_db.db.connections.security_groups[0],
                # If the db is not in the public subnet then we need to put
                # the lambda within the VPC
                vpc=vpc if not app_config.public_db_subnet else None,
                subnet_selection=aws_ec2.SubnetSelection(
                    subnet_type=aws_ec2.SubnetType.PRIVATE_WITH_EGRESS
                )
                if not app_config.public_db_subnet
                else None,
                api_env=stac_ingestor_env,
                ingestor_domain_name_options=(
                    DomainNameOptions(
                        domain_name=app_config.stac_ingestor_api_custom_domain,
                        certificate=aws_certificatemanager.Certificate.from_certificate_arn(
                            self,
                            "stac-ingestor-api-cdn-certificate",
                            certificate_arn=app_config.acm_certificate_arn,
                        ),
                    )
                    if app_config.stac_ingestor_api_custom_domain
                    else None
                ),
            )
            # we can only do that if the role is created here.
            # If injecting a role, that role's trust relationship
            # must be already set up, or set up after this deployment.
            if not app_config.data_access_role_arn:
                data_access_role = self._grant_assume_role_with_principal_pattern(
                    data_access_role, stac_ingestor.handler_role.role_name
                )

        #######################################################################
        # Bastion Host
        if app_config.bastion_host:
            BastionHost(
                self,
                "bastion-host",
                vpc=vpc,
                db=pgstac_db.db,
                ipv4_allowlist=app_config.bastion_host_allow_ip_list,
                user_data=(
                    aws_ec2.UserData.custom(
                        yaml.dump(app_config.bastion_host_user_data)
                    )
                    if app_config.bastion_host_user_data is not None
                    else aws_ec2.UserData.for_linux()
                ),
                create_elastic_ip=app_config.bastion_host_create_elastic_ip,
            )

        if app_config.stac_browser_version:
            stac_browser_bucket = aws_s3.Bucket(
                self,
                "stac-browser-bucket",
                bucket_name=app_config.build_service_name("stac-browser"),
                removal_policy=RemovalPolicy.DESTROY,
                auto_delete_objects=True,
                website_index_document="index.html",
                public_read_access=True,
                block_public_access=aws_s3.BlockPublicAccess(
                    block_public_acls=False,
                    block_public_policy=False,
                    ignore_public_acls=False,
                    restrict_public_buckets=False,
                ),
                object_ownership=aws_s3.ObjectOwnership.OBJECT_WRITER,
            )
            StacBrowser(
                self,
                "stac-browser",
                github_repo_tag=app_config.stac_browser_version,
                stac_catalog_url=f"https://{app_config.stac_api_custom_domain}",
                website_index_document="index.html",
                bucket_arn=stac_browser_bucket.bucket_arn,
            )

        #######################################################################
        # Business API

        setup_db = BootstrappedDb(
            self,
            "bootstrappedbusinessdb",
            db=pgstac_db.db,
            new_dbname=app_config.business_dbname,
            new_username=app_config.business_dbuser,
            secrets_prefix=os.path.join(app_config.stage, app_config.project_id),
            context_dir=context_dir,
        )

        #######################################################################
        # Vector Service
        vector = TiPgApiLambda(
            self,
            "vector-api",
            db=pgstac_db.db,
            db_secret=pgstac_db.pgstac_secret,
            api_env={
                "EOAPI_VECTOR_NAME": app_config.build_service_name("vector"),
                "description": f"{app_config.stage} tipg API",
                "POSTGRES_USER": setup_db.secret.secret_value_from_json(
                    "username"
                ).to_string(),
                "POSTGRES_PASS": setup_db.secret.secret_value_from_json(
                    "password"
                ).to_string(),
                "POSTGRES_DBNAME": setup_db.secret.secret_value_from_json(
                    "dbname"
                ).to_string(),
                "POSTGRES_HOST": setup_db.secret.secret_value_from_json(
                    "host"
                ).to_string(),
                "POSTGRES_PORT": setup_db.secret.secret_value_from_json(
                    "port"
                ).to_string(),
                "EOAPI_VECTOR_SCHEMAS": '["business"]',
            },
            # If the db is not in the public subnet then we need to put
            # the lambda within the VPC
            vpc=vpc if not app_config.public_db_subnet else None,
            subnet_selection=aws_ec2.SubnetSelection(
                subnet_type=aws_ec2.SubnetType.PRIVATE_WITH_EGRESS
            )
            if not app_config.public_db_subnet
            else None,
            tipg_api_domain_name=(
                DomainName(
                    self,
                    "vector-api-domain-name",
                    domain_name=app_config.vector_api_custom_domain,
                    certificate=aws_certificatemanager.Certificate.from_certificate_arn(
                        self,
                        "vector-api-cdn-certificate",
                        certificate_arn=app_config.acm_certificate_arn,
                    ),
                )
                if app_config.vector_api_custom_domain
                else None
            ),
            lambda_function_options={
                "code": aws_lambda.Code.from_docker_build(
                    path=os.path.abspath(context_dir),
                    file="infrastructure/dockerfiles/Dockerfile.vector",
                    build_args={
                        "PYTHON_VERSION": "3.11",
                    },
                    platform="linux/amd64",
                ),
                "handler": "handler.handler",
                "runtime": aws_lambda.Runtime.PYTHON_3_11,
            },
        )
        setup_db.is_required_by(vector)

        #######################################################################
        # Business API
        business_lambda = aws_lambda.Function(
            scope=self,
            id="business-lambda",
            runtime=aws_lambda.Runtime.PYTHON_3_11,
            handler="handler.handler",
            memory_size=3000,
            log_retention=aws_logs.RetentionDays.ONE_WEEK,
            timeout=Duration.seconds(30),
            code=aws_lambda.Code.from_docker_build(
                path=os.path.abspath(context_dir),
                file="infrastructure/dockerfiles/Dockerfile.business",
                build_args={"PYTHON_VERSION": "3.11"},
            ),
            vpc=vpc,
            vpc_subnets=aws_ec2.SubnetSelection(
                subnet_type=aws_ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            environment={
                "MODE": "production",
                "POSTGRES_USER": setup_db.secret.secret_value_from_json(
                    "username"
                ).to_string(),
                "POSTGRES_PASS": setup_db.secret.secret_value_from_json(
                    "password"
                ).to_string(),
                "POSTGRES_DBNAME": setup_db.secret.secret_value_from_json(
                    "dbname"
                ).to_string(),
                "POSTGRES_HOST": setup_db.secret.secret_value_from_json(
                    "host"
                ).to_string(),
                "POSTGRES_PORT": setup_db.secret.secret_value_from_json(
                    "port"
                ).to_string(),
                "RASTER_ENDPOINT": raster.url,
                "VECTOR_ENDPOINT": vector.url,
                "STAC_ENDPOINT": stac.url,
            },
        )
        setup_db.is_required_by(business_lambda)

        business_lambda.connections.allow_to(
            pgstac_db.db,
            aws_ec2.Port.tcp(5432),
            "allow connections from business application",
        )

        if app_config.business_api_custom_domain and app_config.acm_certificate_arn:
            api_certificate = aws_certificatemanager.Certificate.from_certificate_arn(
                self, "APICertificate", app_config.acm_certificate_arn
            )
            default_domain_mapping = DomainMappingOptions(
                domain_name=DomainName(
                    self,
                    "BusinessApiDomainName",
                    domain_name=app_config.business_api_custom_domain,
                    certificate=api_certificate,
                )
            )
        else:
            default_domain_mapping = None

        business_api = HttpApi(
            scope=self,
            id="business-api",
            default_integration=HttpLambdaIntegration(
                "business-api-integration",
                handler=business_lambda,  # type: ignore
            ),
            default_domain_mapping=default_domain_mapping,
        )

        CfnOutput(self, "business-api-url", value=business_api.url)

    def _create_data_access_role(self) -> aws_iam.Role:
        """
        Creates an IAM role with full S3 read access.
        """

        data_access_role = aws_iam.Role(
            self,
            "data-access-role",
            assumed_by=aws_iam.ServicePrincipal("lambda.amazonaws.com"),
        )

        data_access_role.add_to_policy(
            aws_iam.PolicyStatement(
                actions=[
                    "s3:Get*",
                ],
                resources=["*"],
                effect=aws_iam.Effect.ALLOW,
            )
        )
        return data_access_role

    def _grant_assume_role_with_principal_pattern(
        self,
        role_to_assume: aws_iam.Role,
        principal_pattern: str,
        account_id: str = boto3.client("sts").get_caller_identity().get("Account"),
    ) -> aws_iam.Role:
        """
        Grants assume role permissions to the role of the given
        account with the given name pattern. Default account
        is the current account.
        """

        role_to_assume.assume_role_policy.add_statements(
            aws_iam.PolicyStatement(
                effect=aws_iam.Effect.ALLOW,
                principals=[aws_iam.AnyPrincipal()],
                actions=["sts:AssumeRole"],
                conditions={
                    "StringLike": {
                        "aws:PrincipalArn": [
                            f"arn:aws:iam::{account_id}:role/{principal_pattern}"
                        ]
                    }
                },
            )
        )

        return role_to_assume


app = App()

app_config = AppConfig()

vpc_stack = VpcStack(
    scope=app,
    app_config=app_config,
    id=f"vpc{app_config.project_id}",
)

pgstac_infra_stack = eoAPIStack(
    scope=app, vpc=vpc_stack.vpc, app_config=app_config, id=app_config.project_id
)

app.synth()
