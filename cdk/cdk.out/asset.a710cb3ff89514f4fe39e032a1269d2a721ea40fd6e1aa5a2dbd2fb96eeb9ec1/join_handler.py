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

# Initialize connection outside handler for container reuse
redis_host = os.environ.get("REDIS_HOST", "localhost")
redis_port = int(os.environ.get("REDIS_PORT", 6380))
r = redis.Redis(host=redis_host, port=redis_port, decode_responses=True)

def lambda_handler(event, context):
    visitor_id = str(uuid.uuid4())
    now = time.time()
    
    # 1. Start the global waiting room clock if this is the first arrival (SETNX)
    r.setnx("wr_start_time", now)
    
    # 2. Assign a strict, atomic queue position
    position = r.incr("wr_queue_position")
    
    # 3. Track the visitor's state in a Redis Hash
    r.hset(f"visitor:{visitor_id}", mapping={
        "position": position,
        "join_time": now
    })
    
    # Optional: TTL to purge abandoned sessions from the cache automatically
    r.expire(f"visitor:{visitor_id}", 3600)
    
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