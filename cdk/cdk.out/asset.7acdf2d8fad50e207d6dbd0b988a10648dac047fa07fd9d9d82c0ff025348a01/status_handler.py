"""
STATUS  (GET /waiting-room/status/{visitor_id})  -  YOURS TO WRITE
===================================================================
Answer exactly as CONTRACT.md section 4 says: waiting, admitted (with the token), admitted after the order was
placed (no token), or 400 for a malformed visitor_id.

When you admit a visitor you must write their admission record (CONTRACT.md section 4). Count it once, at that
moment (not on every poll), with the provided helper:

    import telemetry
    telemetry.admitted()
    telemetry.time_in_line(seconds_since_they_joined)
"""

import json
import os
import time
import uuid
import redis
import boto3
import telemetry

redis_host = os.environ.get("REDIS_HOST", "localhost")
redis_port = int(os.environ.get("REDIS_PORT", 6380))
r = redis.Redis(host=redis_host, port=redis_port, ssl=True, decode_responses=True)

dynamodb = boto3.resource("dynamodb")
table_name = os.environ.get("ADMISSIONS_TABLE")
admissions_table = dynamodb.Table(table_name)

admit_per_second = float(os.environ.get("ADMIT_PER_SECOND", "10"))
admission_valid_seconds = int(os.environ.get("ADMISSION_VALID_SECONDS", "600"))

def lambda_handler(event, context):
    try:
        visitor_id = event["pathParameters"]["visitor_id"]
        # Validate UUID format to satisfy the 400 contract requirement
        uuid.UUID(visitor_id)
    except (KeyError, ValueError):
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "malformed_visitor_id"})
        }

    visitor_key = f"visitor:{visitor_id}"
    visitor_data = r.hgetall(visitor_key)
    
    if not visitor_data:
        return {
            "statusCode": 403,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "not_in_queue_or_bot"})
        }
        
    position = int(visitor_data.get("position", 0))
    join_time = float(visitor_data.get("join_time", 0))
    token = visitor_data.get("token")
    
    now = time.time()
    
    # 1. If a token exists in the cache, they have already crossed the gate
    if token:
        # Check DDB to see if the token was already consumed by the OrderProducer
        response = admissions_table.get_item(Key={"visitor_id": visitor_id})
        item = response.get("Item")
        
        if item and item.get("used"):
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"status": "admitted", "order_placed": True})
            }
        elif item:
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({
                    "status": "admitted", 
                    "admission_token": item["admission_token"],
                    "expires_at": int(item["expires_at"])
                })
            }
        else:
            return {
                "statusCode": 403,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": "admission_record_missing_or_expired"})
            }

    # 2. Calculate the currently advancing serving counter
    start_time = float(r.get("wr_start_time") or now)
    elapsed = now - start_time
    serving_counter = elapsed * admit_per_second
    
    # 3. Admission Check
    if position <= serving_counter:
        new_token = str(uuid.uuid4())
        expires_at = int(now + admission_valid_seconds)
        
        # Pin the token to their profile hash to prevent duplicate DDB writes on subsequent polls
        r.hset(visitor_key, "token", new_token)
        
        # Execute the formal admission write to DynamoDB
        admissions_table.put_item(Item={
            "visitor_id": visitor_id,
            "admission_token": new_token,
            "used": False,
            "expires_at": expires_at,
            "ttl": expires_at + 300 # Buffer for cleanup
        })
        
        telemetry.admitted()
        telemetry.time_in_line(now - join_time)
        
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "status": "admitted",
                "admission_token": new_token,
                "expires_at": expires_at
            })
        }
        
    # 4. If their position > serving_counter, they stay in the queue
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"status": "waiting"})
    }