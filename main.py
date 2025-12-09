import asyncio
import httpx
import os
import json
from datetime import datetime, UTC

from dotenv import load_dotenv
from discord_webhook import AsyncDiscordWebhook, DiscordEmbed
from typing import List, Union, TypedDict

load_dotenv()

# Configuration from the YAML env
LOCK_FILE = os.getenv("LOCK_FILE_PATH", "version.lock")
URL = "https://download.portal.battlefield.com/versions.json"
# Using the specific User-Agent from the curl command to ensure consistent behavior
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:143.0) Gecko/20100101 Firefox/143.0 PortalSDKWachtower/https://github.com/battlefield-portal-community/PortalSDKWachtower"
if not os.getenv("DISCORD_WEBHOOK_URL"):
    print("DISCORD_WEBHOOK_URL environment variable not set. Exiting...")
    exit(1)

class VersionEntry(TypedDict):
    version: str
    fileSize: int

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

    webhook = AsyncDiscordWebhook(url=os.getenv("DISCORD_WEBHOOK_URL"))
    embed = DiscordEmbed(username="Portal SDK Watchtower", color=0x00ff00)

    embed.title = f"New Portal SDK Version Available!"
    embed.description = "<@916729041002852363>"
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
            try:
                with open(LOCK_FILE, 'w') as f:
                    json.dump(details, f, indent=4)
                print(f"Updated {LOCK_FILE} with new version info.")
                await send_discord_webhook(new_version, new_size, current_sdk_version, current_sdk_size)
            except Exception as e:
                print(f"Failed to update {LOCK_FILE}: {e}")
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
