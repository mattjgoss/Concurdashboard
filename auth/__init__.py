# auth/__init__.py
"""
Authentication modules for the Concur Accruals API.

- concur_oauth: OAuth 2.0 refresh token flow for Concur API access
- azure_ad: Azure AD JWT validation for SharePoint integration
"""

from .concur_oauth import ConcurOAuthClient
from .azure_ad import get_current_user, get_azure_ad_config_status, require_scope

__all__ = [
    "ConcurOAuthClient",
    "get_current_user",
    "get_azure_ad_config_status",
    "require_scope",
]
