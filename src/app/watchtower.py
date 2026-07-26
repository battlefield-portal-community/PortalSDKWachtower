import asyncio
import json
import os
from datetime import datetime, UTC
from email.utils import parsedate_to_datetime
from enum import Enum

import httpx
from pydantic import BaseModel

from app.config import LOCK_FILE, URL, USER_AGENT, env
from app.discord import send_discord_webhook
from app.r2 import upload_sdk_to_r2


class VersionInfo(BaseModel):
    """A single entry from the versions.json manifest.

    Field names mirror the manifest / lock-file JSON keys so the model
    round-trips through ``json`` without aliasing. ``fileSize`` arrives as a
    string and is coerced to int; ``lastModified`` is absent from the manifest
    and filled in by us (from the CDN's Last-Modified header, or detection time).
    """
    version: str
    fileSize: int
    lastModified: str | None = None


class FetchStatus(Enum):
    UPDATED = "updated"            # 200 with a fresh manifest entry
    NOT_MODIFIED = "not_modified"  # 304 — nothing changed since last poll
    FAILED = "failed"              # bad status / malformed body (soft failure)


class FetchResult(BaseModel):
    """Outcome of a single versions.json fetch. ``entry`` is set only when
    ``status`` is UPDATED; ``etag`` should be fed back into the next fetch."""
    status: FetchStatus
    etag: str | None = None
    entry: VersionInfo | None = None


class SdkState(BaseModel):
    """The latest version/size we've seen, plus the ETag that tracks it."""
    version: str | None = None
    size: int | None = None
    etag: str | None = None


def _parse_http_date(value: str | None) -> str | None:
    """Convert an HTTP-date header (e.g. 'Tue, 21 Jul 2026 14:50:00 GMT') to our
    stored 'YYYY-MM-DD HH:MM:SS' UTC format. Returns None if the header is
    absent or unparseable, so callers can fall back to a local timestamp."""
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    # HTTP-dates are GMT; guard against a naive datetime just in case.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


async def get_version_details(
    client: httpx.AsyncClient, etag: str | None
) -> FetchResult:
    """Fetch the latest version entry from versions.json.

    Sends a conditional request (If-None-Match) so the CDN can answer 304 when
    nothing changed — no body to download or parse.
    """
    headers = {'User-Agent': USER_AGENT}
    if etag:
        headers['If-None-Match'] = etag

    response = await client.get(URL, headers=headers)

    if response.status_code == 304:
        return FetchResult(status=FetchStatus.NOT_MODIFIED, etag=etag)

    if response.status_code != 200:
        print(f"Failed to fetch data. Status code: {response.status_code}")
        return FetchResult(status=FetchStatus.FAILED, etag=etag)

    data = response.json()

    # Logic corresponds to jq: .versions | last
    versions_list = data.get('versions', [])
    if not versions_list:
        print("Error: 'versions' list is empty or missing in JSON response.")
        return FetchResult(status=FetchStatus.FAILED, etag=etag)

    entry = VersionInfo(**versions_list[-1])
    # Prefer the CDN's Last-Modified (when the manifest was published) over our
    # local detection time. check_version falls back if this is absent.
    published_at = _parse_http_date(response.headers.get("Last-Modified"))
    if published_at:
        entry.lastModified = published_at
    # Keep the CDN's ETag if it sent one; otherwise reuse the previous value.
    return FetchResult(
        status=FetchStatus.UPDATED,
        etag=response.headers.get("ETag", etag),
        entry=entry,
    )


async def check_version(client: httpx.AsyncClient, state: SdkState) -> SdkState:
    result = await get_version_details(client, state.etag)

    # Unchanged (304) or a soft failure: keep the baseline, but adopt whatever
    # ETag the fetch came back with.
    if result.status is not FetchStatus.UPDATED or result.entry is None:
        return state.model_copy(update={"etag": result.etag})

    entry = result.entry
    new_version = entry.version
    new_size = entry.fileSize
    # Check Version and Size Difference
    version_mismatch = state.version != new_version
    size_mismatch = state.size != new_size

    if version_mismatch:
        print(f"Portal SDK version has changed. Old: {state.version}, New: {new_version}")

    if size_mismatch:
        print(f"Portal SDK size has changed. Old: {state.size}, New: {new_size}")

    if version_mismatch or size_mismatch:
        # Timestamp of the release, in UTC. Prefer the manifest's Last-Modified
        # (set from the CDN header in get_version_details); fall back to the
        # moment we detected it if the CDN didn't send a usable header. Reused
        # for both the lock file and the R2 entry so they agree.
        published_at = entry.lastModified or datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        entry.lastModified = published_at

        try:
            if not env.skip_local_mapping_update:
                with open(LOCK_FILE, 'w') as f:
                    json.dump(entry.model_dump(), f, indent=4)
                print(f"Updated {LOCK_FILE} with new version info.")
        except Exception as e:
            print(f"Failed to update {LOCK_FILE}: {e}")

        # Notify first so the ping goes out ASAP. A failure here must NOT
        # block the R2 upload below — the two are independent.
        try:
            await send_discord_webhook(new_version, new_size, state.version, state.size)
        except Exception as e:
            print(f"Failed to send Discord notification: {e}")

        # Download the new SDK, upload it to R2, and update the mapping.
        try:
            await upload_sdk_to_r2(new_version, new_size, published_at)
        except Exception as e:
            print(f"Failed to upload SDK to R2 / update mapping: {e}")

    return SdkState(version=new_version, size=new_size, etag=result.etag)


async def main():
    state = SdkState()

    if os.path.exists(LOCK_FILE):
        print(f"Reading configuration from {LOCK_FILE}...")
        try:
            with open(LOCK_FILE, 'r') as f:
                data = json.load(f)
                state.version = data.get('version')
                state.size = data.get('fileSize')
        except Exception as e:
            print(f"Error reading lock file: {e}")

    # One client for the whole run: reuses connections and applies a timeout so
    # a stalled connection fails fast instead of hanging the poll loop forever.
    timeout = httpx.Timeout(env.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        if not state.version or not state.size:
            print(f"{LOCK_FILE} not found or invalid. Fetching latest version to initialize...")
            try:
                # Baseline that mismatches everything, so the first fetch is
                # treated as a new release (notify + upload).
                state = await check_version(client, SdkState(version="INVALID", size=0))
            except httpx.RequestError as e:
                print(f"Could not reach {e.request.url!r} while initializing: {e}")

        print(f"Current Baseline: Version={state.version}, Size={state.size}")

        if state.version is None or state.size is None:
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
                state = await check_version(client, state)
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
