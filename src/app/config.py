from dotenv import load_dotenv
from pydantic_settings import BaseSettings

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
    skip_local_mapping_update: bool = False


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
