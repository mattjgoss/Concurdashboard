import time
import requests
from fastapi import HTTPException

from app.keyvault import get_secret


class ConcurOAuthClient:
    """
    Retrieves Concur access tokens using the Refresh Token grant.
    Secrets are read from Azure Key Vault via app.keyvault.get_secret().

    Supports either naming convention:
      - Preferred: concur-api-base-url / concur-client-id / concur-client-secret / concur-refresh-token
      - Legacy:   CONCUR_BASE_URL / CONCUR_CLIENT_ID / CONCUR_CLIENT_SECRET / CONCUR_REFRESH_TOKEN
    """

    def __init__(self):
        self._access_token = None
        self._expires_at = 0.0
        self._cfg = None  # lazy-loaded config dict

    # --------------------------
    # Config / Secrets
    # --------------------------

    def _read_secret(self, preferred: str, legacy: str) -> str:
        """
        Try preferred secret name first, then legacy.
        """
        try:
            v = get_secret(preferred)
            if v:
                return v
        except Exception:
            pass
        return get_secret(legacy)

    def _load_config(self) -> dict:
        """
        Lazy-load secrets from Key Vault once, then reuse.
        Keeps startup resilient (docs can load even if KV is temporarily unavailable).
        """
        if self._cfg is not None:
            return self._cfg

        base_url = self._read_secret("concur-api-base-url", "CONCUR_BASE_URL").rstrip("/")
        client_id = self._read_secret("concur-client-id", "CONCUR_CLIENT_ID")
        client_secret = self._read_secret("concur-client-secret", "CONCUR_CLIENT_SECRET")
        refresh_token = self._read_secret("concur-refresh-token", "CONCUR_REFRESH_TOKEN")

        self._cfg = {
            "base_url": base_url,
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        }
        return self._cfg

    @property
    def base_url(self) -> str:
        return self._load_config()["base_url"]

    # --------------------------
    # Token
    # --------------------------

    def get_access_token(self) -> str:
        now = time.time()
        if self._access_token and now < self._expires_at - 60:
            return self._access_token

        cfg = self._load_config()
        token_url = f'{cfg["base_url"]}/oauth2/v0/token'

        try:
            resp = requests.post(
                token_url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": cfg["refresh_token"],
                    "client_id": cfg["client_id"],
                    "client_secret": cfg["client_secret"],
                },
                timeout=20,
            )
        except requests.RequestException as e:
            raise HTTPException(status_code=502, detail=f"Concur token request failed: {str(e)}")

        # Make auth failures obvious (wrong refresh token/client credentials/base URL)
        if resp.status_code in (400, 401, 403):
            raise HTTPException(
                status_code=502,
                detail=f"Concur OAuth rejected credentials (status {resp.status_code}). "
                       f"Check Key Vault secrets and Concur app configuration."
            )

        resp.raise_for_status()
        data = resp.json()

        self._access_token = data["access_token"]
        self._expires_at = now + float(data.get("expires_in", 1800))

        # Concur may rotate refresh_token. We deliberately do NOT write back to Key Vault from the app.
        # If you need rotation, do it via a controlled admin process / pipeline, not runtime code.

        return self._access_token
