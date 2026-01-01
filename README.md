# Concur Accruals API - Technical Documentation

## Overview

A FastAPI microservice providing SAP Concur expense card management for SharePoint SPFx integration. The primary use case is enabling SharePoint users to view and export their own unassigned card transactions, with legacy support for organization-wide accruals reporting.

**Primary Integration**: SharePoint SPFx web parts using Azure AD OAuth 2.0  
**Authentication**: Dual-layer (Azure AD JWT for client auth + Concur OAuth for API access)  
**Deployment Target**: Azure App Service with Managed Identity  
**Configuration**: Azure Key Vault with in-memory caching  

---

## Architecture Overview

### System Layers

```
┌─────────────────────────────────────────────────────┐
│         SharePoint User (Browser)                   │
└──────────────────────┬──────────────────────────────┘
                       │ HTTPS + Azure AD JWT
                       ▼
┌─────────────────────────────────────────────────────┐
│            FastAPI Application                       │
│  ┌───────────────────────────────────────────────┐  │
│  │ CORS Middleware (SP origin whitelist)        │  │
│  └───────────┬───────────────────────────────────┘  │
│              ▼                                       │
│  ┌───────────────────────────────────────────────┐  │
│  │ Azure AD JWT Validation                       │  │
│  │ - Extract user UPN from token                 │  │
│  │ - Verify signature, audience, issuer          │  │
│  └───────────┬───────────────────────────────────┘  │
│              ▼                                       │
│  ┌───────────────────────────────────────────────┐  │
│  │ User Identity Resolution                      │  │
│  │ - UPN → Concur User ID via Identity API      │  │
│  └───────────┬───────────────────────────────────┘  │
│              ▼                                       │
│  ┌───────────────────────────────────────────────┐  │
│  │ Cards API Integration                         │  │
│  │ - Fetch transactions for user                 │  │
│  │ - Defensive pagination (handles tenant quirks)│  │
│  │ - Filter unassigned (no expenseId/reportId)   │  │
│  └───────────┬───────────────────────────────────┘  │
│              ▼                                       │
│  ┌───────────────────────────────────────────────┐  │
│  │ Response Generation                           │  │
│  │ - JSON (for UI) OR                            │  │
│  │ - Excel export (via template)                 │  │
│  └───────────────────────────────────────────────┘  │
└──────────────┼───────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────┐
│      Azure Key Vault (Managed Identity)              │
│  - Concur OAuth credentials (cached 5min)            │
│  - Access token (cached in ConcurOAuthClient)        │
└───────────────────────────────────────────────────────┘
               │
               ▼
┌───────────────────────────────────────────────────────┐
│            SAP Concur APIs                            │
│  - Identity v4.1 (user resolution via SCIM)          │
│  - Cards v4 (transaction queries with pagination)     │
│  - Expense Reports v4 (legacy org-wide endpoint only) │
└───────────────────────────────────────────────────────┘
```

---

## Project Structure

```
├── main.py                          # FastAPI app + all endpoints
├── requirements.txt                 # Python dependencies
├── .deployment                      # Azure deployment config
│
├── auth/
│   ├── __init__.py                  # Package exports
│   ├── azure_ad.py                  # Azure AD JWT validation (SharePoint)
│   └── concur_oauth.py              # Concur OAuth refresh token client (standalone)
│
├── services/
│   ├── __init__.py                  
│   ├── identity_service.py          # Azure Key Vault access + Concur Identity helpers
│   ├── excel_export.py              # OpenPyXL report generation
│   └── cards_service.py             # (Not used by main.py; separate implementation)
│
├── logic/
│   └── card_totals.py               # (Not used; legacy aggregation code)
│
├── models/
│   ├── requests.py                  # Pydantic request models
│   ├── responses.py                 # Response models
│   └── __init__.py
│
└── reports/
    └── accrual report.xlsx          # Excel template (must be in deployment package)
```

**Note**: The application has a monolithic structure with all business logic in `main.py`. The `services/` directory contains reusable components, but endpoint implementations live in the main module.

