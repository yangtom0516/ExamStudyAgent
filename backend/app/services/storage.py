"""File storage over the S3 API (SeaweedFS locally; any S3-compatible store works).

boto3 is synchronous, so each call runs in a worker thread to keep the event loop free.
"""

import asyncio

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import Settings


class Storage:
    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.s3_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
            region_name="us-east-1",  # required by boto3; ignored by SeaweedFS
            config=Config(
                s3={"addressing_style": "path"},  # http://host:8333/<bucket>/<key>, not <bucket>.host
                connect_timeout=3,
                read_timeout=10,
                retries={"max_attempts": 2},
            ),
        )

    async def bucket_exists(self) -> bool:
        try:
            await asyncio.to_thread(self._client.head_bucket, Bucket=self.bucket)
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchBucket", "NotFound"):
                return False
            raise

    async def ensure_bucket(self) -> None:
        if not await self.bucket_exists():
            await asyncio.to_thread(self._client.create_bucket, Bucket=self.bucket)
