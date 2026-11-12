"""File storage over the S3 API (SeaweedFS locally; any S3-compatible store works).

boto3 is synchronous, so each call runs in a worker thread to keep the event loop free.

Key layout: sessions/<session_id>/<document_id>.<ext>, so a session's files can be
deleted together by prefix.
"""

import asyncio
import uuid
from typing import BinaryIO

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import Settings

NOT_FOUND_CODES = ("404", "NoSuchBucket", "NoSuchKey", "NotFound")


def session_prefix(session_id: uuid.UUID) -> str:
    return f"sessions/{session_id}/"


def document_key(session_id: uuid.UUID, document_id: uuid.UUID, extension: str) -> str:
    return f"{session_prefix(session_id)}{document_id}{extension}"


def _is_not_found(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") in NOT_FOUND_CODES


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
                # Newer boto3 adds AWS-specific checksums to every upload by default; only send them
                # when an operation requires it, for compatibility with non-AWS S3 stores.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                connect_timeout=3,
                read_timeout=30,
                retries={"max_attempts": 2},
            ),
        )

    # --- bucket ---

    async def bucket_exists(self) -> bool:
        try:
            await asyncio.to_thread(self._client.head_bucket, Bucket=self.bucket)
            return True
        except ClientError as exc:
            if _is_not_found(exc):
                return False
            raise

    async def ensure_bucket(self) -> None:
        if not await self.bucket_exists():
            await asyncio.to_thread(self._client.create_bucket, Bucket=self.bucket)

    # --- objects ---

    async def upload(self, key: str, fileobj: BinaryIO, content_type: str) -> None:
        await asyncio.to_thread(
            self._client.upload_fileobj,
            fileobj,
            self.bucket,
            key,
            ExtraArgs={"ContentType": content_type},
        )

    async def download(self, key: str) -> bytes:
        def _read() -> bytes:
            return self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

        return await asyncio.to_thread(_read)

    async def exists(self, key: str) -> bool:
        try:
            await asyncio.to_thread(self._client.head_object, Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            if _is_not_found(exc):
                return False
            raise

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._client.delete_object, Bucket=self.bucket, Key=key)

    async def delete_prefix(self, prefix: str) -> None:
        """Delete every object whose key starts with `prefix` (e.g. all files of a session)."""

        def _delete_all() -> None:
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    self._client.delete_object(Bucket=self.bucket, Key=obj["Key"])

        await asyncio.to_thread(_delete_all)
