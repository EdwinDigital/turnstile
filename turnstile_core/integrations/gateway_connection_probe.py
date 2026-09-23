from __future__ import annotations

import httpx
from pydantic import HttpUrl


class GatewayConnectionProbeError(RuntimeError):
    pass


class GatewayConnectionProbe:
    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._client = client
        self._timeout_seconds = timeout_seconds

    def validate(self, endpoint_url: HttpUrl, *, header_name: str, credential: str) -> None:
        url = f"{str(endpoint_url).rstrip('/')}/v1/models"
        headers = {header_name: credential}
        try:
            if self._client is not None:
                response = self._client.get(
                    url,
                    headers=headers,
                    follow_redirects=False,
                    timeout=self._timeout_seconds,
                )
            else:
                with httpx.Client(timeout=self._timeout_seconds, follow_redirects=False) as client:
                    response = client.get(url, headers=headers)
        except httpx.HTTPError as error:
            raise GatewayConnectionProbeError("Gateway endpoint could not be reached") from error
        if response.status_code != 200:
            raise GatewayConnectionProbeError(
                f"Gateway validation returned HTTP {response.status_code}"
            )