---

## Core Implementation

### 1. Configuration Management

#### Helper Functions

```python
def env(name: str, fallback: Optional[str] = None) -> Optional[str]:
    """Read environment variable, return fallback if missing/empty."""
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return fallback
    return v

def kv(name: str, fallback: Optional[str] = None) -> Optional[str]:
    """
    Read from Azure Key Vault with fallback to environment variable.
    Uses services.identity_service.get_secret() which:
    - Authenticates via Managed Identity
    - Caches secrets in-memory for 5 minutes
    - Raises exception if Key Vault unavailable
    """
    try:
        return get_secret(name)  # from services.identity_service
    except Exception:
        return fallback  # silently fall back for local dev
```

#### Concur API Base URL

```python
def concur_base_url() -> str:
    """
    Priority order:
    1. Key Vault secret: concur-api-base-url
    2. Env var: CONCUR_API_BASE_URL
    3. Env var: CONCUR_BASE_URL
    4. Default: https://www.concursolutions.com
    """
    return (
        kv("concur-api-base-url")
        or env("CONCUR_API_BASE_URL")
        or env("CONCUR_BASE_URL")
        or "https://www.concursolutions.com"
    ).rstrip("/")
```

**Why Multiple Fallbacks**: Supports both local development (env vars) and production (Key Vault) without code changes.

---

### 2. Concur OAuth Client (Process-Scoped Singleton)

```python
_oauth_client: Optional[ConcurOAuthClient] = None

def get_oauth_client() -> ConcurOAuthClient:
    """
    Lazily instantiate and cache a ConcurOAuthClient for the process lifetime.
    
    Config priority:
    1. Key Vault secrets (concur-token-url, etc.)
    2. Environment variables (CONCUR_TOKEN_URL, etc.)
    
    Raises HTTPException(500) if required credentials missing.
    """
    global _oauth_client
    if _oauth_client is not None:
        return _oauth_client
    
    # Fetch credentials (Key Vault first, then env vars)
    token_url = kv("concur-token-url") or env("CONCUR_TOKEN_URL")
    client_id = kv("concur-client-id") or env("CONCUR_CLIENT_ID")
    client_secret = kv("concur-client-secret") or env("CONCUR_CLIENT_SECRET")
    refresh_token = kv("concur-refresh-token") or env("CONCUR_REFRESH_TOKEN")
    
    # Validate all required
    missing = []
    if not token_url:
        missing.append("concur-token-url / CONCUR_TOKEN_URL")
    # ... (check all 4 required fields)
    
    if missing:
        raise HTTPException(500, detail=f"Missing Concur OAuth config: {missing}")
    
    # Instantiate once
    _oauth_client = ConcurOAuthClient(
        token_url=token_url,
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
    )
    return _oauth_client
```

**Key Behavior**:
- **Singleton Pattern**: One instance per FastAPI worker process
- **Lazy Loading**: Only instantiated on first API call requiring Concur access
- **Token Caching**: `ConcurOAuthClient` handles access token caching (30min with 60s buffer)
- **Credential Refresh**: Fetches from Key Vault on initialization only; credentials not reloaded per-request

---

### 3. Authentication Flow

#### Azure AD JWT Validation (from SharePoint)

```python
from auth.azure_ad import get_current_user

@app.post("/api/cards/unassigned/search")
def api_cards_unassigned_search(
    req: UnassignedCardsRequest,
    current_user: Dict[str, Any] = Depends(get_current_user)  # FastAPI dependency
):
    # current_user contains decoded JWT claims:
    # - upn: user principal name (email)
    # - oid: object ID
    # - name: display name
    # - x-request-id: correlation ID from SharePoint
```

**What `get_current_user` Does** (in `auth/azure_ad.py`):
1. Extract `Bearer <token>` from `Authorization` header
2. Decode JWT header to get `kid` (key ID)
3. Fetch Microsoft public key from JWKS endpoint
4. Verify RSA-256 signature
5. Validate `exp` (expiration), `aud` (audience), `iss` (issuer), `nbf` (not-before)
6. Return decoded payload as dict
7. Raise `HTTPException(401)` if any validation fails

