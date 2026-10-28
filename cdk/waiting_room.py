"""
YOUR WAITING ROOM  (this file is yours to change)
=================================================
The provided stack (cdk/stack.py) builds this construct when you deploy with `-c waiting_room=on`. It hands you the
EXISTING REST API and connects your admissions table to the provided gate. Everything that makes your option work
goes here, in lambdas/waiting_room/, and in the tests you write.

WHAT THE STACK REQUIRES FROM YOU   (tests/test_starter_wiring.py checks all of it: keep it passing)
  1. A constructor   WaitingRoom(scope, construct_id, *, api, admissions_per_second)
  2. self.admissions_table : a DynamoDB table keyed by `visitor_id` (string). The provided gate reads and consumes
     the admission records in it (see CONTRACT.md, section 4). This skeleton already creates one.
  3. These two routes on `api` (already added below):
         POST /waiting-room/join
         GET  /waiting-room/status/{visitor_id}

OPTIONAL
  - self.functions : a list of every Lambda function you create. The dashboard's "errors and throttles" panel sums
    over this list, so add each function you create to it.
  - dashboard_widgets() : return up to 3 CloudWatch widgets for the dashboard's "your widgets" row.

WHAT THIS SKELETON ALREADY DOES: the admissions table, two stub functions that answer 501 (not implemented), and
the two routes. WHAT IS LEFT TO YOU: everything that makes it a waiting room. Change anything in this file.
"""
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticache as elasticache
from constructs import Construct

HANDLERS = Path(__file__).resolve().parent.parent / "lambdas" / "waiting_room"

class WaitingRoom(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        api: apigw.RestApi,
        admissions_per_second: float = 10,
    ) -> None:
        super().__init__(scope, construct_id)
        self.admissions_per_second = admissions_per_second

        # -- 1. REQUIRED: the table the provided gate reads ------------------------------------------------------
        self.admissions_table = dynamodb.Table(
            self, "Admissions",
            table_name="week5-wr-admissions",
            partition_key=dynamodb.Attribute(name="visitor_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # -- 2. NETWORK INSTUCTURE FOR REDIS ---------------------------------------------------------------------
        # Create an isolated VPC to avoid expensive NAT Gateway costs (staying under the $2 budget)
        self.vpc = ec2.Vpc(
            self, "WrVpc",
            max_azs=2,
            nat_gateways=0, # No NAT gateway needed
            subnet_configuration=[
                ec2.SubnetConfiguration(name="Isolated", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED)
            ]
        )
        
        # Add a DynamoDB Gateway Endpoint so isolated Lambdas can write admission tokens
        self.vpc.add_gateway_endpoint(
            "DDBEndpoint", 
            service=ec2.GatewayVpcEndpointAwsService.DYNAMODB
        )

        # Security Groups to allow Lambdas to securely talk to Redis
        self.lambda_sg = ec2.SecurityGroup(self, "LambdaSG", vpc=self.vpc)
        self.redis_sg = ec2.SecurityGroup(self, "RedisSG", vpc=self.vpc)
        self.redis_sg.add_ingress_rule(self.lambda_sg, ec2.Port.tcp(6379), "Allow Lambda to access Redis")
        self.redis_sg.add_ingress_rule(self.lambda_sg, ec2.Port.tcp(6380), "Allow Lambda to access Redis TLS")

        # -- 3. REDIS SERVERLESS CACHE ---------------------------------------------------------------------------
        subnet_ids = [s.subnet_id for s in self.vpc.isolated_subnets]
        
        self.redis_subnet_group = elasticache.CfnSubnetGroup(
            self, "WrRedisSubnets",
            description="Subnets for Redis",
            subnet_ids=subnet_ids
        )

        self.redis = elasticache.CfnCacheCluster(
            self, "WrRedis",
            engine="redis",
            cache_node_type="cache.t3.micro",
            num_cache_nodes=1,
            vpc_security_group_ids=[self.redis_sg.security_group_id],
            cache_subnet_group_name=self.redis_subnet_group.ref,
        )

        # -- 4. LAMBDA FUNCTIONS ---------------------------------------------------------------------------------
        env = {
            "ADMISSIONS_TABLE": self.admissions_table.table_name,
            "ADMIT_PER_SECOND": str(int(admissions_per_second)),
            "POLL_INTERVAL_SECONDS": "5",
            "ADMISSION_VALID_SECONDS": "600",            
            "REDIS_HOST": self.redis.attr_redis_endpoint_address,
            "REDIS_PORT": self.redis.attr_redis_endpoint_port,
        }
        
        code = lambda_.Code.from_asset(str(HANDLERS))

        def make_fn(construct_id, function_name, handler):
            return lambda_.Function(
                self, construct_id,
                function_name=function_name,
                runtime=lambda_.Runtime.PYTHON_3_12,
                handler=handler,
                code=code,
                timeout=cdk.Duration.seconds(10),
                memory_size=128,
                environment=env,
                log_retention=logs.RetentionDays.ONE_WEEK,
                # Bind Lambdas to the isolated VPC so they can hit Redis
                vpc=self.vpc,
                vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
                security_groups=[self.lambda_sg],
            )

        self.join_fn = make_fn("Join", "week5-wr-join", "join_handler.lambda_handler")
        self.status_fn = make_fn("Status", "week5-wr-status", "status_handler.lambda_handler")
        
        self.admissions_table.grant_read_write_data(self.status_fn)

        self.functions = [self.join_fn, self.status_fn]

        # -- 5. REQUIRED: the two routes ---------------------------------------------------------------------
        waiting_room = api.root.add_resource("waiting-room")
        waiting_room.add_resource("join").add_method("POST", apigw.LambdaIntegration(self.join_fn))
        waiting_room.add_resource("status").add_resource("{visitor_id}").add_method(
            "GET", apigw.LambdaIntegration(self.status_fn))

    def dashboard_widgets(self) -> list:
            # Custom Widget to track Redis Micro Node Health
            return [cw.GraphWidget(
                title="Redis Connections & CPU",
                left=[cw.Metric(
                    namespace="AWS/ElastiCache",
                    metric_name="CurrConnections",
                    dimensions_map={"CacheClusterId": self.redis.ref},
                    statistic="Maximum"
                )],
                right=[cw.Metric(
                    namespace="AWS/ElastiCache",
                    metric_name="CPUUtilization",
                    dimensions_map={"CacheClusterId": self.redis.ref},
                    statistic="Average"
                )],
                width=12,
            )]
        
        
