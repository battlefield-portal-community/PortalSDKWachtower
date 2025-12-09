import asyncio
import httpx
import sys
import os
import json

# Configuration from the YAML env
LOCK_FILE = "version.lock"
URL = "https://download.portal.battlefield.com/versions.json"
# Using the specific User-Agent from the curl command to ensure consistent behavior
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:143.0) Gecko/20100101 Firefox/143.0 PortalSDKWachtower/https://github.com/battlefield-portal-community/PortalSDKWachtower"

async def get_version_details():
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
        
        return {
            "version": latest_entry.get('version'),
            "fileSize": str(latest_entry.get('fileSize'))
        }

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
            current_sdk_version, current_sdk_size = await check_version(current_sdk_version, current_sdk_size)
            await asyncio.sleep(10)
        except KeyboardInterrupt:
            print("Exiting...")
            return

if __name__ == "__main__":
    asyncio.run(main())
