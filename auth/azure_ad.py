# auth/azure_ad.py
"""
Azure AD JWT token validation for SharePoint SPFx integration.

This module validates JWT tokens sent by SharePoint's AadHttpClient.
Tokens are validated for:
- Signature (RSA with Azure AD public keys)
- Expiration
- Issuer (Azure AD tenant)
- Audience (this API's app ID)
- Optional: required scopes or roles
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional, Set
from functools import lru_cache
import os

import jwt
from jwt import PyJWKClient
from fastapi import HTTPException, Security, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

# ======================================================
# CONFIGURATION
# ======================================================

# Azure AD tenant ID (from environment or Key Vault)
AZURE_AD_TENANT_ID = os.getenv("AZURE_AD_TENANT_ID", "")

# This API's Application ID (the audience claim)
AZURE_AD_APP_ID = os.getenv("AZURE_AD_APP_ID", "")

# Optional: App ID URI (alternative audience format)
AZURE_AD_APP_ID_URI = os.getenv("AZURE_AD_APP_ID_URI", "")

# Acceptable audiences (app ID and/or app ID URI)
def get_valid_audiences() -> Set[str]:
    audiences = set()
    if AZURE_AD_APP_ID:
        audiences.add(AZURE_AD_APP_ID)
    if AZURE_AD_APP_ID_URI:
        audiences.add(AZURE_AD_APP_ID_URI)
    return audiences

# Azure AD issuer URLs (v1 and v2 token formats)
def get_valid_issuers() -> Set[str]:
    if not AZURE_AD_TENANT_ID:
        return set()
    return {
        f"https://sts.windows.net/{AZURE_AD_TENANT_ID}/",  # v1 tokens
        f"https://login.microsoftonline.com/{AZURE_AD_TENANT_ID}/v2.0",  # v2 tokens
    }

# JWKS endpoint (Microsoft signing keys)
JWKS_URL = f"https://login.microsoftonline.com/{AZURE_AD_TENANT_ID}/discovery/v2.0/keys"

# Enable/disable token validation (useful for local dev)
VALIDATE_AZURE_AD_TOKEN = os.getenv("VALIDATE_AZURE_AD_TOKEN", "true").lower() == "true"

# ======================================================
# JWT VALIDATION
# ======================================================

@lru_cache(maxsize=1)
def get_jwks_client() -> PyJWKClient:
    """
    Create a cached JWKS client for fetching Azure AD public keys.
    Keys are cached to avoid repeated network calls.
    """
    if not AZURE_AD_TENANT_ID:
        raise RuntimeError("AZURE_AD_TENANT_ID not configured")
    return PyJWKClient(JWKS_URL, cache_keys=True, max_cached_keys=16)


def validate_azure_ad_token(token: str) -> Dict:
    """
    Validate an Azure AD JWT token.
    
    Args:
        token: JWT token string from Authorization header
        
    Returns:
        Decoded token payload (dict with claims)
        
    Raises:
        HTTPException: If token is invalid, expired, or missing required claims
    """
    
    # 1. Configuration check
    valid_audiences = get_valid_audiences()
    valid_issuers = get_valid_issuers()
    
    if not valid_audiences:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "server_misconfiguration",
                "message": "Azure AD authentication not configured (AZURE_AD_APP_ID or AZURE_AD_APP_ID_URI required)",
            }
        )
    
    if not valid_issuers:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "server_misconfiguration",
                "message": "Azure AD tenant not configured (AZURE_AD_TENANT_ID required)",
            }
        )
    
    # 2. Decode token header to get signing key ID
    try:
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")
        if not kid:
            raise HTTPException(
                status_code=401,
                detail={"error": "invalid_token", "message": "Token missing 'kid' header"}
            )
    except jwt.DecodeError as e:
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid_token", "message": f"Malformed token: {str(e)}"}
        )
    
    # 3. Fetch signing key from Azure AD JWKS endpoint
    try:
        jwks_client = get_jwks_client()
        signing_key = jwks_client.get_signing_key(kid)
    except Exception as e:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_token",
                "message": f"Unable to fetch signing key: {str(e)}"
            }
        )
    
    # 4. Verify and decode token
    try:
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=list(valid_audiences),
            issuer=None,  # We'll validate manually to support both v1 and v2
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_aud": True,
                "require": ["exp", "iat", "aud"],
            }
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail={"error": "token_expired", "message": "Token has expired"}
        )
    except jwt.InvalidAudienceError:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_audience",
                "message": f"Token audience does not match. Expected one of: {valid_audiences}"
            }
        )
    except jwt.InvalidTokenError as e:
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid_token", "message": str(e)}
        )
    
    # 5. Validate issuer manually (support both v1 and v2 formats)
    token_issuer = payload.get("iss", "")
    if token_issuer not in valid_issuers:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_issuer",
                "message": f"Token issuer '{token_issuer}' not trusted. Expected one of: {valid_issuers}"
            }
        )
    
    # 6. Validate nbf (not before) if present
    nbf = payload.get("nbf")
    if nbf and time.time() < nbf:
        raise HTTPException(
            status_code=401,
            detail={"error": "token_not_yet_valid", "message": "Token not yet valid (nbf claim)"}
        )
    
    return payload


def validate_scopes(payload: Dict, required_scopes: Optional[List[str]] = None) -> None:
    """
    Validate that token contains required scopes.
    
    Args:
        payload: Decoded JWT payload
        required_scopes: List of required scope names (e.g., ["access_as_user"])
        
    Raises:
        HTTPException: If required scopes are missing
    """
    if not required_scopes:
        return
    
    # Scopes can be in 'scp' claim (delegated permissions) or 'roles' claim (app permissions)
    token_scopes = set()
    
    # Delegated permissions (user context)
    scp = payload.get("scp", "")
    if isinstance(scp, str):
        token_scopes.update(scp.split())
    
    # Application permissions (app context)
    roles = payload.get("roles", [])
    if isinstance(roles, list):
        token_scopes.update(roles)
    
    missing_scopes = set(required_scopes) - token_scopes
    if missing_scopes:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "insufficient_scope",
                "message": f"Token missing required scopes: {missing_scopes}",
                "required": required_scopes,
                "provided": list(token_scopes)
            }
        )


# ======================================================
# FASTAPI DEPENDENCIES
# ======================================================

# HTTP Bearer scheme for Swagger UI
bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme),
    x_request_id: Optional[str] = Header(None)
) -> Dict:
    """
    FastAPI dependency for validating Azure AD tokens.
    
    Usage:
        @app.get("/protected")
        def protected_endpoint(user: Dict = Depends(get_current_user)):
            return {"user": user["upn"]}
    
    Returns:
        Decoded token payload with claims (upn, oid, tid, etc.)
        
    Raises:
        HTTPException: 401 if token invalid, 403 if insufficient permissions
    """
    
    # Allow bypass if validation is disabled (local dev only)
    if not VALIDATE_AZURE_AD_TOKEN:
        return {
            "bypass": True,
            "message": "Azure AD validation disabled (VALIDATE_AZURE_AD_TOKEN=false)",
            "x-request-id": x_request_id
        }
    
    # Check for credentials
    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "missing_token",
                "message": "Authorization header required. Expected: Bearer <token>"
            },
            headers={"WWW-Authenticate": "Bearer"}
        )
    
    # Validate token
    payload = validate_azure_ad_token(credentials.credentials)
    
    # Optional: validate required scopes
    # validate_scopes(payload, required_scopes=["access_as_user"])
    
    # Add correlation ID from SharePoint request
    if x_request_id:
        payload["x-request-id"] = x_request_id
    
    return payload


def require_scope(*scopes: str):
    """
    FastAPI dependency factory for scope-based authorization.
    
    Usage:
        @app.get("/admin", dependencies=[Depends(require_scope("Admin.ReadWrite"))])
        def admin_endpoint():
            return {"status": "admin access granted"}
    """
    async def scope_checker(user: Dict = Security(get_current_user)):
        validate_scopes(user, required_scopes=list(scopes))
        return user
    return scope_checker


# ======================================================
# DIAGNOSTICS
# ======================================================

def get_azure_ad_config_status() -> Dict:
    """
    Return current Azure AD configuration status (for diagnostics).
    Does NOT return secret values.
    """
    return {
        "validation_enabled": VALIDATE_AZURE_AD_TOKEN,
        "tenant_id_configured": bool(AZURE_AD_TENANT_ID),
        "tenant_id": AZURE_AD_TENANT_ID if AZURE_AD_TENANT_ID else None,
        "app_id_configured": bool(AZURE_AD_APP_ID),
        "app_id_uri_configured": bool(AZURE_AD_APP_ID_URI),
        "valid_audiences": list(get_valid_audiences()),
        "valid_issuers": list(get_valid_issuers()) if AZURE_AD_TENANT_ID else [],
        "jwks_url": JWKS_URL if AZURE_AD_TENANT_ID else None,
    }