#### Concur OAuth (for API Access)

```python
def concur_headers() -> Dict[str, str]:
    """
    Returns headers with a valid Concur access token.
    Token automatically refreshed if expired.
    """
    oauth = get_oauth_client()
    token = oauth.get_access_token()  # Cached or refreshed
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
```

**Token Refresh Logic** (in `auth/concur_oauth.py`):
```python
def get_access_token(self) -> str:
    now = time.time()
    
    # Return cached token if still valid (60s safety buffer)
    if self._access_token and now < self._expires_at - 60:
        return self._access_token
    
    # Refresh token via Concur OAuth endpoint
    resp = requests.post(
        self.token_url,
        data={
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
    )
    
    data = resp.json()
    self._access_token = data["access_token"]
    self._expires_at = now + data.get("expires_in", 1800)  # Usually 30 min
    
    # Handle refresh token rotation (Concur may return new refresh token)
    if "refresh_token" in data:
        self.refresh_token = data["refresh_token"]
    
    return self._access_token
```

---

### 4. User Identity Resolution

**Problem**: SharePoint provides Azure AD UPN (e.g., `user@contoso.com`), but Concur APIs require Concur User ID (UUID).

**Solution**: `get_concur_user_id_for_upn()`

```python
def get_concur_user_id_for_upn(upn_or_email: str) -> str:
    """
    Resolve Azure AD UPN to Concur User ID via Identity v4.1 SCIM.
    
    Tries two SCIM filters:
    1. userName eq "user@contoso.com"
    2. emails.value eq "user@contoso.com"
    
    Returns: Concur user ID (UUID)
    Raises: HTTPException(404) if no match found
    """
    url = f"{concur_base_url()}/profile/identity/v4.1/Users"
    
    for flt in (f'userName eq "{upn_or_email}"', f'emails.value eq "{upn_or_email}"'):
        params = {"filter": flt, "startIndex": 1, "count": 1}
        resp = requests.get(url, headers=concur_headers(), params=params, timeout=30)
        resp.raise_for_status()
        
        resources = resp.json().get("Resources", []) or []
        if resources and isinstance(resources, list):
            user_id = resources[0].get("id")
            if user_id:
                return user_id  # Found!
    
    raise HTTPException(404, detail=f"Concur user not found for {upn_or_email}")
```

**Important**: This assumes Azure AD UPN matches Concur `userName` or `emails.value`. If your organization uses different identifiers, this logic needs to change.

---

### 5. Cards API Integration with Defensive Pagination

**Problem**: Concur Cards v4 API pagination behavior is tenant-dependent:
- Some tenants honor `page`/`pageSize` parameters
- Some tenants ignore pagination and always return first page
- No official docs clarify this behavior

**Solution**: Defensive pagination with infinite loop protection

```python
def get_card_transactions(
    user_id: str,
    date_from: str,
    date_to: str,
    status: Optional[str] = None,
    page_size: int = 200
) -> List[Dict]:
    """
    Fetch all card transactions for a user in a date range.
    
    Pagination strategy:
    - Iterate page 1, 2, 3, ... until one of:
      1. Empty response (no more data)
      2. Fewer items than page_size (last page)
      3. First transaction ID repeats (tenant ignores paging)
      4. Max 100 pages reached (hard safety limit)
    """
    url = f"{concur_base_url()}/cards/v4/users/{user_id}/transactions"
    
    # Clamp page_size to sane range
    page_size = max(1, min(page_size, 500))
    
    page = 1
    seen_first_id: Optional[str] = None
    all_items: List[Dict] = []
    
    while True:
        params = {
            "transactionDateFrom": date_from,
            "transactionDateTo": date_to,
            "page": page,
            "pageSize": page_size,
        }
        if status:
            params["status"] = status  # e.g., "UN" for unassigned
        
        resp = requests.get(url, headers=concur_headers(), params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json() or {}
        
        # Handle varying response shapes
        items = (
            payload.get("Items")
            or payload.get("items")
            or payload.get("Transactions")
            or payload.get("transactions")
            or []
        )
        
        if not items:
            break  # No more data
        
        # Detect if tenant ignores paging (returns same first item)
        first_id = str(items[0].get("id") or items[0].get("transactionId") or "")
        if page > 1 and first_id and seen_first_id == first_id:
            break  # Infinite loop protection
        if page == 1 and first_id:
            seen_first_id = first_id
        
        all_items.extend(items)
        
        if len(items) < page_size:
            break  # Last page (partial results)
        
        page += 1
        if page > 100:  # Safety: max 100 pages (100 * 500 = 50k transactions)
            break
    
    return all_items
```

