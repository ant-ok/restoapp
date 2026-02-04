from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests
from django.conf import settings


class PosterConfigError(RuntimeError):
    pass


class PosterAPIError(RuntimeError):
    pass


@dataclass
class PosterClient:
    base_url: str
    token: str
    auth_style: str
    timeout: int = 20

    @classmethod
    def from_settings(cls) -> "PosterClient":
        base_url = settings.POSTER_API_BASE_URL
        token = settings.POSTER_API_TOKEN
        auth_style = settings.POSTER_AUTH_STYLE

        missing = [
            name
            for name, value in [
                ("POSTER_API_BASE_URL", base_url),
                ("POSTER_API_TOKEN", token),
                ("POSTER_AUTH_STYLE", auth_style),
            ]
            if not value
        ]
        if missing:
            raise PosterConfigError(
                "Missing settings: " + ", ".join(missing)
            )

        return cls(base_url=base_url, token=token, auth_style=auth_style)

    def _apply_auth(
        self,
        params: Dict[str, Any],
        headers: Dict[str, str],
    ) -> tuple[Dict[str, Any], Dict[str, str]]:
        if self.auth_style == "query_token":
            params = dict(params)
            params["token"] = self.token
            return params, headers
        if self.auth_style == "query_access_token":
            params = dict(params)
            params["access_token"] = self.token
            return params, headers
        if self.auth_style == "bearer":
            headers = dict(headers)
            headers["Authorization"] = f"Bearer {self.token}"
            return params, headers
        raise PosterConfigError(
            "Unknown POSTER_AUTH_STYLE. Use 'query_token', 'query_access_token', or 'bearer'."
        )

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self.request("GET", path, params=params)

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        form_body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        params = params or {}
        headers: Dict[str, str] = {}
        params, headers = self._apply_auth(params, headers)
        url = f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"

        try:
            response = requests.request(
                method=method.upper(),
                url=url,
                params=params,
                json=json_body,
                data=form_body,
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise PosterAPIError(str(exc)) from exc

        try:
            return response.json()
        except ValueError as exc:
            raise PosterAPIError("Response is not valid JSON") from exc
