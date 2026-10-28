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
r = redis.Redis(host=redis_host, port=redis_port, decode_responses=True)

dynamodb = boto3.resource("dynamodb")
table_name = os.environ.get("ADMISSIONS_TABLE")
admissions_table = dynamodb.Table(table_name)

admit_per_second = float(os.environ.get("ADMIT_PER_SECOND", "10"))
admission_valid_seconds = int(os.environ.get("ADMISSION_VALID_SECONDS", "600"))

def lambda_handler(event, context):
    try:
        visitor_id = event["pathParameters"]["visitor_id"]
        uuid.UUID(visitor_id)
    except (KeyError, ValueError):
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "malformed_visitor_id"})
        }

    visitor_key = f"v4_visitor:{visitor_id}"
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
    
    if token:
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

    
# 2. Lazily advance the serving counter (FIXED CLOCK INITIALIZATION & PACING)
    last_eval_time_raw = r.get("v4_last_eval_time")
    if not last_eval_time_raw:
        r.set("v4_last_eval_time", now)
        last_eval_time = now
    else:
        last_eval_time = float(last_eval_time_raw)
        
    serving_pos = float(r.get("v4_serving_pos") or 0.0)
    max_pos = float(r.get("v4_wr_queue_position") or 0.0)
    
    elapsed = now - last_eval_time
    
    # CRITICAL FIX: Only generate capacity if at least 0.1 seconds have passed 
    # to prevent micro-polling from artificially inflating the rate.
    if elapsed >= 0.1:
        new_serving_pos = serving_pos + (elapsed * admit_per_second)
        
        if new_serving_pos > max_pos:
            new_serving_pos = max_pos
            
        r.set("v4_serving_pos", new_serving_pos)
        r.set("v4_last_eval_time", now)
        serving_pos = new_serving_pos
    
    # 3. Admission Check
    if position <= serving_pos:
        new_token = str(uuid.uuid4())
        expires_at = int(now + admission_valid_seconds)
        
        r.hset(visitor_key, "token", new_token)
        
        admissions_table.put_item(Item={
            "visitor_id": visitor_id,
            "admission_token": new_token,
            "used": False,
            "expires_at": expires_at,
            "ttl": expires_at + 300
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
        
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"status": "waiting"})
    }