**Why This Matters**:
- Without infinite loop protection, app could hang if tenant ignores paging
- Handles both compliant and non-compliant Concur implementations
- Provides safety limits to prevent runaway API calls

---

### 6. Primary Endpoint: Unassigned Cards for Current User

```python
@app.post("/api/cards/unassigned/search")
def api_cards_unassigned_search(
    req: UnassignedCardsRequest, 
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Returns unassigned card transactions for the authenticated SharePoint user.
    
    Flow:
    1. Extract UPN from Azure AD token claims
    2. Resolve UPN to Concur User ID
    3. Fetch card transactions (with status="UN" for unassigned)
    4. Filter out transactions with expenseId or reportId
    5. Return JSON with summary + transaction list
    """
    # Step 1: Get user identity from JWT
    upn = (
        current_user.get("upn") 
        or current_user.get("preferred_username") 
        or current_user.get("email")
    )
    if not upn:
        raise HTTPException(400, "Cannot determine user identity")
    
    # Step 2: UPN → Concur User ID
    concur_user_id = get_concur_user_id_for_upn(upn)
    
    # Step 3: Fetch transactions
    txns = get_card_transactions(
        concur_user_id,
        req.transactionDateFrom,
        req.transactionDateTo,
        status="UN",  # Unassigned filter
        page_size=req.pageSize,
    )
    
    # Step 4: Filter unassigned
    unassigned = filter_unassigned_cards(txns)
    
    # Step 5: Return JSON
    return {
        "summary": {
            "upn": upn,
            "concurUserId": concur_user_id,
            "dateFrom": req.transactionDateFrom,
            "dateTo": req.transactionDateTo,
            "unassignedCardCount": len(unassigned),
        },
        "transactions": unassigned,
    }
```

**Request Model**:
```python
class UnassignedCardsRequest(BaseModel):
    transactionDateFrom: str  # YYYY-MM-DD
    transactionDateTo: str    # YYYY-MM-DD
    dateType: str = "TRANSACTION"  # Metadata only (not used for filtering)
    pageSize: int = 200  # Capped to 1-500 in get_card_transactions()
```

**Response Example**:
```json
{
  "summary": {
    "upn": "john.smith@contoso.com",
    "concurUserId": "550e8400-e29b-41d4-a716-446655440000",
    "dateFrom": "2025-01-01",
    "dateTo": "2025-12-31",
    "unassignedCardCount": 3
  },
  "transactions": [
    {
      "transactionId": "txn-123",
      "cardProgramId": "VISA_CORPORATE",
      "cardProgramName": "Visa Corporate Card",
      "accountKey": "1234",
      "lastFourDigits": "5678",
      "transactionDate": "2025-03-15T10:30:00Z",
      "postedDate": "2025-03-17T00:00:00Z",
      "billingDate": null,
      "merchantName": "Amazon Web Services",
      "description": "AWS Cloud Services",
      "postedAmount": 125.50,
      "postedCurrencyCode": "USD",
      "transactionAmount": 125.50,
      "transactionCurrencyCode": "USD",
      "billingAmount": null,
      "billingCurrencyCode": null
    }
  ]
}
```

