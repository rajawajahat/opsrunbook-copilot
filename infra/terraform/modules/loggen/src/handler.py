import json
import os
import traceback
import uuid
from datetime import datetime, timezone


def lambda_handler(event, context):
    rid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    print(f"[INFO] loggen start rid={rid} ts={now} app={os.getenv('APP','opsrunbook')}")
    print("[WARN] simulated warning: upstream latency high")

    # Fake secret patterns to validate redaction
    print("Authorization: Bearer abc.def.ghi")
    print("password=supersecret123")
    print("aws_secret_access_key=abcdefghijklmnopqrstuvwxyz1234567890/+==")

    try:
        raise RuntimeError("Simulated failure for OpsRunbook Copilot testing")
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e} rid={rid}")
        print(traceback.format_exc())

    return {
        "ok": True,
        "rid": rid,
        "event": event,
    }
