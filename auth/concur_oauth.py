# auth/concur_oauth.py
from __future__ import annotations

import time
from typing import Optional, Dict
import requests


class ConcurOAuthClient:
    """
    Phase 1: small, dependency-free OAuth client.

    - No Key Vault access here
    - No 'app.*' imports
    - Caller provides token_url + secrets (client_id, client_secret, refresh_token)
    """

    def __init__(self, *, token_url: str, client_id: str, client_secret: str, refresh_token: str):
        self.token_url = token_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token

        self._access_token: Optional[str] = None
        self._expires_at: float = 0.0

    def get_access_token(self) -> str:
        now = time.time()
        if self._access_token and now < self._expires_at - 60:
            return self._access_token

        resp = requests.post(
            self.token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=30,
        )
        resp.raise_for_status()

        data: Dict = resp.json() or {}
        self._access_token = str(data["access_token"])
        self._expires_at = now + float(data.get("expires_in", 1800))
        return self._access_token
