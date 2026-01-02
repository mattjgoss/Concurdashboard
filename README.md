# Concur Users API - Technical Documentation.

**Version**: 4.0  
**Last Updated**: 2026-01-02  
**Purpose**: SharePoint-integrated Concur Identity user management system

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Architecture Overview](#architecture-overview)
3. [Authentication Deep Dive](#authentication-deep-dive)
4. [Azure Key Vault Integration](#azure-key-vault-integration)
5. [API Endpoints Reference](#api-endpoints-reference)
6. [Data Flow Diagrams](#data-flow-diagrams)
7. [Concur Identity API Integration](#concur-identity-api-integration)
8. [Error Handling Strategy](#error-handling-strategy)
9. [Configuration Management](#configuration-management)
10. [Deployment Guide](#deployment-guide)
11. [Troubleshooting](#troubleshooting)

---

## Executive Summary

This FastAPI microservice provides a **simplified, SharePoint-integrated interface** to SAP Concur's Identity API. The primary use case is enabling SharePoint users to browse and export Concur user data through a web part.

### Key Capabilities

- **User Listing**: Retrieve Concur users with SCIM v2.0 pagination
- **User Details**: Fetch full SCIM profile for individual users
- **Excel Export**: Generate Excel reports of user data
- **Tenant-Safe Attributes**: Automatically falls back when Concur extensions unavailable
- **Azure AD Authentication**: Secure SharePoint integration via JWT validation
- **Diagnostic Tools**: Health checks, auth tests, and config verification

### Technology Stack

| Component | Technology |
|-----------|------------|
| Framework | FastAPI 0.115.6 |
| Runtime | Python 3.11+ |
| Authentication | Azure AD JWT + Concur OAuth 2.0 |
| Secrets | Azure Key Vault (Managed Identity) |
| Deployment | Azure App Service |
| API Integration | SAP Concur Identity v4.1 (SCIM) |

---

## Architecture Overview

### High-Level System Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                   SharePoint Online                          │
│  ┌────────────────────────────────────────────────────────┐  │
│  │         SPFx Web Part (React)                         │  │
│  │  - User clicks "Load Users"                            │  │
│  │  - AadHttpClient auto-attaches Azure AD JWT           │  │
│  └──────────────────────┬─────────────────────────────────┘  │
└─────────────────────────┼─────────────────────────────────────┘
                          │ HTTPS Request
                          │ Authorization: Bearer <JWT>
                          │ x-request-id: <UUID>
                          ▼
┌─────────────────────────────────────────────────────────────┐
│              Azure App Service (Linux)                       │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  NGINX Reverse Proxy  │  ┌──────────────────────────┐  │  │
│  │  (:80 → :8000)        │  │  Gunicorn (4 workers)    │  │  │
│  └────────────────────────┘  │  ├─ Uvicorn Worker 1   │  │  │
│                              │  ├─ Uvicorn Worker 2   │  │  │
│  ┌────────────────────────┐  │  ├─ Uvicorn Worker 3   │  │  │
│  │  CORS Middleware       │  │  └─ Uvicorn Worker 4   │  │  │
│  │  (SharePoint origin)   │  └──────────────────────────┘  │  │
│  └──────────┬─────────────┘                                 │  │
│             ▼                                                │  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Azure AD JWT Validation (auth/azure_ad.py)           │  │
│  │  1. Extract Bearer token from header                   │  │
│  │  2. Decode JWT header → extract kid (key ID)          │  │
│  │  3. Fetch Microsoft JWKS public keys                   │  │
│  │  4. Verify RS256 signature                             │  │
│  │  5. Validate: exp, aud, iss, nbf claims               │  │
│  │  6. Return decoded payload (upn, oid, name, etc.)     │  │
│  └──────────┬─────────────────────────────────────────────┘  │
│             ▼                                                │  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  API Route Handler (/api/users)                        │  │
│  │  - Receives: current_user dict from dependency         │  │
│  │  - Calls: list_users_tenant_safe()                     │  │
│  └──────────┬─────────────────────────────────────────────┘  │
│             ▼                                                │  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Concur OAuth Client (Singleton)                       │  │
│  │  - Lazy initialization on first API call               │  │
│  │  - Credentials from Key Vault or env vars             │  │
│  │  - Token caching (30min with 60s buffer)              │  │
│  │  - Automatic refresh on expiration                     │  │
│  └──────────┬─────────────────────────────────────────────┘  │
│             ▼                                                │  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Concur Identity API Client                            │  │
│  │  - Pagination: startIndex/count                        │  │
│  │  - Tenant-safe attributes (fallback if 400)           │  │
│  │  - Error enrichment (context + truncated response)     │  │
│  └──────────┬─────────────────────────────────────────────┘  │
└─────────────┼─────────────────────────────────────────────────┘
              │ Outbound HTTPS
              ▼
┌─────────────────────────────────────────────────────────────┐
│           Azure Key Vault (via Managed Identity)             │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Secrets:                                              │  │
│  │  - concur-api-base-url                                 │  │
│  │  - concur-token-url                                    │  │
│  │  - concur-client-id                                    │  │
│  │  - concur-client-secret                                │  │
│  │  - concur-refresh-token                                │  │
│  │                                                         │  │
│  │  Access:                                               │  │
│  │  - App Service Managed Identity (system-assigned)      │  │
│  │  - Permissions: Get, List (secret operations)          │  │
│  │  - Caching: 5 minutes in-memory per secret            │  │
│  └────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────┐
│              SAP Concur APIs                                 │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  OAuth 2.0 Endpoint:                                   │  │
│  │  POST /oauth2/v0/token                                 │  │
│  │  - grant_type: refresh_token                           │  │
│  │  - Responds: access_token (30min TTL)                  │  │
│  └────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Identity v4.1 (SCIM 2.0):                            │  │
│  │  GET /profile/identity/v4.1/Users                      │  │
│  │  - Pagination: startIndex, count                       │  │
│  │  - Filter support: userName, emails, enterprise ext    │  │
│  │  - Returns: SCIM Resources array                       │  │
│  └────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

### Component Responsibilities

| Component | Responsibility | State Management |
|-----------|---------------|------------------|
| **CORS Middleware** | Origin validation, preflight handling | Stateless |
| **Azure AD JWT Validator** | Token signature/claims verification | Stateless (JWKS cache in `auth/azure_ad.py`) |
| **OAuth Client Singleton** | Concur access token lifecycle | Process-scoped (per Gunicorn worker) |
| **Key Vault Client** | Secret retrieval with caching | Process-scoped (5min cache) |
| **Identity API Client** | SCIM user queries, pagination | Stateless |

---

## Authentication Deep Dive

### Two-Layer Authentication Architecture

This application uses **dual authentication**:
1. **Client Authentication** (SharePoint → API): Azure AD JWT
2. **Service Authentication** (API → Concur): OAuth 2.0 Refresh Token

```
┌──────────────────────────────────────────────────────────────┐
│  Authentication Layer 1: SharePoint User → API               │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  SharePoint User Identity                              │  │
│  │  ├─ Azure AD UPN: user@contoso.com                     │  │
│  │  ├─ Object ID: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx   │  │
│  │  └─ Groups/Roles: [SharePoint Members, ...]           │  │
│  └──────────────────────┬─────────────────────────────────┘  │
│                         │                                    │
│                         │ SPFx calls AadHttpClient           │
│                         ▼                                    │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Azure AD Token Service (Microsoft)                    │  │
│  │  1. Authenticates user                                 │  │
│  │  2. Issues JWT for API (audience = API app ID)         │  │
│  │  3. Signs with RS256 (private key)                     │  │
│  │  4. Includes claims: upn, oid, scp, aud, iss, exp      │  │
│  └──────────────────────┬─────────────────────────────────┘  │
│                         │                                    │
│                         │ Token embedded in Authorization    │
│                         ▼                                    │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  API Receives Request                                  │  │
│  │  Authorization: Bearer eyJ0eXAiOiJKV1QiLCJhbGciOiJ...  │  │
│  └──────────────────────┬─────────────────────────────────┘  │
│                         │                                    │
│                         │ Dependency injection triggers      │
│                         ▼                                    │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  get_current_user() Dependency (auth/azure_ad.py)     │  │
│  │  ┌──────────────────────────────────────────────────┐  │  │
│  │  │  Step 1: Extract token                           │  │  │
│  │  │  - Parse Authorization header                      │  │  │
│  │  │  - Validate format: "Bearer <token>"              │  │  │
│  │  └──────────────────────────────────────────────────┘  │  │
│  │  ┌──────────────────────────────────────────────────┐  │  │
│  │  │  Step 2: Decode JWT header (no verification)     │  │  │
│  │  │  - Extract kid (key ID)                           │  │  │
│  │  │  - Extract alg (must be RS256)                     │  │  │
│  │  └──────────────────────────────────────────────────┘  │  │
│  │  ┌──────────────────────────────────────────────────┐  │  │
│  │  │  Step 3: Fetch signing key                        │  │  │
│  │  │  - URL: login.microsoftonline.com/{tenant}/       │  │  │
│  │  │         discovery/v2.0/keys                       │  │  │
│  │  │  - Find key matching kid                          │  │  │
│  │  │  - Cache keys (PyJWKClient @lru_cache)            │  │  │
│  │  └──────────────────────────────────────────────────┘  │  │
│  │  ┌──────────────────────────────────────────────────┐  │  │
│  │  │  Step 4: Verify signature                         │  │  │
│  │  │  - Use public key from JWKS                       │  │  │
│  │  │  - Verify RS256 signature                         │  │  │
│  │  │  - Raises JWTError if invalid                      │  │  │
│  │  └──────────────────────────────────────────────────┘  │  │
│  │  ┌──────────────────────────────────────────────────┐  │  │
│  │  │  Step 5: Validate claims                          │  │  │
│  │  │  - exp: Token not expired                         │  │  │
│  │  │  - nbf: Token valid (not before)                   │  │  │
│  │  │  - aud: Matches AZURE_AD_APP_ID or APP_ID_URI     │  │  │
│  │  │  - iss: From correct Azure AD tenant              │  │  │
│  │  └──────────────────────────────────────────────────┘  │  │
│  │  ┌──────────────────────────────────────────────────┐  │  │
│  │  │  Step 6: Return user payload                      │  │  │
│  │  │  {                                                │  │  │
│  │  │    "upn": "user@contoso.com",                     │  │  │
│  │  │    "oid": "uuid",                                 │  │  │
│  │  │    "name": "John Smith",                          │  │  │
│  │  │    "scp": "access_as_user",                       │  │  │
│  │  │    "aud": "api://concur-users-api",               │  │  │
│  │  │    "iss": "https://sts.windows.net/{tenant}/",    │  │  │
│  │  │    "exp": 1735689600,                             │  │  │
│  │  │    "x-request-id": "uuid-from-header"             │  │  │
│  │  │  }                                                 │  │  │
│  │  └──────────────────────────────────────────────────┘  │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│  Authentication Layer 2: API → Concur                        │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  OAuth Client Initialization (First API Call)          │  │
│  │  1. Load credentials from Key Vault:                   │  │
│  │     - concur-token-url                                 │  │
│  │     - concur-client-id                                 │  │
│  │     - concur-client-secret                             │  │
│  │     - concur-refresh-token                             │  │
│  │  2. Instantiate ConcurOAuthClient(...)                │  │
│  │  3. Store in global _oauth_client variable            │  │
│  └──────────────────────┬─────────────────────────────────┘  │
│                         │                                    │
│                         │ On each Concur API call            │
│                         ▼                                    │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  get_access_token() Logic                              │  │
│  │  ┌──────────────────────────────────────────────────┐  │  │
│  │  │  Check: Is cached token still valid?             │  │  │
│  │  │  - now < expires_at - 60 seconds                  │  │  │
│  │  │  - If YES: return cached token                    │  │  │
│  │  │  - If NO: proceed to refresh                      │  │  │
│  │  └──────────────────────────────────────────────────┘  │  │
│  │  ┌──────────────────────────────────────────────────┐  │  │
│  │  │  Refresh Token Flow                               │  │  │
│  │  │  POST {token_url}                                 │  │  │
│  │  │  Body:                                            │  │  │
│  │  │    grant_type: "refresh_token"                    │  │  │
│  │  │    refresh_token: {current_refresh_token}         │  │  │
│  │  │    client_id: {client_id}                         │  │  │
│  │  │    client_secret: {client_secret}                 │  │  │
│  │  └──────────────────────────────────────────────────┘  │  │
│  │  ┌──────────────────────────────────────────────────┐  │  │
│  │  │  Response:                                        │  │  │
│  │  │  {                                                │  │  │
│  │  │    "access_token": "...",                         │  │  │
│  │  │    "token_type": "Bearer",                        │  │  │
│  │  │    "expires_in": 1800,  // 30 minutes            │  │  │
│  │  │    "refresh_token": "..." // may rotate          │  │  │
│  │  │  }                                                 │  │  │
│  │  └──────────────────────────────────────────────────┘  │  │
│  │  ┌──────────────────────────────────────────────────┐  │  │
│  │  │  Cache Update                                     │  │  │
│  │  │  - self._access_token = response["access_token"]  │  │  │
│  │  │  - self._expires_at = now + expires_in           │  │  │
│  │  │  - If new refresh_token: update self.refresh_token│  │  │
│  │  └──────────────────────────────────────────────────┘  │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

### JWT Token Claims Breakdown

**Standard Claims**:
- `aud` (audience): API application ID or Application ID URI
- `iss` (issuer): `https://sts.windows.net/{tenant-id}/` or `https://login.microsoftonline.com/{tenant-id}/v2.0`
- `exp` (expiration): Unix timestamp (typically `now + 1 hour`)
- `nbf` (not before): Unix timestamp
- `iat` (issued at): Unix timestamp

**User Identity Claims**:
- `upn` (user principal name): Primary email/login
- `unique_name`: Alternative identity claim
- `preferred_username`: Another identity variant
- `oid` (object ID): Permanent user identifier in Azure AD
- `name`: Display name

**Application Claims**:
- `scp` (scopes): Space-separated list (e.g., `"access_as_user"`)
- `app_displayname`: Client application name
- `appid`: Client application ID
- `x-request-id`: Custom header (correlation ID from SharePoint)

---

## Azure Key Vault Integration

### Managed Identity Authentication

```
┌──────────────────────────────────────────────────────────────┐
│  Azure App Service Managed Identity Flow                     │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  App Service Configuration                             │  │
│  │  - System-Assigned Managed Identity: Enabled           │  │
│  │  - Principal ID: auto-generated by Azure               │  │
│  │  - No credentials needed in code                       │  │
│  └──────────────────────┬─────────────────────────────────┘  │
│                         │                                    │
│                         │ App requests secret                │
│                         ▼                                    │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  DefaultAzureCredential Chain (azure-identity)         │  │
│  │  Tries in order:                                       │  │
│  │  1. EnvironmentCredential (env vars) - Skip           │  │
│  │  2. ManagedIdentityCredential - Success!              │  │
│  │     - Queries: http://169.254.169.254/metadata/...     │  │
│  │     - Returns: access token for Key Vault             │  │
│  │  3. AzureCliCredential - Not tried                     │  │
│  │  4. ... other credential types                         │  │
│  └──────────────────────┬─────────────────────────────────┘  │
│                         │                                    │
│                         │ Token attached to request          │
│                         ▼                                    │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Azure Key Vault API                                   │  │
│  │  GET https://{vault}.vault.azure.net/secrets/{name}    │  │
│  │  Headers:                                              │  │
│  │    Authorization: Bearer {managed-identity-token}      │  │
│  └──────────────────────┬─────────────────────────────────┘  │
│                         │                                    │
│                         │ Validates identity via AAD         │
│                         │ Checks access policy               │
│                         ▼                                    │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Key Vault Access Policy Check                         │  │
│  │  - Principal: {app-service-principal-id}               │  │
│  │  - Permissions: Get, List (secrets)                    │  │
│  │  - If authorized: return secret value                  │  │
│  │  - If denied: 403 Forbidden                            │  │
│  └──────────────────────┬─────────────────────────────────┘  │
│                         │                                    │
│                         │ Secret value returned              │
│                         ▼                                    │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  In-Memory Cache (services/identity_service.py)        │  │
│  │  _SECRET_CACHE = {                                     │  │
│  │    "concur-client-id": {                               │  │
│  │      "value": "abc123...",                             │  │
│  │      "ts": 1735689000.0  // 5min TTL                  │  │
│  │    }                                                    │  │
│  │  }                                                      │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

### Secret Lifecycle

**Initial Fetch** (Cold Start):
1. App starts → no secrets cached
2. First API call triggers `get_oauth_client()`
3. Calls `kv("concur-client-id")` → `get_secret("concur-client-id")`
4. Managed Identity auth → Key Vault GET
5. Cache entry created with timestamp
6. Secret returned to caller

**Subsequent Fetches** (Warm):
1. API call triggers `kv("concur-client-id")`
2. Check cache: `now - cached["ts"] < 300 seconds`
3. If valid: return `cached["value"]`
4. If expired: Fetch from Key Vault again

**Fallback Strategy**:
```python
def kv(name: str, fallback: Optional[str] = None) -> Optional[str]:
    try:
        return get_secret(name)  # Try Key Vault
    except Exception:
        return fallback  # Fall back to env var or None
```

**Why This Matters**:
- **Local Development**: Key Vault unavailable → uses env vars
- **Production**: Key Vault available → uses secure storage
- **Resilience**: Temporary Key Vault outage → cached values continue working
- **Performance**: Reduces Key Vault API calls (~50-100ms each)

---

## API Endpoints Reference

### Endpoint Map

```
GET  /build                    ← Deployment fingerprint
GET  /kv-test                  ← Key Vault diagnostics
GET  /auth/config-status       ← Azure AD config check
GET  /api/whoami               ← Current user info (requires auth)
GET  /api/concur/auth-test     ← Concur OAuth test
GET  /api/users                ← List users (requires auth)
GET  /api/users/{user_id}      ← User detail (requires auth)
GET  /api/users/export         ← Excel export (requires auth)
```

### 1. GET /api/users

**Purpose**: Retrieve paginated list of Concur users for SharePoint UI grid

**Authentication**: Required (Azure AD JWT)

**Query Parameters**:
- `take`: Number of users to return (default: 500, range: 1-5000)

**Request Example**:
```http
GET /api/users?take=100 HTTP/1.1
Host: concur-users-api.azurewebsites.net
Authorization: Bearer eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiIs...
x-request-id: 550e8400-e29b-41d4-a716-446655440000
```

**Response Example**:
```json
{
  "meta": {
    "requestedBy": "john.smith@contoso.com",
    "returned": 100,
    "concurBaseUrl": "https://us2.api.concursolutions.com",
    "attributeMode": "with_concur_extension"
  },
  "users": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "displayName": "John Smith",
      "userName": "john.smith@contoso.com",
      "email": "john.smith@contoso.com",
      "active": true,
      "employeeNumber": "EMP001"
    }
  ]
}
```

**Implementation Flow**:
```
1. FastAPI dependency injection calls get_current_user()
   ├─ Validates Azure AD JWT
   └─ Returns current_user dict

2. Call list_users_tenant_safe(take=take)
   ├─ Try: _identity_list_users_paged(ATTRS_WITH_CONCUR_EXT)
   │   ├─ If success: return (users, "with_concur_extension")
   │   └─ If 400 "Unrecognized attributes": catch and retry
   └─ Retry: _identity_list_users_paged(ATTRS_NO_CONCUR_EXT)
       └─ Return (users, "no_concur_extension")

3. _identity_list_users_paged() pagination loop:
   ├─ page = 0, startIndex = 1
   ├─ While page < max_pages (200):
   │   ├─ GET /profile/identity/v4.1/Users
   │   │   ?attributes={attrs}
   │   │   &startIndex={startIndex}
   │   │   &count=200
   │   ├─ Append Resources to all_users[]
   │   ├─ startIndex += itemsPerPage
   │   ├─ Break if: no resources, startIndex > totalResults, or len(resources) < count
   │   └─ page++
   └─ Return all_users

4. Transform users: _to_grid_row_identity(user) for each
   ├─ Extract enterprise extension attributes
   ├─ Extract primary email from emails array
   └─ Return flattened dict

5. Build response with meta + users array
```

### 2. GET /api/users/{user_id}

**Purpose**: Fetch complete SCIM profile for a single user

**Authentication**: Required (Azure AD JWT)

**Path Parameters**:
- `user_id`: Concur user UUID

**Request Example**:
```http
GET /api/users/550e8400-e29b-41d4-a716-446655440000 HTTP/1.1
Host: concur-users-api.azurewebsites.net
Authorization: Bearer eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiIs...
```

**Response Example** (SCIM v2.0 User Resource):
```json
{
  "schemas": [
    "urn:ietf:params:scim:schemas:core:2.0:User",
    "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User",
    "urn:ietf:params:scim:schemas:extension:concur:2.0:User"
  ],
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "userName": "john.smith@contoso.com",
  "displayName": "John Smith",
  "active": true,
  "emails": [
    {
      "value": "john.smith@contoso.com",
      "type": "work",
      "primary": true
    }
  ],
  "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User": {
    "employeeNumber": "EMP001",
    "organization": "Contoso Corp",
    "division": "Engineering",
    "department": "Platform"
  },
  "urn:ietf:params:scim:schemas:extension:concur:2.0:User": {
    "orgUnit1": "Engineering",
    "orgUnit2": "Platform",
    "custom21": "CC-1234",
    "reimbursementCurrency": "USD",
    "ledgerCode": "DEFAULT"
  }
}
```

**Error Handling**:
```json
// 400 Bad Request (missing user_id)
{
  "detail": {
    "error": "missing_user_id",
    "message": "user_id is required"
  }
}

// 502 Bad Gateway (Concur error)
{
  "detail": {
    "where": "user_detail",
    "error": "concur_error",
    "concur_status": 404,
    "url": "https://us2.api.concursolutions.com/profile/identity/v4.1/Users/xxx",
    "base_url": "https://us2.api.concursolutions.com",
    "response": "{\"errors\":[{\"errorCode\":\"invalidId\",\"errorMessage\":\"User not found\"}]}"
  }
}
```

### 3. GET /api/users/export

**Purpose**: Generate Excel file with user data

**Authentication**: Required (Azure AD JWT)

**Query Parameters**:
- `take`: Number of users to include (default: 1000, range: 1-5000)

**Response**:
- **Content-Type**: `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`
- **Content-Disposition**: `attachment; filename="Concur_Users_20260102_0915.xlsx"`

**Excel Structure**:
- **Sheet 1**: "unassigned card transactions" (empty)
- **Sheet 2**: "unsubnitted reports" (empty)
- **Sheet 3**: "Users" (if `extra_sheets` parameter supported by `export_accruals_to_excel`)

### 4. GET /api/whoami

**Purpose**: Debug endpoint to inspect decoded JWT claims

**Authentication**: Required (Azure AD JWT)

**Response**: Raw decoded JWT payload
```json
{
  "aud": "api://concur-users-api",
  "iss": "https://sts.windows.net/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx/",
  "iat": 1735689000,
  "nbf": 1735689000,
  "exp": 1735692600,
  "upn": "john.smith@contoso.com",
  "oid": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "name": "John Smith",
  "scp": "access_as_user",
  "x-request-id": "550e8400-e29b-41d4-a716-446655440000"
}
```

---

## Data Flow Diagrams

### Complete Request Lifecycle

```
┌────────────────────────────────────────────────────────────┐
│ PHASE 1: Request Initiation (SharePoint Browser)          │
└────────────────────────────────────────────────────────────┘

User clicks "Load Users" button in SharePoint web part
           │
           ▼
SPFx component calls API:
  const client = await context.aadHttpClientFactory
    .getClient('api://concur-users-api');
  const response = await client.get(
    'https://concur-users-api.azurewebsites.net/api/users?take=500',
    AadHttpClient.configurations.v1
  );
           │
           ├─ AadHttpClient automatically:
           │  ├─ Gets Azure AD token for current user
           │  ├─ Sets Authorization: Bearer <token>
           │  └─ Sets x-request-id: <uuid>
           │
           ▼
HTTP Request sent:
  GET /api/users?take=500 HTTP/1.1
  Host: concur-users-api.azurewebsites.net
  Authorization: Bearer eyJ0eXAiOi...
  x-request-id: 550e8400-e29b-41d4-a716-446655440000
  Origin: https://contoso.sharepoint.com

┌────────────────────────────────────────────────────────────┐
│ PHASE 2: Azure App Service Processing                     │
└────────────────────────────────────────────────────────────┘

Request hits NGINX (:80)
           │
           ▼
NGINX forwards to Gunicorn (:8000)
           │
           ▼
Gunicorn worker picks up request
           │
           ▼
Uvicorn ASGI server handles request
           │
           ▼
FastAPI application receives request
           │
           ▼
CORS Middleware executes:
  ├─ Check: Origin in allowed_origins?
  ├─ If preflight (OPTIONS): return CORS headers
  └─ If actual request: add CORS headers to response
           │
           ▼
Route matching: /api/users found
           │
           ▼
Dependency resolution: Depends(get_current_user)
           │
           ▼
get_current_user() executes:
  ├─ Extract Authorization header
  ├─ Parse bearer scheme
  ├─ Decode JWT (no verification)
  ├─ Extract kid from header
  ├─ Fetch Microsoft public key (JWKS)
  ├─ Verify RS256 signature
  ├─ Validate exp, nbf, aud, iss
  ├─ Extract x-request-id from header
  └─ Return decoded payload
           │
           ▼
api_users_list() function executes:
  ├─ Parameter: take=500
  ├─ Parameter: current_user={...decoded JWT...}
  └─ Calls list_users_tenant_safe(500)

┌────────────────────────────────────────────────────────────┐
│ PHASE 3: Concur API Integration                           │
└────────────────────────────────────────────────────────────┘

list_users_tenant_safe(500) executes:
           │
           ▼
Try with Concur extension attributes:
  _identity_list_users_paged(
    attributes=ATTRS_WITH_CONCUR_EXT,
    count=200,
    max_pages=200
  )
           │
           ├─ If HTTPException with 400 "Unrecognized attributes":
           │  └─ Retry with ATTRS_NO_CONCUR_EXT
           │
           ▼
_identity_list_users_paged() pagination loop:
           │
           ▼
Initialize: page=0, startIndex=1, all_users=[]
           │
           ▼
While page < 200:
  ├─ Build params:
  │  {
  │    "attributes": "id,userName,displayName,...",
  │    "startIndex": startIndex,
  │    "count": 200
  │  }
  │
  ├─ Call concur_headers():
  │  ├─ Call get_oauth_client() (lazy singleton init)
  │  │  ├─ If _oauth_client exists: return cached
  │  │  └─ If not exists:
  │  │     ├─ Load credentials from Key Vault:
  │  │     │  ├─ kv("concur-token-url")
  │  │     │  │  ├─ Try: get_secret("concur-token-url")
  │  │     │  │  │  ├─ Check cache: now - ts < 300?
  │  │     │  │  │  │  ├─ If yes: return cached value
  │  │     │  │  │  │  └─ If no: fetch from Key Vault
  │  │     │  │  │  │     ├─ DefaultAzureCredential auth
  │  │     │  │  │  │     ├─ SecretClient.get_secret()
  │  │     │  │  │  │     ├─ Update cache: {value, ts}
  │  │     │  │  │  │     └─ Return value
  │  │     │  │  └─ Catch: return env("CONCUR_TOKEN_URL")
  │  │     │  ├─ kv("concur-client-id")
  │  │     │  ├─ kv("concur-client-secret")
  │  │     │  └─ kv("concur-refresh-token")
  │  │     ├─ Validate all present
  │  │     ├─ Instantiate: ConcurOAuthClient(...)
  │  │     └─ Store in _oauth_client
  │  │
  │  ├─ Call oauth.get_access_token():
  │  │  ├─ Check: now < expires_at - 60?
  │  │  │  ├─ If yes: return cached access_token
  │  │  │  └─ If no: refresh
  │  │  ├─ Refresh flow:
  │  │  │  ├─ POST to token_url
  │  │  │  │  Body: {
  │  │  │  │    grant_type: "refresh_token",
  │  │  │  │    refresh_token: current_refresh_token,
  │  │  │  │    client_id: client_id,
  │  │  │  │    client_secret: client_secret
  │  │  │  │  }
  │  │  │  ├─ Response: {access_token, expires_in, refresh_token?}
  │  │  │  ├─ Update: _access_token = access_token
  │  │  │  ├─ Update: _expires_at = now + expires_in
  │  │  │  └─ If new refresh_token: update refresh_token
  │  │  └─ Return access_token
  │  │
  │  └─ Return: {"Authorization": "Bearer <token>", "Accept": "application/json"}
  │
  ├─ Make request:
  │  GET https://us2.api.concursolutions.com/profile/identity/v4.1/Users
  │    ?attributes=id,userName,...
  │    &startIndex=1
  │    &count=200
  │  Headers: {Authorization: Bearer <concur-token>, Accept: application/json}
  │
  ├─ Handle response:
  │  ├─ If network error: raise HTTPException 502 with details
  │  ├─ If not resp.ok: raise HTTPException 502 with Concur error
  │  └─ If ok: parse JSON
  │
  ├─ Extract payload.Resources
  │  ├─ If empty or not list: break
  │  └─ Append to all_users
  │
  ├─ Update pagination:
  │  ├─ totalResults = payload.totalResults
  │  ├─ itemsPerPage = payload.itemsPerPage || len(resources)
  │  ├─ startIndex += itemsPerPage
  │  └─ page++
  │
  ├─ Break conditions:
  │  ├─ startIndex > totalResults
  │  ├─ len(resources) < count
  │  └─ page >= max_pages
  │
  └─ Loop continues...

Return all_users (up to max_pages * count)

┌────────────────────────────────────────────────────────────┐
│ PHASE 4: Response Formatting                              │
└────────────────────────────────────────────────────────────┘

list_users_tenant_safe() returns:
  (users, "with_concur_extension")  or
  (users, "no_concur_extension")
           │
           ▼
Slice users[:take] → users[:500]
           │
           ▼
Transform each user:
  _to_grid_row_identity(user)
  ├─ Extract enterprise extension
  ├─ Extract primary email from emails array
  └─ Return: {
       id, displayName, userName,
       email, active, employeeNumber
     }
           │
           ▼
Build response dict:
  {
    "meta": {
      "requestedBy": current_user.upn,
      "returned": len(users),
      "concurBaseUrl": concur_base_url(),
      "attributeMode": mode
    },
    "users": [transformed_users]
  }
           │
           ▼
FastAPI serializes to JSON
           │
           ▼
CORS headers added
           │
           ▼
Uvicorn sends HTTP response
           │
           ▼
Gunicorn forwards to NGINX
           │
           ▼
NGINX sends to client

┌────────────────────────────────────────────────────────────┐
│ PHASE 5: SharePoint UI Update                             │
└────────────────────────────────────────────────────────────┘

Browser receives response:
  HTTP/1.1 200 OK
  Content-Type: application/json
  Access-Control-Allow-Origin: https://contoso.sharepoint.com
  
  {
    "meta": {...},
    "users": [...]
  }
           │
           ▼
SPFx web part parses JSON
           │
           ▼
Updates React state:
  setState({
    users: response.users,
    loading: false,
    metadata: response.meta
  })
           │
           ▼
React re-renders DetailsList component
           │
           ▼
User sees grid populated with users
```

---

## Concur Identity API Integration

### SCIM 2.0 Protocol

The Concur Identity v4.1 API implements **SCIM 2.0** (System for Cross-domain Identity Management).

**Key Characteristics**:
- **RESTful**: Standard HTTP verbs (GET, POST, PATCH, DELETE)
- **JSON**: Request/response bodies
- **Pagination**: startIndex/count (1-indexed)
- **Filtering**: SCIM filter expressions (not used in current implementation)
- **Attributes**: Selective field retrieval via `?attributes=` parameter

### Pagination Strategy

```python
# Current implementation: Simple pagination without filter
GET /profile/identity/v4.1/Users
  ?attributes=id,userName,displayName,active,emails.value,
               urn:ietf:params:scim:schemas:extension:enterprise:2.0:User,
               urn:ietf:params:scim:schemas:extension:concur:2.0:User
  &startIndex=1
  &count=200

# Response structure
{
  "totalResults": 5432,        // Total users in system
  "itemsPerPage": 200,         // Users in this response
  "startIndex": 1,             // Current page start
  "Resources": [               // Array of User objects
    {
      "id": "...",
      "userName": "...",
      ...
    }
  ]
}
```

**Pagination Loop Logic**:
```python
startIndex = 1
page = 0
all_users = []

while page < max_pages:
    params = {"startIndex": startIndex, "count": 200}
    response = GET /Users with params
    
    resources = response["Resources"]
    if not resources:
        break  # No more data
    
    all_users.extend(resources)
    
    itemsPerPage = response["itemsPerPage"] or len(resources)
    startIndex += itemsPerPage
    page += 1
    
    # Stop conditions
    if startIndex > response["totalResults"]:
        break
    if len(resources) < count:
        break  # Last page (partial)

return all_users
```

### Tenant-Safe Attribute Handling

**Problem**: Some Concur tenants don't support the Concur extension schema, resulting in 400 errors.

**Solution**: Try-catch with fallback

```python
# Attempt 1: With Concur extension
ATTRS_WITH_CONCUR_EXT = (
    "id,userName,displayName,active,emails.value,"
    "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User,"
    "urn:ietf:params:scim:schemas:extension:concur:2.0:User"
)

try:
    users = _identity_list_users_paged(attributes=ATTRS_WITH_CONCUR_EXT)
    return (users, "with_concur_extension")
except HTTPException as he:
    if is_unrecognized_attributes_error(he):
        # Attempt 2: Without Concur extension
        ATTRS_NO_CONCUR_EXT = (
            "id,userName,displayName,active,emails.value,"
            "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
        )
        users = _identity_list_users_paged(attributes=ATTRS_NO_CONCUR_EXT)
        return (users, "no_concur_extension")
    raise  # Other errors propagate
```

**Why This Matters**:
- **Compatibility**: Works with all Concur tenant configurations
- **Graceful Degradation**: Returns what's available
- **Transparency**: `attributeMode` in response tells client what was returned

---

## Error Handling Strategy

### Error Enrichment Philosophy

All errors include **contextual metadata** to aid debugging:

```python
raise HTTPException(
    status_code=502,
    detail={
        "where": "identity_list_users_paged",     # Function name
        "error": "concur_error",                   # Error category
        "concur_status": resp.status_code,         # Concur HTTP status
        "url": url,                                # Request URL
        "params": params,                          # Query parameters
        "base_url": base,                          # Concur base URL
        "response": (resp.text or "")[:2000],      # Truncated response
    }
)
```

### Error Categories

| HTTP Status | Category | Meaning |
|-------------|----------|---------|
| 400 | Client Error | Invalid request from SharePoint |
| 401 | Unauthorized | Azure AD JWT invalid/missing |
| 403 | Forbidden | Valid JWT but insufficient permissions |
| 404 | Not Found | User ID doesn't exist in Concur |
| 500 | Server Error | Application bug or misconfiguration |
| 502 | Bad Gateway | Concur API error or timeout |

### Example Error Responses

**401 - Missing/Invalid JWT**:
```json
{
  "detail": {
    "error": "missing_token",
    "message": "No Authorization header found"
  }
}
```

**502 - Concur API Error**:
```json
{
  "detail": {
    "where": "identity_list_users_paged",
    "error": "concur_error",
    "concur_status": 401,
    "url": "https://us2.api.concursolutions.com/profile/identity/v4.1/Users",
    "params": {"attributes": "...", "startIndex": 1, "count": 200},
    "base_url": "https://us2.api.concursolutions.com",
    "response": "{\"error\":\"unauthorized\",\"error_description\":\"Invalid access token\"}"
  }
}
```

---

## Configuration Management

### Configuration Hierarchy

```
1. Azure Key Vault (Production)
   └─ authenticate: Managed Identity
   └─ cache: 5 minutes
   └─ secrets: concur-*

2. Environment Variables (Fallback/Local Dev)
   └─ set: Azure App Service → Configuration → Application Settings
   └─ examples: CONCUR_TOKEN_URL, CONCUR_CLIENT_ID

3. Hardcoded Defaults (Last Resort)
   └─ concur_base_url() → "https://www.concursolutions.com"
```

### Required Configuration

| Variable | Source Priority | Example | Required |
|----------|----------------|---------|----------|
| `KEYVAULT_NAME` | Env only | `concur-kv` | Production |
| `AZURE_AD_TENANT_ID` | Env only | `xxx-xxx-xxx-xxx` | Yes |
| `AZURE_AD_APP_ID` | Env only | `xxx-xxx-xxx-xxx` | Yes |
| `AZURE_AD_APP_ID_URI` | Env only | `api://concur-users-api` | Yes |
| `VALIDATE_AZURE_AD_TOKEN` | Env only | `true` | Yes (prod) |
| `SP_ORIGIN` | Env only | `https://contoso.sharepoint.com` | Recommended |
| `concur-token-url` | KV → Env | `https://us2.api.concursolutions.com/oauth2/v0/token` | Yes |
| `concur-client-id` | KV → Env | `abc123...` | Yes |
| `concur-client-secret` | KV → Env | `secret456...` | Yes |
| `concur-refresh-token` | KV → Env | `refresh789...` | Yes |
| `concur-api-base-url` | KV → Env → Default | `https://us2.api.concursolutions.com` | No |

---

## Deployment Guide

### Azure App Service Setup

```bash
# Variables
RG="concur-users-rg"
APP_NAME="concur-users-api"
LOCATION="australiaeast"
KV_NAME="concur-kv"
SP_ORIGIN="https://contoso.sharepoint.com"

# 1. Create Resource Group
az group create --name $RG --location $LOCATION

# 2. Create Key Vault
az keyvault create \
  --name $KV_NAME \
  --resource-group $RG \
  --location $LOCATION \
  --sku standard

# 3. Add secrets to Key Vault
az keyvault secret set --vault-name $KV_NAME --name concur-token-url --value "https://us2.api.concursolutions.com/oauth2/v0/token"
az keyvault secret set --vault-name $KV_NAME --name concur-client-id --value "your-client-id"
az keyvault secret set --vault-name $KV_NAME --name concur-client-secret --value "your-client-secret"
az keyvault secret set --vault-name $KV_NAME --name concur-refresh-token --value "your-refresh-token"
az keyvault secret set --vault-name $KV_NAME --name concur-api-base-url --value "https://us2.api.concursolutions.com"

# 4. Create App Service Plan (Linux)
az appservice plan create \
  --name "${APP_NAME}-plan" \
  --resource-group $RG \
  --location $LOCATION \
  --sku B1 \
  --is-linux

# 5. Create Web App
az webapp create \
  --name $APP_NAME \
  --resource-group $RG \
  --plan "${APP_NAME}-plan" \
  --runtime "PYTHON:3.11"

# 6. Enable Managed Identity
az webapp identity assign \
  --name $APP_NAME \
  --resource-group $RG

# Get principal ID
PRINCIPAL_ID=$(az webapp identity show \
  --name $APP_NAME \
  --resource-group $RG \
  --query principalId -o tsv)

# 7. Grant Key Vault access to Managed Identity
az keyvault set-policy \
  --name $KV_NAME \
  --object-id $PRINCIPAL_ID \
  --secret-permissions get list

# 8. Configure App Settings
az webapp config appsettings set \
  --name $APP_NAME \
  --resource-group $RG \
  --settings \
    KEYVAULT_NAME=$KV_NAME \
    AZURE_AD_TENANT_ID="<your-tenant-id>" \
    AZURE_AD_APP_ID="<api-app-id>" \
    AZURE_AD_APP_ID_URI="api://concur-users-api" \
    VALIDATE_AZURE_AD_TOKEN="true" \
    SP_ORIGIN=$SP_ORIGIN \
    SCM_DO_BUILD_DURING_DEPLOYMENT="true" \
    WEBSITE_RUN_FROM_PACKAGE="1"

# 9. Configure startup command
az webapp config set \
  --name $APP_NAME \
  --resource-group $RG \
  --startup-file "gunicorn main:app --workers 4 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000 --timeout 120"

# 10. Deploy code
zip -r deploy.zip . -x "*.git*" -x "venv/*" -x "__pycache__/*" -x ".venv/*"

az webapp deployment source config-zip \
  --name $APP_NAME \
  --resource-group $RG \
  --src deploy.zip

# 11. Verify deployment
az webapp browse --name $APP_NAME --resource-group $RG
```

### Azure AD App Registration

```bash
# 1. Create app registration for API
az ad app create \
  --display-name "Concur Users API" \
  --identifier-uris "api://concur-users-api" \
  --sign-in-audience "AzureADMyOrg"

APP_ID=$(az ad app list --display-name "Concur Users API" --query "[0].appId" -o tsv)
echo "API Application ID: $APP_ID"

# 2. Expose API (add scope)
# Note: Must be done in Azure Portal
# App Registration → Expose an API → Add a scope
# Scope name: access_as_user
# Consent: Admins and users
# Description: Access Concur Users API

# 3. Note values for App Service configuration:
echo "Set these in App Service:"
echo "  AZURE_AD_TENANT_ID: $(az account show --query tenantId -o tsv)"
echo "  AZURE_AD_APP_ID: $APP_ID"
echo "  AZURE_AD_APP_ID_URI: api://concur-users-api"
```

---

## Troubleshooting

### Diagnostic Endpoints

**Check deployment**:
```bash
curl https://concur-users-api.azurewebsites.net/build
```

**Check Key Vault**:
```bash
curl https://concur-users-api.azurewebsites.net/kv-test
```

**Check Azure AD config**:
```bash
curl https://concur-users-api.azurewebsites.net/auth/config-status
```

**Check Concur auth**:
```bash
curl https://concur-users-api.azurewebsites.net/api/concur/auth-test
```

### Common Issues

**401 - Invalid JWT**:
- Check `AZURE_AD_TENANT_ID` matches your Azure AD tenant
- Check `AZURE_AD_APP_ID` matches API app registration
- Verify SharePoint web part requests token for correct API

**502 - Concur OAuth Failed**:
- Check Key Vault secrets are set correctly
- Verify Concur refresh token hasn't expired
- Test with `/api/concur/auth-test` endpoint

**502 - Concur 400 "Unrecognized attributes"**:
- Normal if tenant doesn't support Concur extension
- Should auto-fallback to `ATTRS_NO_CONCUR_EXT`
- Check `attributeMode` in response

**Empty users array**:
- Check Concur tenant has users
- Verify OAuth token has correct scopes
- Check pagination parameters

---

## Performance Characteristics

### Latency Breakdown

**Cold Start** (first request after deployment):
```
Total latency: ~3-5 seconds
├─ Key Vault secret fetch: 4 secrets × 100ms = 400ms
├─ Concur OAuth token: ~200ms
├─ Concur Identity API: ~1-2 seconds (depends on user count)
└─ Azure AD JWT validation: ~50ms (JWKS fetch)
```

**Warm Request** (subsequent requests):
```
Total latency: ~1-2 seconds
├─ Key Vault: 0ms (cached)
├─ Concur OAuth: 0ms (token cached, not expired)
├─ Azure AD JWT: ~10ms (JWKS cached)
└─ Concur Identity API: ~1-2 seconds (network + processing)
```

### Caching Strategy

| Item | TTL | Storage | Hit Rate (est.) |
|------|-----|---------|----------------|
| Key Vault secrets | 5 min | In-memory (per worker) | 99% |
| Concur access token | 30 min | In-memory (per worker) | 95% |
| Azure AD JWKS keys | LRU cache | In-memory (@lru_cache) | 99% |

### Scalability

**Current Limitations**:
- Sequential pagination (not parallelized)
- Single-tenant design (one Concur instance)
- Synchronous HTTP calls

**Estimated Throughput**:
- **50 users**: ~1-2 seconds
- **500 users**: ~2-5 seconds (3 pages @ 200 users/page)
- **5000 users**: ~20-30 seconds (25 pages)

**Future Optimizations**:
- Async/await with `httpx`
- Redis caching for cross-worker shared state
- GraphQL-style field selection
- WebSocket support for real-time updates

---

**Version**: 4.0  
**Author**: Engineering Team  
**Last Updated**: 2026-01-02
