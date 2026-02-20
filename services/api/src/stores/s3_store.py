from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import boto3


@dataclass(frozen=True)
class PutObjectResult:
    bucket: str
    key: str
    sha256: str
    byte_size: int


class S3EvidenceStore:
    def __init__(self, *, region: str):
        self._client = boto3.client("s3", region_name=region)

    @staticmethod
    def _to_bytes(payload: dict[str, Any]) -> bytes:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")

    def put_json(self, *, bucket: str, key: str, payload: dict[str, Any]) -> PutObjectResult:
        body = self._to_bytes(payload)
        sha = hashlib.sha256(body).hexdigest()

        self._client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
        )

        return PutObjectResult(bucket=bucket, key=key, sha256=sha, byte_size=len(body))

    def get_json(self, bucket: str, key: str) -> dict[str, Any]:
        resp = self._client.get_object(Bucket=bucket, Key=key)
        body = resp["Body"].read().decode("utf-8")
        return json.loads(body)
