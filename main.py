import asyncio
import httpx
import os
import json
from datetime import datetime, UTC

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from pydantic_settings import BaseSettings
from discord_webhook import AsyncDiscordWebhook, DiscordEmbed
from typing import List, Union, TypedDict

load_dotenv()


class Settings(BaseSettings):
    discord_webhook_url: str
    lock_file_path: str = "version.lock"
    # Cloudflare R2 (S3-compatible) credentials for uploading SDKs.
    r2_account_id: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_bucket: str
    r2_endpoint: str
    skip_r2_upload: bool = False
    skip_r2_mapping_update: bool = False


# Module-level settings instance. Constructing this raises if a required
# field (e.g. DISCORD_WEBHOOK_URL) is missing, so startup fails fast.
env = Settings()

LOCK_FILE = env.lock_file_path
URL = "https://download.portal.battlefield.com/versions.json"
SDK_DOWNLOAD_URL = "https://download.portal.battlefield.com/PortalSDK.zip"
# Object key (in the R2 bucket) holding the version -> file mapping the Worker reads.
MAPPING_KEY = "versions.json"
# Multipart part size. Must be >= 5 MiB (S3 minimum for non-final parts).
MULTIPART_PART_SIZE = 100 * 1024 * 1024  # 100 MiB
# Using the specific User-Agent from the curl command to ensure consistent behavior
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:143.0) Gecko/20100101 Firefox/143.0 PortalSDKWachtower/https://github.com/battlefield-portal-community/PortalSDKWachtower"


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

class VersionEntry(TypedDict):
    version: str
    fileSize: int
    lastModified: str

class HumanBytes:
    METRIC_LABELS: List[str] = ["B", "kB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB"]
    BINARY_LABELS: List[str] = ["B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB", "ZiB", "YiB"]
    PRECISION_OFFSETS: List[float] = [0.5, 0.05, 0.005, 0.0005] # PREDEFINED FOR SPEED.
    PRECISION_FORMATS: List[str] = ["{}{:.0f} {}", "{}{:.1f} {}", "{}{:.2f} {}", "{}{:.3f} {}"] # PREDEFINED FOR SPEED.

    @staticmethod
    def format(num: Union[int, float], metric: bool=False, precision: int=1) -> str:
        """
        Human-readable formatting of bytes, using binary (powers of 1024)
        or metric (powers of 1000) representation.
        """

        assert isinstance(num, (int, float)), "num must be an int or float"
        assert isinstance(metric, bool), "metric must be a bool"
        assert isinstance(precision, int) and 0 <= precision <= 3, "precision must be an int (range 0-3)"

        unit_labels = HumanBytes.METRIC_LABELS if metric else HumanBytes.BINARY_LABELS
        last_label = unit_labels[-1]
        unit_step = 1000 if metric else 1024
        unit_step_thresh = unit_step - HumanBytes.PRECISION_OFFSETS[precision]

        is_negative = num < 0
        if is_negative: # Faster than ternary assignment or always running abs().
            num = abs(num)

        for unit in unit_labels:
            if num < unit_step_thresh:
                # VERY IMPORTANT:
                # Only accepts the CURRENT unit if we're BELOW the threshold where
                # float rounding behavior would place us into the NEXT unit: F.ex.
                # when rounding a float to 1 decimal, any number ">= 1023.95" will
                # be rounded to "1024.0". Obviously, we don't want ugly output such
                # as "1024.0 KiB", since the proper term for that is "1.0 MiB".
                break
            if unit != last_label:
                # We only shrink the number if we HAVEN'T reached the last unit.
                # NOTE: These looped divisions accumulate floating point rounding
                # errors, but each new division pushes the rounding errors further
                # and further down in the decimals, so it doesn't matter at all.
                num /= unit_step

        return HumanBytes.PRECISION_FORMATS[precision].format("-" if is_negative else "", num, unit)