---

### 7. Excel Export Endpoint

```python
@app.post("/api/cards/unassigned/export")
def api_cards_unassigned_export(
    req: UnassignedCardsRequest, 
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Generate Excel file with unassigned cards for current user.
    
    Internally calls /search endpoint, then formats as Excel.
    """
    # Reuse search logic
    data = api_cards_unassigned_search(req, current_user=current_user)
    unassigned = data["transactions"]
    
    # Generate Excel bytes
    excel_bytes = export_accruals_to_excel(
        unsubmitted_reports=[],  # Not included in user-specific export
        unassigned_cards=unassigned,
        card_totals_by_program=None,
        card_totals_by_user=None,
        meta={
            "dateFrom": req.transactionDateFrom,
            "dateTo": req.transactionDateTo,
            "dateType": req.dateType
        },
    )
    
    # Stream response
    filename = f"Concur_Unassigned_Cards_{datetime.now():%Y%m%d_%H%M}.xlsx"
    return StreamingResponse(
        BytesIO(excel_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
       headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
```

**Excel Generation** (`services/excel_export.py`):
```python
def export_accruals_to_excel(
    unsubmitted_reports: List[Dict],
    unassigned_cards: List[Dict],
    card_totals_by_program: Optional[List[Dict]] = None,
    card_totals_by_user: Optional[List[Dict]] = None,
    meta: Optional[Dict] = None
) -> bytes:
    """
    Populate Excel template with data and return bytes.
    
    Template path resolution (handles Azure /home/site/wwwroot structure):
    - Resolves to: <script_dir>/../reports/accrual report.xlsx
    - Raises FileNotFoundError if template missing
    """
    if not os.path.exists(TEMPLATE_PATH):
        raise FileNotFoundError(f"Template not found: {TEMPLATE_PATH}")
    
    wb = load_workbook(TEMPLATE_PATH)
    
    # Populate "unassigned card transactions" sheet
    ws_cards = wb["unassigned card transactions"]
    # Clear existing data (row 2 onwards)
    _clear_data(ws_cards, start_row=2)
    
    for row, card in enumerate(unassigned_cards, start=2):
        ws_cards.cell(row, 1).value = card.get("cardProgramName")
        ws_cards.cell(row, 2).value = card.get("accountKey")
        ws_cards.cell(row, 3).value = card.get("lastFourDigits")
        ws_cards.cell(row, 4).value = card.get("transactionDate")
        ws_cards.cell(row, 5).value = card.get("postedDate")
        ws_cards.cell(row, 6).value = card.get("merchantName")
        ws_cards.cell(row, 7).value = card.get("description")
        ws_cards.cell(row, 8).value = card.get("postedAmount")
        ws_cards.cell(row, 9).value = card.get("postedCurrencyCode")
    
    # Save to BytesIO
    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return output.read()
```

**Template Requirements**:
- Must exist at `reports/accrual report.xlsx` in deployment package
- Must have sheet named `"unassigned card transactions"` with headers in row 1
- Optionally: `"unsubnitted reports"` sheet (for legacy endpoint)
- Optionally: `"Card totals"` sheet (created dynamically if needed)

---

### 8. Legacy Endpoint (Organization-Wide)

```python
@app.post("/api/accruals/search")
def api_accruals_search(req: AccrualsSearchRequest):
    """
    Legacy org-wide accruals export.
    
    WARNING: Can be slow for large organizations (sequential API calls per user).
    Does NOT require Azure AD authentication (should be added for production).
    
    Returns: Excel file (not JSON like v1 implementation)
    """
    # Get all users matching org filter
    users = get_users(req)
    
    unsubmitted_reports_all: List[Dict] = []
    unassigned_cards_all: List[Dict] = []
    
    # Sequential processing (slow!)
    for u in users:
        user_id = u.get("id")
        if not user_id:
            continue
        
        # Fetch expense reports
        reports = get_expense_reports(user_id)
        unsubmitted_reports_all.extend(filter_unsubmitted_reports(reports))
        
        # Fetch card transactions (huge date window!)
        txns = get_card_transactions(
            user_id,
            "2000-01-01",  # Historical
            datetime.now().strftime("%Y-%m-%d")
        )
        unassigned_cards_all.extend(filter_unassigned_cards(txns))
    
    # Generate Excel
    excel_bytes = export_accruals_to_excel(
        unsubmitted_reports_all,
        unassigned_cards_all
    )
    
    filename = f"Concur_Accruals_{datetime.now():%Y%m%d_%H%M}.xlsx"
    return StreamingResponse(
        BytesIO(excel_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
```

