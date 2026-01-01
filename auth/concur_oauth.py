# auth/concur_oauth.py
from __future__ import annotations

import time
from typing import Optional, Dict, Tuple
import requests


class ConcurOAuthClient:
    """
    Phase 1: small, dependency-free OAuth client.

    - No Key Vault access here
    - No 'app.*' imports
    - Caller provides token_url + secrets (client_id, client_secret, refresh_token)
    """

    def __init__(self, *, token_url: str, client_id: str, client_secret: str, refresh_token: str):
        self.token_url = (token_url or "").strip().rstrip("/")
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self.refresh_token = (refresh_token or "").strip()

        if not self.token_url or not self.client_id or not self.client_secret or not self.refresh_token:
            raise ValueError("ConcurOAuthClient missing required config (token_url/client_id/client_secret/refresh_token)")

        self._access_token: Optional[str] = None
        self._expires_at: float = 0.0

    def get_access_token(self) -> str:
        """Returns a cached access token if valid, otherwise refreshes it."""
        token, _maybe_new_refresh = self.get_access_token_with_refresh_token()
        return token

    def get_access_token_with_refresh_token(self) -> Tuple[str, Optional[str]]:
        """
        Returns (access_token, new_refresh_token_if_returned).

        Concur may return a new refresh_token. We update in-memory refresh_token automatically and
        also return it so the caller can persist it (DB/KeyVault write-enabled setup later).
        """
        now = time.time()
        if self._access_token and now < self._expires_at - 60:
            return self._access_token, None

        resp = requests.post(
            self.token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Accept": "application/json"},
            timeout=30,
        )

        # Preserve useful error detail
        if resp.status_code >= 400:
            raise RuntimeError(f"Concur token refresh failed: HTTP {resp.status_code} - {resp.text}")

        data: Dict = resp.json() or {}
        access = data.get("access_token")
        if not access:
            raise RuntimeError(f"Concur token response missing access_token. Keys={list(data.keys())}")

        # Cache token
        self._access_token = str(access)
        self._expires_at = now + float(data.get("expires_in", 1800))

        # Handle refresh rotation
        new_refresh = data.get("refresh_token")
        if new_refresh and isinstance(new_refresh, str) and new_refresh.strip() and new_refresh.strip() != self.refresh_token:
            self.refresh_token = new_refresh.strip()
            return self._access_token, self.refresh_token

        return self._access_token, None
