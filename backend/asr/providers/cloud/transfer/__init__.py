"""How the cloud provider makes audio reachable by URL.

See docs/research/file-transfer-for-cloud-asr.md.
"""

from asr.providers.cloud.transfer.public_url import validate_public_url
from asr.providers.cloud.transfer.temp_upload import (
    UploadPolicy,
    fetch_policy,
    safe_object_name,
    upload_file,
)

__all__ = ["UploadPolicy", "fetch_policy", "safe_object_name", "upload_file", "validate_public_url"]
