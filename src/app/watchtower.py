import asyncio
import json
import os
from datetime import datetime, UTC

import httpx

from app.config import LOCK_FILE, URL, USER_AGENT, env
from app.discord import send_discord_webhook
from app.humanbytes import VersionEntry
from app.r2 import upload_sdk_to_r2

# Sentinel returned when the CDN reports the manifest is unchanged (HTTP 304).
NOT_MODIFIED = object()


async def get_version_details(
    client: httpx.AsyncClient, etag: str | None
) -> tuple[VersionEntry | object | None, str | None]:
    """Fetch the latest version entry from versions.json.

    Sends a conditional request (If-None-Match) so the CDN can answer 304 when
    nothing changed — no body to download or parse. Returns ``(result, etag)``
    where ``result`` is the newest VersionEntry, the ``NOT_MODIFIED`` sentinel,
    or ``None`` on a soft failure (bad status / malformed body). The returned
    etag should be fed back into the next call.
    """
    headers = {'User-Agent': USER_AGENT}
    if etag:
        headers['If-None-Match'] = etag

    response = await client.get(URL, headers=headers)

    if response.status_code == 304:
        return NOT_MODIFIED, etag

    if response.status_code != 200:
        print(f"Failed to fetch data. Status code: {response.status_code}")
        return None, etag

    data = response.json()

    # Logic corresponds to jq: .versions | last
    versions_list = data.get('versions', [])
    if not versions_list:
        print("Error: 'versions' list is empty or missing in JSON response.")
        return None, etag

    latest_entry = versions_list[-1]
    latest_entry["fileSize"] = int(latest_entry["fileSize"])
    # Keep the CDN's ETag if it sent one; otherwise reuse the previous value.
    return latest_entry, response.headers.get("ETag", etag)


async def check_version(
    client: httpx.AsyncClient,
    current_sdk_version: str,
    current_sdk_size: float,
    etag: str | None,
) -> tuple[str, float, str | None]:
    details, etag = await get_version_details(client, etag)

    # Unchanged (304) or a soft failure: keep the current baseline as-is.
    if details is NOT_MODIFIED or details is None:
        return current_sdk_version, current_sdk_size, etag

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

        return new_version, new_size, etag

    return current_sdk_version, current_sdk_size, etag


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

    # One client for the whole run: reuses connections and applies a timeout so
    # a stalled connection fails fast instead of hanging the poll loop forever.
    timeout = httpx.Timeout(env.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        etag: str | None = None

        if not current_sdk_version or not current_sdk_size:
            print(f"{LOCK_FILE} not found or invalid. Fetching latest version to initialize...")
            try:
                current_sdk_version, current_sdk_size, etag = await check_version(
                    client, "INVALID", 0, etag
                )
            except httpx.RequestError as e:
                print(f"Could not reach {e.request.url!r} while initializing: {e}")

        print(f"Current Baseline: Version={current_sdk_version}, Size={current_sdk_size}")

        if current_sdk_version is None or current_sdk_size is None:
            print("Failed to fetch latest version to create baseline. Exiting...")
            return

        # Backoff + log dedup: a CDN outage otherwise spams one line per poll.
        # We log the first failure and then only every 10th, back off the poll
        # interval exponentially, and log once when the CDN recovers.
        base_interval = env.poll_interval_seconds
        backoff = base_interval
        consecutive_errors = 0

        while True:
            try:
                current_sdk_version, current_sdk_size, etag = await check_version(
                    client, current_sdk_version, current_sdk_size, etag
                )
                if consecutive_errors:
                    print(f"versions.json reachable again after {consecutive_errors} failed attempt(s).")
                consecutive_errors = 0
                backoff = base_interval
                sleep_for = base_interval
            except httpx.RequestError as e:
                consecutive_errors += 1
                if consecutive_errors == 1 or consecutive_errors % 10 == 0:
                    print(
                        f"versions.json unreachable ({consecutive_errors} consecutive "
                        f"failure(s)): {e}"
                    )
                backoff = min(backoff * 2, env.max_backoff_seconds)
                sleep_for = backoff
            except Exception as e:
                print(f"An unexpected error occurred: {e}")
                sleep_for = base_interval

            await asyncio.sleep(sleep_for)
