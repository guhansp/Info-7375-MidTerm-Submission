"""
JOIN  (POST /waiting-room/join)  -  YOURS TO WRITE
==================================================
Answer exactly as CONTRACT.md section 4 says:

    202  {"visitor_id": "<uuid>", "status": "waiting", "poll_interval_seconds": <n>, "poll_path": "..."}

Environment variables the skeleton sets (add your own in cdk/waiting_room.py):
    ADMISSIONS_TABLE, ADMIT_PER_SECOND, POLL_INTERVAL_SECONDS, ADMISSION_VALID_SECONDS

When a visitor gets a place in line, count them with the provided helper (one line, once per visitor):

    import telemetry
    telemetry.joined()
"""
import json
import os
import uuid
import time
import redis
import telemetry

redis_host = os.environ.get("REDIS_HOST", "localhost")
redis_port = int(os.environ.get("REDIS_PORT", 6380))
r = redis.Redis(host=redis_host, port=redis_port, decode_responses=True)

def lambda_handler(event, context):
    visitor_id = str(uuid.uuid4())
    now = time.time()
    rate = float(os.environ.get("ADMIT_PER_SECOND", "10"))
    
    # 1. Assign strict queue position
    position = r.incr("v5_wr_queue_position")
    
    # 2. Math-based pacing: Handle lulls by fast-forwarding the virtual start time
    current_start_raw = r.get("v5_virtual_start")
    if not current_start_raw:
        r.setnx("v5_virtual_start", now)
        virtual_start = now
    else:
        virtual_start = float(current_start_raw)
        
    target_time = virtual_start + (position / rate)
    
    # If the queue is empty/idle, target_time falls into the past. Fast-forward it.
    if target_time < now:
        new_virtual_start = now - (position / rate)
        r.set("v5_virtual_start", new_virtual_start)
    
    # 3. Track visitor state
    r.hset(f"v5_visitor:{visitor_id}", mapping={
        "position": position,
        "join_time": now
    })
    r.expire(f"v5_visitor:{visitor_id}", 3600)
    
    telemetry.joined()
    poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "5"))
    
    return {
        "statusCode": 202,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "visitor_id": visitor_id,
            "status": "waiting",
            "poll_interval_seconds": poll_interval,
            "poll_path": f"/waiting-room/status/{visitor_id}"
        }),
    }