async def send_discord_webhook(version: str, file_size: float, old_version: str, old_size: float) -> None:
    file_size_readable = HumanBytes.format(file_size, metric=True)
    size_change = HumanBytes.format(file_size - old_size, metric=True, precision=3)
    if old_size < file_size:
        size_change = f"+{size_change}"

    allowed_mentions = {
        "roles": ["916729041002852363", "916779659239239691"]
    }
    content = "New Portal SDK Version Available! <@&916729041002852363>"
    webhook = AsyncDiscordWebhook(url=env.discord_webhook_url, allowed_mentions=allowed_mentions, content=content )
    embed = DiscordEmbed(username="Portal SDK Watchtower", color=0x00ff00)
    embed.set_thumbnail(url="https://lis.bfportal.gg/portal-animation-logo.gif")
    embed.add_embed_field(name="New Version", value=f"`{old_version} -> {version}`")
    embed.add_embed_field(name="File Size", value=f"{file_size_readable}")
    embed.add_embed_field(name="", value="[Download](https://download.portal.battlefield.com/PortalSDK.zip)", inline=False)
    if file_size - old_size != 0:
        embed.add_embed_field(name="Size Change", value=f"`{size_change}`")
    embed.set_timestamp(datetime.now(UTC))
    embed.set_footer(text="Portal SDK Watchtower")

    webhook.add_embed(embed)
    await webhook.execute()

async def get_version_details() -> VersionEntry | None:
    headers = {'User-Agent': USER_AGENT}
    
    async with httpx.AsyncClient() as client:
        response = await client.get(URL, headers=headers)
        
        if response.status_code != 200:
            print(f"Failed to fetch data. Status code: {response.status_code}")
            return None

        data = response.json()
        
        # Logic corresponds to jq: .versions | last
        versions_list = data.get('versions', [])
        if not versions_list:
            print("Error: 'versions' list is empty or missing in JSON response.")
            return None

        latest_entry = versions_list[-1]
        latest_entry["fileSize"] = int(latest_entry["fileSize"])
        return latest_entry

async def check_version(current_sdk_version: str, current_sdk_size: float):
    try:
        details = await get_version_details()
        
        if not details:
            return current_sdk_version, current_sdk_size

        new_version = details['version']
        new_size = details['fileSize']
        # Check Version and Size Difference
        version_mismatch = current_sdk_version != new_version
        size_mismatch = current_sdk_size != new_size
        
        if version_mismatch:
                print(f"Portal SDK version has changed. Old: {current_sdk_version}, New: {new_version}")
        
        if size_mismatch:
                print(f"Portal SDK size has changed. Old: {current_sdk_size}, New: {new_size}")
        if version_mismatch or size_mismatch:
            # Stamp the moment we detected this release, in UTC. Reused for both
            # the lock file and the R2 entry so they agree (and so the R2 time
            # reflects detection, not upload-completion which can lag by hours).
            detected_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
            details["lastModified"] = detected_at

            try:
                with open(LOCK_FILE, 'w') as f:
                    json.dump(details, f, indent=4)
                print(f"Updated {LOCK_FILE} with new version info.")
            except Exception as e:
                print(f"Failed to update {LOCK_FILE}: {e}")

            # Notify first so the ping goes out ASAP. A failure here must NOT
            # block the R2 upload below — the two are independent.
            try:
                await send_discord_webhook(new_version, new_size, current_sdk_version, current_sdk_size)
            except Exception as e:
                print(f"Failed to send Discord notification: {e}")

            # Download the new SDK, upload it to R2, and update the mapping.
            try:
                await upload_sdk_to_r2(new_version, new_size, detected_at)
            except Exception as e:
                print(f"Failed to upload SDK to R2 / update mapping: {e}")

            return new_version, new_size

        return current_sdk_version, current_sdk_size

    except httpx.RequestError as e:
        print(f"An error occurred while requesting {e.request.url!r}: {e}")
        return current_sdk_version, current_sdk_size
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return current_sdk_version, current_sdk_size

async def main():
    current_sdk_version: str | None = None
    current_sdk_size: float | None = None

    if os.path.exists(LOCK_FILE):
        print(f"Reading configuration from {LOCK_FILE}...")
        try:
            with open(LOCK_FILE, 'r') as f:
                data = json.load(f)
                current_sdk_version = data.get('version')
                current_sdk_size = data.get('fileSize')
        except Exception as e:
            print(f"Error reading lock file: {e}")

    if not current_sdk_version or not current_sdk_size:
        print(f"{LOCK_FILE} not found or invalid. Fetching latest version to initialize...")
        current_sdk_version, current_sdk_size = await check_version("INVALID", 0)

    print(f"Current Baseline: Version={current_sdk_version}, Size={current_sdk_size}")

    if current_sdk_version  is None or current_sdk_size is None:
        print("Failed to fetch latest version to create baseline. Exiting...")
        return
    while True:
        try:
            if current_sdk_version and current_sdk_size:
                current_sdk_version, current_sdk_size = await check_version(current_sdk_version, current_sdk_size)
            await asyncio.sleep(10)
        except KeyboardInterrupt:
            print("Exiting...")
            return

if __name__ == "__main__":
    asyncio.run(main())
