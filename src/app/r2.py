import asyncio
import json

import boto3
import httpx
from botocore.exceptions import ClientError

from app.config import (
    MAPPING_KEY,
    MULTIPART_PART_SIZE,
    SDK_DOWNLOAD_URL,
    USER_AGENT,
    env,
)


def _r2_client():
    return boto3.client(
        "s3",
        endpoint_url=env.r2_endpoint,
        aws_access_key_id=env.r2_access_key_id,
        aws_secret_access_key=env.r2_secret_access_key,
        region_name="auto",
    )


def _object_exists(client, key: str) -> bool:
    try:
        client.head_object(Bucket=env.r2_bucket, Key=key)
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def _stream_download_to_r2(client, key: str) -> int:
    """Stream the SDK zip straight into an R2 multipart upload.

    The full (~6.5 GB) file is never buffered to disk: chunks are read from the
    HTTP stream into an in-memory buffer and flushed as ~100 MiB parts.
    """
    if env.skip_r2_upload:
        return 0
    mpu = client.create_multipart_upload(
        Bucket=env.r2_bucket, Key=key, ContentType="application/zip"
    )
    upload_id = mpu["UploadId"]
    parts: list[dict] = []
    total = 0
    try:
        with httpx.Client(timeout=None) as hx:
            with hx.stream("GET", SDK_DOWNLOAD_URL, headers={"User-Agent": USER_AGENT}) as resp:
                resp.raise_for_status()
                buffer = bytearray()
                part_number = 1
                for chunk in resp.iter_bytes(chunk_size=8 * 1024 * 1024):
                    buffer.extend(chunk)
                    total += len(chunk)
                    while len(buffer) >= MULTIPART_PART_SIZE:
                        body = bytes(buffer[:MULTIPART_PART_SIZE])
                        del buffer[:MULTIPART_PART_SIZE]
                        result = client.upload_part(
                            Bucket=env.r2_bucket, Key=key, PartNumber=part_number,
                            UploadId=upload_id, Body=body,
                        )
                        parts.append({"ETag": result["ETag"], "PartNumber": part_number})
                        part_number += 1
                # Final part: whatever remains (any size is allowed for the last part).
                # Also covers the small-file case where no full part was flushed.
                if buffer or not parts:
                    result = client.upload_part(
                        Bucket=env.r2_bucket, Key=key, PartNumber=part_number,
                        UploadId=upload_id, Body=bytes(buffer),
                    )
                    parts.append({"ETag": result["ETag"], "PartNumber": part_number})
        client.complete_multipart_upload(
            Bucket=env.r2_bucket, Key=key, UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
        return total
    except Exception:
        client.abort_multipart_upload(Bucket=env.r2_bucket, Key=key, UploadId=upload_id)
        raise


def _update_mapping(client, entry: dict) -> None:
    if env.skip_r2_mapping_update:
        return
    try:
        obj = client.get_object(Bucket=env.r2_bucket, Key=MAPPING_KEY)
        mapping = json.loads(obj["Body"].read())
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            mapping = {"versions": []}
        else:
            raise

    versions = mapping.setdefault("versions", [])
    if not any(v.get("key") == entry["key"] for v in versions):
        versions.append(entry)
    client.put_object(
        Bucket=env.r2_bucket, Key=MAPPING_KEY,
        Body=json.dumps(mapping, indent=2).encode(),
        ContentType="application/json",
    )


def _upload_and_register(version: str, file_size: int, last_modified: str) -> None:
    """Blocking R2 work: upload the SDK (if needed) and update versions.json."""
    client = _r2_client()
    key = f"SDKs/PortalSDK-v{version}.zip"

    # Size that describes the object actually stored in R2. The API-advertised
    # file_size can be stale (observed 58 bytes larger than the real zip), so we
    # prefer the true stored-object size. Falls back to file_size when the upload
    # is skipped locally (env.skip_r2_upload) and there's no object to measure.
    actual_size = file_size
    if env.skip_r2_upload:
        print("R2 upload skipped (skip_r2_upload); using API-advertised size.")
    elif _object_exists(client, key):
        print(f"R2 object {key} already exists; skipping upload.")
        actual_size = client.head_object(Bucket=env.r2_bucket, Key=key)["ContentLength"]
    else:
        print(f"Uploading SDK to R2 as {key}...")
        actual_size = _stream_download_to_r2(client, key)
        print(f"Uploaded {key} ({actual_size} bytes) to R2.")

    entry = {
        "version": version,
        "key": key,
        "fileSize": actual_size,
        "lastModified": last_modified,
    }
    _update_mapping(client, entry)
    print(f"Updated {MAPPING_KEY} with {key}.")


async def upload_sdk_to_r2(version: str, file_size: int, last_modified: str) -> None:
    """Run the blocking R2 upload + mapping update off the event loop."""
    await asyncio.to_thread(_upload_and_register, version, file_size, last_modified)
