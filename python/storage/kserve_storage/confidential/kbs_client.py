# Copyright 2025 The KServe Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os
import re

import requests

from .secret_resolver import SecretResolutionError, SecretResolver

logger = logging.getLogger(__name__)

# kbs:///<repo>/<type>/<tag>
_KBS_RESOURCE_ID_RE = re.compile(r"^kbs:///(?P<repo>[^/]+)/(?P<type>[^/]+)/(?P<tag>[^/]+)$")


class KBSSecretResolver(SecretResolver):
    """Resolves decryption keys from a Confidential Containers Key Broker Service (KBS).

    The KBS URL is read from the ``KBS_URL`` environment variable.  Attestation
    is handled transparently by the CoCo guest components / attestation-agent
    at the transport level — this client simply issues an HTTP GET to the KBS
    resource endpoint.
    """

    def __init__(self, kbs_url: str | None = None, timeout: int = 30):
        self._kbs_url = kbs_url or os.environ.get("KBS_URL")
        if not self._kbs_url:
            raise SecretResolutionError(
                "KBS_URL environment variable is not set and no kbs_url was provided"
            )
        # Strip trailing slash for consistent URL construction
        self._kbs_url = self._kbs_url.rstrip("/")
        self._timeout = timeout

    def resolve_key(self, resource_id: str) -> bytes:
        """Retrieve a decryption key from KBS for the given resource identifier.

        Args:
            resource_id: A KBS resource URI in the format ``kbs:///<repo>/<type>/<tag>``.

        Returns:
            The raw key bytes.

        Raises:
            SecretResolutionError: If the resource ID is malformed, the KBS is
                unreachable, or the key cannot be retrieved.
        """
        match = _KBS_RESOURCE_ID_RE.match(resource_id)
        if not match:
            raise SecretResolutionError(
                f"Invalid KBS resource ID format: {resource_id!r}, "
                "expected kbs:///<repo>/<type>/<tag>"
            )

        repo = match.group("repo")
        rtype = match.group("type")
        tag = match.group("tag")

        url = f"{self._kbs_url}/kbs/v0/resource/{repo}/{rtype}/{tag}"
        logger.info("Requesting key from KBS: %s", url)

        try:
            response = requests.get(url, timeout=self._timeout)
            response.raise_for_status()
        except requests.RequestException as e:
            raise SecretResolutionError(f"Failed to retrieve key from KBS: {e}") from e

        return response.content
