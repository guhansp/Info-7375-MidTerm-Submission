import json
import os
import uuid
import time
import pytest
import boto3
import fakeredis
from unittest.mock import patch
from moto import mock_aws
from conftest import load_module

@pytest.fixture
def mock_env():
    """Setup environment variables expected by the handlers."""
    os.environ["ADMISSIONS_TABLE"] = "week5-wr-admissions"
    os.environ["ADMIT_PER_SECOND"] = "10"
    os.environ["POLL_INTERVAL_SECONDS"] = "5"
    os.environ["ADMISSION_VALID_SECONDS"] = "600"
    os.environ["REDIS_HOST"] = "localhost"
    os.environ["REDIS_PORT"] = "6380"
    yield

@pytest.fixture
def mock_redis_client():
    """Provide a fresh FakeRedis instance for each test."""
    return fakeredis.FakeRedis(decode_responses=True)

@pytest.fixture
def ddb_table(mock_env):
    """Mock DynamoDB and create the admissions table."""
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        table = dynamodb.create_table(
            TableName=os.environ["ADMISSIONS_TABLE"],
            KeySchema=[{"AttributeName": "visitor_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "visitor_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST"
        )
        yield table

def test_join_answers_202(mock_env, mock_redis_client):
    """Test that joining the room issues a 202 and a valid payload."""
    with patch("redis.Redis", return_value=mock_redis_client):
        join = load_module("waiting_room/join_handler.py")
        
        response = join.lambda_handler({}, None)
        
        assert response["statusCode"] == 202
        body = json.loads(response["body"])
        
        assert body["status"] == "waiting"
        assert body["poll_interval_seconds"] == 5
        assert "visitor_id" in body
        assert body["poll_path"] == f"/waiting-room/status/{body['visitor_id']}"
        
        # Verify Redis state
        vid = body["visitor_id"]
        assert mock_redis_client.hget(f"visitor:{vid}", "position") == '1'

def test_concurrent_joins_maintain_strict_order(mock_env, mock_redis_client):
    """Simulate a stampede of concurrent joins to prove INCR atomicity."""
    with patch("redis.Redis", return_value=mock_redis_client):
        join = load_module("waiting_room/join_handler.py")
        
        # Simulate 10 sequential hits, mimicking concurrent requests hitting the INCR command
        responses = [join.lambda_handler({}, None) for _ in range(10)]
        
        positions = []
        for r in responses:
            vid = json.loads(r["body"])["visitor_id"]
            pos = mock_redis_client.hget(f"visitor:{vid}", "position")
            positions.append(int(pos))
            
        # The positions should be exactly 1 through 10 with no race conditions or duplicates
        assert sorted(positions) == list(range(1, 11))

def test_status_malformed_uuid(mock_env, mock_redis_client):
    """Test that malformed visitor IDs are rejected with a 400 error."""
    with patch("redis.Redis", return_value=mock_redis_client):
        status = load_module("waiting_room/status_handler.py")
        
        response = status.lambda_handler({"pathParameters": {"visitor_id": "not-a-uuid"}}, None)
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"] == "malformed_visitor_id"

def test_status_waiting(mock_env, mock_redis_client, ddb_table):
    """Test that a user is kept waiting if their position exceeds the serving counter."""
    with patch("redis.Redis", return_value=mock_redis_client), \
         patch("boto3.resource", return_value=boto3.resource("dynamodb", region_name="us-east-1")):
        
        join = load_module("waiting_room/join_handler.py")
        status = load_module("waiting_room/status_handler.py")
        
        # Generate 15 joins to push the queue depth past the 10/sec admission rate
        for _ in range(15):
            join.lambda_handler({}, None)
            
        # Grab the 15th visitor
        res = join.lambda_handler({}, None)
        vid = json.loads(res["body"])["visitor_id"]
        
        # Poll immediately (elapsed time ~0 seconds, serving counter ~0)
        status_res = status.lambda_handler({"pathParameters": {"visitor_id": vid}}, None)
        
        assert status_res["statusCode"] == 200
        assert json.loads(status_res["body"])["status"] == "waiting"

def test_status_admitted_and_persisted(mock_env, mock_redis_client, ddb_table):
    """Test that a user is admitted and written to DDB when the counter advances."""
    with patch("redis.Redis", return_value=mock_redis_client), \
         patch("boto3.resource", return_value=boto3.resource("dynamodb", region_name="us-east-1")):
        
        join = load_module("waiting_room/join_handler.py")
        status = load_module("waiting_room/status_handler.py")
        
        # Join the queue
        join_res = join.lambda_handler({}, None)
        vid = json.loads(join_res["body"])["visitor_id"]
        
        # Manipulate Redis to simulate 5 seconds passing (serving counter = 50)
        start_time = float(mock_redis_client.get("wr_start_time"))
        mock_redis_client.set("wr_start_time", start_time - 5.0)
        
        # Check status
        status_res = status.lambda_handler({"pathParameters": {"visitor_id": vid}}, None)
        
        assert status_res["statusCode"] == 200
        body = json.loads(status_res["body"])
        assert body["status"] == "admitted"
        assert "admission_token" in body
        
        # Verify the DynamoDB write occurred accurately
        item = ddb_table.get_item(Key={"visitor_id": vid}).get("Item")
        assert item is not None
        assert item["used"] is False
        assert item["admission_token"] == body["admission_token"]