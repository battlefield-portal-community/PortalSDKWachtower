import asyncio
import json
import os
from datetime import datetime, UTC

import httpx

from app.config import LOCK_FILE, URL, USER_AGENT, env
from app.discord import send_discord_webhook
from app.humanbytes import VersionEntry
from app.r2 import upload_sdk_to_r2


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
                if not env.skip_local_mapping_update:
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