**SCIM Filter Construction** (for org filtering):
```python
def build_identity_filter(req: Any) -> Optional[str]:
    """
    Build SCIM filter for Concur Identity API.
    
    URN Schema: urn:ietf:params:scim:schemas:extension:concur:2.0:User
    (Note: Changed from 'spend' to 'concur' namespace)
    """
    parts = []
    if getattr(req, "orgUnit1", None):
        parts.append(
            f'urn:ietf:params:scim:schemas:extension:concur:2.0:User:orgUnit1 eq "{req.orgUnit1}"'
        )
    # ... similar for orgUnit2-6 and custom21
    
    if not parts:
        return None
    return " and ".join(parts)
```

---

## CORS Configuration

```python
allowed_origin = env("SP_ORIGIN", "")  # e.g., https://contoso.sharepoint.com
origins = [allowed_origin] if allowed_origin else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,  # Required for Authorization header
    allow_methods=["*"],
    allow_headers=["*"],
)
```

**Environment Variable**:
- `SP_ORIGIN`: SharePoint tenant URL (e.g., `https://contoso.sharepoint.com`)
- If not set: Allows all origins (`["*"]`) - **NOT RECOMMENDED FOR PRODUCTION**

---

## Diagnostic Endpoints

### GET /build
```python
@app.get("/build")
def build():
    return {
        "fingerprint": BUILD_FINGERPRINT,  # SCM_COMMIT_ID or WEBSITE_DEPLOYMENT_ID
        "run_from_package": env("WEBSITE_RUN_FROM_PACKAGE"),
        "cwd": os.getcwd(),
        "pythonpath0": sys.path[0],
    }
```

### GET /kv-test
```python
@app.get("/kv-test")
def kv_test():
    st = keyvault_status()  # from services.identity_service
    return {"status": "ok", "keyvault": st}
```

**Returns**:
```json
{
  "status": "ok",
  "keyvault": {
    "keyvault_name_set": true,
    "keyvault_name": "my-kv",
    "keyvault_url": "https://my-kv.vault.azure.net/",
    "client_initialized": true,
    "cache_size": 4,
    "cache_ttl_seconds": 300
  }
}
```

### GET /auth/config-status
```python
@app.get("/auth/config-status")
def auth_config_status():
    return get_azure_ad_config_status()  # from auth.azure_ad
```

**Returns Azure AD configuration** (tenant ID, app ID, valid audiences, etc.)

### GET /api/concur/auth-test
```python
@app.get("/api/concur/auth-test")
def api_concur_auth_test():
    """
    End-to-end test:
    1. Get OAuth client
    2. Fetch access token
    3. Call Concur Identity API (lightweight check)
    """
    try:
        return concur_auth_test()
    except Exception as ex:
        raise HTTPException(500, detail=str(ex))
```

**Returns**:
```json
{
  "status_code": 200,
  "ok": true,
  "sample": [{"id": "...", "userName": "..."}]
}
```

---

## Configuration

### Environment Variables

#### Required (Azure App Service)

| Variable | Example | Description |
|----------|---------|-------------|
| `KEYVAULT_NAME` | `concur-secrets-kv` | Azure Key Vault name (auto-set) |
| `AZURE_AD_TENANT_ID` | `xxx-xxx-xxx` | Azure AD tenant ID |
| `AZURE_AD_APP_ID` | `xxx-xxx-xxx` | API app registration client ID |
| `AZURE_AD_APP_ID_URI` | `api://concur-api` | Application ID URI |
| `VALIDATE_AZURE_AD_TOKEN` | `true` | Enable JWT validation |

#### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `SP_ORIGIN` | `*` | SharePoint origin for CORS |
| `SECRET_TTL_SECONDS` | `300` | Key Vault cache TTL |
| `CONCUR_TOKEN_URL` | (derived) | Override OAuth endpoint |
| `CONCUR_API_BASE_URL` | (required) | Fallback if Key Vault unavailable |

### Azure Key Vault Secrets

| Secret Name | Example Value |
|-------------|---------------|
| `concur-api-base-url` | `https://us2.api.concursolutions.com` |
| `concur-token-url` | `https://us2.api.concursolutions.com/oauth2/v0/token` |
| `concur-client-id` | `abc123...` |
| `concur-client-secret` | `secret456...` |
| `concur-refresh-token` | `refresh789...` |

---

## Deployment

### Azure App Service

```bash
# 1. Create deployment package
zip -r deploy.zip . -x "*.git*" -x "venv/*" -x "__pycache__/*"

# 2. Deploy
az webapp deployment source config-zip \
  --resource-group $RG \
  --name $APP_NAME \
  --src deploy.zip

# 3. Configure startup command
# App Service → Configuration → General Settings:
gunicorn main:app \
  --workers 4 \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8000 \
  --timeout 120
```

### Managed Identity

```bash
# Enable
az webapp identity assign --name $APP_NAME --resource-group $RG

# Grant Key Vault access
PRINCIPAL_ID=$(az webapp identity show --name $APP_NAME --resource-group $RG --query principalId -o tsv)
az keyvault set-policy --name $KV_NAME --object-id $PRINCIPAL_ID --secret-permissions get list
```

---

## Performance Considerations

### Bottlenecks

1. **Sequential Concur API calls** (legacy endpoint)
   - 50 users × 2 API calls × 500ms = 50 seconds
   - Solution: Implement async/await (future)

2. **Large date ranges** (legacy endpoint)
   - Fetching from "2000-01-01" to today can return 10k+ transactions
   - Solution: Limit date range or add pagination limits

3. **Key Vault latency** (first call)
   - ~50-100ms per secret
   - Mitigated by 5-minute cache

### Caching

- **Key Vault secrets**: 5min in-memory (per worker)
- **Concur access tokens**: 30min with 60s buffer
- **Azure AD signing keys**: LRU cache (16 keys)

---

## Security

### Authentication Layers
1. HTTPS (Azure App Service enforced)
2. CORS (SharePoint origin whitelist)
3. Azure AD JWT (signature + claims validation)
4. Concur OAuth (refresh token → access token)
5. Managed Identity (Key Vault access)

### What's NOT Authenticated
- **`/api/accruals/search`**: Legacy endpoint has NO auth check
  - Should add `current_user: Dict = Depends(get_current_user)` parameter
  - Currently open to anyone with network access

---

## Troubleshooting

### 404: Concur user not found
- **Cause**: UPN from Azure AD doesn't match Concur userName or emails.value
- **Fix**: Verify UPN mapping or implement custom resolution logic

### Pagination returns duplicate transactions
- **Cause**: Tenant ignores `page` parameter
- **Mitigation**: Defensive pagination detects this and stops

### Excel template not found
- **Cause**: `reports/accrual report.xlsx` not in deployment package
- **Fix**: Ensure file included in zip and path relative to main.py

### Slow responses (legacy endpoint)
- **Cause**: Sequential API calls for many users
- **Fix**: Limit org scope or implement async

---

## Version History

- **v3.0** (2026-01-01): User-centric design, defensive pagination, Excel template path fix
- **v2.0** (2025-01-01): Azure AD integration, CORS, refactored services
- **v1.0** (2024-12-29): Initial production release

**Last Updated**: 2026-01-01  
**Maintained By**: [Your Team]
