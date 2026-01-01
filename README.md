# Concur Accruals API - Technical Documentation

## Overview

A production-grade FastAPI microservice providing SAP Concur expense management capabilities for SharePoint integration. The application aggregates financial accrual data from Concur's Identity, Expense Reports, and Cards APIs, exposing simplified REST endpoints for client consumption.

**Primary Integration**: SharePoint SPFx web parts using Azure AD OAuth 2.0  
**Authentication**: Dual OAuth flows (Azure AD for client auth, Concur OAuth for API access)  
**Deployment Target**: Azure App Service with Managed Identity  
**Configuration**: Azure Key Vault with in-memory caching  

---

## Architecture

### System Layers

```
┌─────────────────────────────────────────────────────┐
│              SharePoint SPFx Client                 │
│         (AadHttpClient + Azure AD tokens)           │
└──────────────────────┬──────────────────────────────┘
                       │ HTTPS + JWT
                       ▼
┌─────────────────────────────────────────────────────┐
│              FastAPI Application                     │
│  ┌───────────────────────────────────────────────┐  │
│  │ CORS Middleware                                │  │
│  │ - SharePoint origins whitelisted               │  │
│  └───────────┬───────────────────────────────────┘  │
│              ▼                                       │
│  ┌───────────────────────────────────────────────┐  │
│  │ Azure AD Authentication Layer                  │  │
│  │ - JWT signature verification (RS256)           │  │
│  │ - Audience/Issuer validation                   │  │
│  │ - Scope extraction                             │  │
│  └───────────┬───────────────────────────────────┘  │
│              ▼                                       │
│  ┌───────────────────────────────────────────────┐  │
│  │ API Endpoints Layer                            │  │
│  │ - /api/accruals/search                         │  │
│  │ - /api/cardtotals/export                       │  │
│  └───────────┬───────────────────────────────────┘  │
│              ▼                                       │
│  ┌───────────────────────────────────────────────┐  │
│  │ Business Logic Layer                           │  │
│  │ - User filtering (SCIM)                        │  │
│  │ - Report/card aggregation                      │  │
│  │ - Excel generation                             │  │
│  └───────────┬───────────────────────────────────┘  │
│              ▼                                       │
│  ┌───────────────────────────────────────────────┐  │
│  │ Concur OAuth Client                            │  │
│  │ - Refresh token flow                           │  │
│  │ - Access token caching (60s buffer)            │  │
│  └───────────┬───────────────────────────────────┘  │
└──────────────┼───────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────┐
│          Azure Key Vault (Managed Identity)          │
│  ┌───────────────────────────────────────────────┐   │
│  │ In-Memory Cache (300s TTL)                     │   │
│  │ - concur-client-id                             │   │
│  │ - concur-client-secret                         │   │
│  │ - concur-refresh-token                         │   │
│  │ - concur-api-base-url                          │   │
│  └────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────┘
               │
               ▼
┌───────────────────────────────────────────────────────┐
│               SAP Concur APIs                         │
│  - Identity v4.1 (SCIM user search)                   │
│  - Expense Reports v4 (report enumeration)            │
│  - Cards v4 (transaction queries)                     │
└───────────────────────────────────────────────────────┘
```

### Request Flow

1. **Client Request** → SharePoint SPFx web part calls API endpoint
2. **CORS Check** → Origin validated against whitelist
3. **JWT Validation** → Azure AD token signature/claims verified
4. **Key Vault Access** → Secrets fetched (with caching)
5. **OAuth Refresh** → Concur access token obtained/refreshed
6. **User Resolution** → Concur Identity API filters users by org structure
7. **Data Aggregation** → Parallel calls to Expense Reports + Cards APIs
8. **Business Logic** → Filter unsubmitted reports, unassigned cards
9. **Response** → JSON or Excel StreamingResponse

---

## Project Structure

```
├── main.py                          # FastAPI app entry point
├── requirements.txt                 # Python dependencies
├── .deployment                      # Azure deployment config
│
├── auth/
│   ├── __init__.py                  # Package exports
│   ├── azure_ad.py                  # JWT validation for SharePoint
│   └── concur_oauth.py              # OAuth client (standalone)
│
├── services/
│   ├── __init__.py                  
│   ├── identity_service.py          # Key Vault + Concur Identity API
│   ├── cards_service.py             # Concur Cards API wrapper
│   └── expense_service.py           # (Reserved for future)
│
├── logic/
│   ├── card_totals.py               # Card aggregation algorithms
│   └── accruals.py                  # (Reserved for future)
│
├── exports/
│   └── excel_export.py              # OpenPyXL report generation
│
├── models/
│   ├── requests.py                  # Pydantic request models
│   ├── responses.py                 # Pydantic response models
│   └── __init__.py
│
└── reports/
    └── accrual report.xlsx          # Excel template
```

---

## Core Components

### 1. Authentication Stack

#### Azure AD JWT Validation (`auth/azure_ad.py`)

**Purpose**: Validate SharePoint-issued Azure AD tokens

**Implementation**:
- Uses `PyJWT` + `cryptography` for RS256 signature verification
- Fetches Microsoft public keys from JWKS endpoint dynamically
- Caches signing keys using `@lru_cache`
- Validates: signature, expiration (`exp`), audience (`aud`), issuer (`iss`), not-before (`nbf`)

**Key Functions**:
```python
def validate_azure_ad_token(token: str) -> Dict:
    """
    1. Decode JWT header → extract kid (key ID)
    2. Fetch signing key from https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys
    3. Verify RSA-256 signature
    4. Validate claims (audience = API app ID, issuer = Azure AD tenant)
    5. Return decoded payload with user claims (upn, oid, name, scp)
    """
```

**FastAPI Dependency**:
```python
async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Security(bearer_scheme),
    x_request_id: str = Header(None)
) -> Dict:
    """
    Dependency injection for endpoints.
    Extracts Bearer token, validates, returns user claims.
    Raises HTTPException(401) if invalid.
    """
```

**Configuration** (env vars):
- `AZURE_AD_TENANT_ID`: Azure AD tenant UUID
- `AZURE_AD_APP_ID`: API app registration client ID
- `AZURE_AD_APP_ID_URI`: Application ID URI (e.g., `api://concur-accruals-api`)
- `VALIDATE_AZURE_AD_TOKEN`: `true|false` (bypass for local dev)

#### Concur OAuth Client (`auth/concur_oauth.py`)

**Purpose**: Manage Concur API access tokens via OAuth 2.0 refresh token flow

**Design Pattern**: Standalone class with no external dependencies (for reusability)

**Token Lifecycle**:
1. Initialize with `token_url`, `client_id`, `client_secret`, `refresh_token`
2. Cache access token in memory with expiration timestamp
3. Refresh when cached token expires (with 60s safety buffer)
4. Handle token rotation (Concur may return new refresh token)

**Implementation**:
```python
class ConcurOAuthClient:
    def get_access_token(self) -> str:
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token  # Use cached token
        
        # Refresh flow: POST to /oauth2/v0/token
        resp = requests.post(self.token_url, data={
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret
        })
        
        # Cache new token (typically 1800s TTL)
        self._access_token = data["access_token"]
        self._expires_at = time.time() + data.get("expires_in", 1800)
        
        # Handle refresh token rotation
        if "refresh_token" in data:
            self.refresh_token = data["refresh_token"]  # Update in-memory
        
        return self._access_token
```

**Usage in `main.py`**:
```python
# Singleton instance (refreshes credentials from Key Vault on each request)
oauth = ConcurOAuthClient()

def concur_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {oauth.get_access_token()}",
        "Accept": "application/json"
    }
```

**Error Handling**:
- 400/401/403 → Raises `HTTPException(502)` with diagnostic details
- Missing `access_token` in response → Raises with response keys for debugging

---

### 2. Configuration Management

#### Azure Key Vault Integration (`services/identity_service.py`)

**Authentication**: Azure Managed Identity (no credentials required)

**Implementation**:
```python
# Lazy initialization pattern
_credential: Optional[DefaultAzureCredential] = None
_secret_client: Optional[SecretClient] = None

def _get_secret_client() -> SecretClient:
    global _credential, _secret_client
    
    if _secret_client is None:
        _credential = DefaultAzureCredential()
        vault_url = f"https://{KEYVAULT_NAME}.vault.azure.net/"
        _secret_client = SecretClient(vault_url=vault_url, credential=_credential)
    
    return _secret_client
```

**Caching Strategy**:
```python
_SECRET_CACHE: Dict[str, Dict[str, object]] = {}
_SECRET_TTL_SECONDS = 300  # 5 minutes

def get_secret(name: str) -> str:
    now = time.time()
    cached = _SECRET_CACHE.get(name)
    
    if cached and (now - cached["ts"]) < _SECRET_TTL_SECONDS:
        return cached["value"]  # Return from cache
    
    # Fetch from Key Vault
    client = _get_secret_client()
    value = client.get_secret(name).value
    
    # Update cache
    _SECRET_CACHE[name] = {"value": value, "ts": now}
    return value
```

**Secrets Stored**:
- `concur-client-id`
- `concur-client-secret`
- `concur-refresh-token`
- `concur-api-base-url` (e.g., `https://us2.api.concursolutions.com`)
- `concur-token-url` (optional, defaults to `{base_url}/oauth2/v0/token`)

**Fallback Mechanism** (`main.py:kv()` wrapper):
```python
def kv(name: str, fallback: Optional[str] = None) -> Optional[str]:
    """
    Try Key Vault first, fall back to environment variable.
    Enables local development without Key Vault access.
    """
    try:
        val = get_secret(name)
        if val and val.strip():
            return val
        return fallback
    except Exception:
        return fallback  # Silently fall back (e.g., KEYVAULT_NAME not set)
```

---

### 3. API Endpoints

#### POST /api/accruals/search

**Purpose**: Find unsubmitted expense reports and unassigned card transactions

**Authentication**: Azure AD JWT required

**Request Model**:
```python
class AccrualsSearchRequest(BaseModel):
    orgUnit1: Optional[str] = None  # Organization hierarchy level 1
    orgUnit2: Optional[str] = None  # Organization hierarchy level 2
    orgUnit3: Optional[str] = None
    orgUnit4: Optional[str] = None
    orgUnit5: Optional[str] = None
    orgUnit6: Optional[str] = None
    custom21: Optional[str] = None  # Custom field (e.g., cost center)
```

**Implementation Flow**:
```python
@app.post("/api/accruals/search")
def accruals_search(
    req: AccrualsSearchRequest,
    current_user: Dict = Depends(get_current_user)  # JWT validation
):
    # 1. Build SCIM filter for Concur Identity API
    filter_expr = build_identity_filter(req)
    # Example: 'urn:...:orgUnit1 eq "Engineering" and urn:...:orgUnit2 eq "Platform"'
    
    # 2. Query Concur Identity API for matching users
    users = get_users(filter_expr)
    # Calls: GET /profile/identity/v4.1/Users?filter=...
    
    # 3. For each user, fetch expense reports and card transactions
    unsubmitted_reports = []
    unassigned_cards = []
    
    for user in users:
        # Expense Reports API
        reports = get_expense_reports(user["id"])
        unsubmitted_reports.extend(filter_unsubmitted_reports(reports))
        
        # Cards API (historical transactions)
        txns = get_card_transactions(user["id"], "2000-01-01", date.today().isoformat())
        unassigned_cards.extend(filter_unassigned_cards(txns))
    
    # 4. Return aggregated results
    return {
        "summary": {
            "unsubmittedReportCount": len(unsubmitted_reports),
            "unassignedCardCount": len(unassigned_cards)
        },
        "unsubmittedReports": [...],  # Array of report objects
        "unassignedCards": [...]       # Array of card transaction objects
    }
```

**SCIM Filter Construction**:
```python
def build_identity_filter(req: Any) -> Optional[str]:
    filters = []
    
    # Organization units (1-6)
    for i in range(1, 7):
        val = getattr(req, f"orgUnit{i}", None)
        if val:
            filters.append(
                f'urn:ietf:params:scim:schemas:extension:spend:2.0:User:orgUnit{i} eq "{val}"'
            )
    
    # Custom field 21
    if getattr(req, "custom21", None):
        filters.append(
            'urn:ietf:params:scim:schemas:extension:spend:2.0:User:customData'
            f'[id eq "custom21" and value eq "{req.custom21}"]'
        )
    
    return " and ".join(filters) if filters else None
```

**Filtering Logic**:
```python
def filter_unsubmitted_reports(reports: List[Dict]) -> List[Dict]:
    results = []
    for r in reports:
        payment_status = r.get("paymentStatusId")
        
        # Exclude already paid or processing
        if payment_status in ("P_PAID", "P_PROC"):
            continue
        
        results.append({
            "lastName": r.get("owner", {}).get("lastName"),
            "firstName": r.get("owner", {}).get("firstName"),
            "reportName": r.get("name"),
            "submitted": r.get("approvalStatus") == "Submitted",
            "reportCreationDate": r.get("creationDate"),
            "reportSubmissionDate": r.get("submitDate"),
            "paymentStatusId": payment_status,
            "totalAmount": r.get("totalAmount", {}).get("value")
        })
    
    return results

def filter_unassigned_cards(transactions: List[Dict]) -> List[Dict]:
    results = []
    for t in transactions:
        # Skip if already assigned to expense/report
        if t.get("expenseId") or t.get("reportId"):
            continue
        
        account = t.get("account") or {}
        payment_type = account.get("paymentType") or {}
        posted = t.get("postedAmount") or {}
        
        results.append({
            "cardProgramId": payment_type.get("id"),
            "accountKey": account.get("lastSegment"),
            "lastFourDigits": account.get("lastFourDigits"),
            "postedAmount": posted.get("value"),
            "currencyCode": posted.get("currencyCode")
        })
    
    return results
```

**Response Example**:
```json
{
  "summary": {
    "unsubmittedReportCount": 3,
    "unassignedCardCount": 7
  },
  "unsubmittedReports": [
    {
      "lastName": "Smith",
      "firstName": "John",
      "reportName": "March Travel",
      "submitted": false,
      "reportCreationDate": "2024-03-10T14:20:00Z",
      "reportSubmissionDate": null,
      "paymentStatusId": "NOT_PAID",
      "totalAmount": 542.30
    }
  ],
  "unassignedCards": [
    {
      "cardProgramId": "VISA_CORPORATE",
      "accountKey": "1234",
      "lastFourDigits": "5678",
      "postedAmount": 89.50,
      "currencyCode": "USD"
    }
  ]
}
```

---

#### POST /api/cardtotals/export

**Purpose**: Generate Excel report with card transaction totals aggregated by program and user

**Authentication**: Azure AD JWT required

**Request Model**:
```python
class CardTotalsRequest(AccrualsSearchRequest):
    transactionDateFrom: str  # YYYY-MM-DD format
    transactionDateTo: str    # YYYY-MM-DD format
    dateType: str             # "TRANSACTION" | "POSTED" | "BILLING"
```

**Date Type Logic**:
- **TRANSACTION**: Use `transactionDate` (card swipe date)
- **POSTED**: Use `postedDate` (bank clearing date)
- **BILLING**: Use `statement.billingDate` (statement period date)

**Implementation**:
```python
@app.post("/api/cardtotals/export")
def card_totals_export(
    req: CardTotalsRequest,
    current_user: Dict = Depends(get_current_user)
):
    # 1. Validate date type
    date_type = (req.dateType or "").upper()
    if date_type not in ("TRANSACTION", "POSTED", "BILLING"):
        raise HTTPException(400, "dateType must be TRANSACTION, POSTED, or BILLING")
    
    # 2. Query users + transactions (same as accruals search)
    filter_expr = build_identity_filter(req)
    users = get_users(filter_expr)
    
    all_txns = []
    for user in users:
        txns = get_card_transactions(
            user["id"],
            req.transactionDateFrom,
            req.transactionDateTo
        )
        all_txns.extend(txns)
    
    # 3. Aggregate by program and user
    totals = compute_card_totals(
        all_txns,
        date.fromisoformat(req.transactionDateFrom),
        date.fromisoformat(req.transactionDateTo),
        date_type
    )
    
    # 4. Generate Excel file
    xlsx_bytes = export_card_totals_excel(totals, {
        "from": req.transactionDateFrom,
        "to": req.transactionDateTo,
        "type": date_type
    })
    
    # 5. Stream response
    filename = f"Concur_Card_Totals_{datetime.now():%Y%m%d_%H%M}.xlsx"
    return StreamingResponse(
        BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )
```

**Aggregation Algorithm** (`logic/card_totals.py`):
```python
def compute_card_totals(transactions, from_date, to_date, date_type):
    by_program = defaultdict(lambda: {"count": 0, "total": 0.0, "currency": ""})
    by_user = defaultdict(lambda: {"count": 0, "total": 0.0, "currency": ""})
    
    for txn in transactions:
        # Extract date based on type
        if date_type == "BILLING":
            d = isoparse(txn["statement"]["billingDate"]).date()
        elif date_type == "POSTED":
            d = isoparse(txn["postedDate"]).date()
        else:  # TRANSACTION
            d = isoparse(txn["transactionDate"]).date()
        
        # Filter by date range
        if not (from_date <= d <= to_date):
            continue
        
        # Extract transaction details
        amount = float(txn["postedAmount"]["value"])
        currency = txn["postedAmount"]["currencyCode"]
        program = txn["account"]["paymentType"]["id"]
        employee_id = txn.get("employeeId")
        
        # User key: employee ID or account number
        user_key = employee_id if employee_id else \
                   f'{txn["account"]["lastSegment"]} ({program})'
        
        # Aggregate by program
        by_program[program]["count"] += 1
        by_program[program]["total"] += amount
        by_program[program]["currency"] = currency
        
        # Aggregate by user
        by_user[user_key]["count"] += 1
        by_user[user_key]["total"] += amount
        by_user[user_key]["currency"] = currency
    
    return {
        "totalsByProgram": [
            {"cardProgramId": k, **v} for k, v in by_program.items()
        ],
        "totalsByUser": [
            {"userKey": k, **v} for k, v in by_user.items()
        ]
    }
```

**Excel Generation** (`exports/excel_export.py`):
```python
def export_card_totals_excel(totals: Dict, meta: Dict) -> bytes:
    # Load template
    wb = load_workbook(TEMPLATE_PATH)
    
    # Get/create "Card totals" sheet
    ws = wb.get("Card totals") or wb.create_sheet("Card totals")
    ws.delete_rows(1, ws.max_row)  # Clear existing data
    
    # Header
    ws["A1"] = "Card totals"
    ws["A2"] = f"Generated {datetime.now():%Y-%m-%d %H:%M}"
    ws["A3"] = f'Date range: {meta["from"]} to {meta["to"]} ({meta["type"]})'
    
    # Totals by program table
    ws.append([])
    ws.append(["Card program", "Count", "Total", "Currency"])
    for prog in totals["totalsByProgram"]:
        ws.append([
            prog.get("cardProgramId"),
            prog.get("count"),
            prog.get("total"),
            prog.get("currency")
        ])
    
    # Totals by user table
    ws.append([])
    ws.append(["User key", "Count", "Total", "Currency"])
    for user in totals["totalsByUser"]:
        ws.append([
            user.get("userKey"),
            user.get("count"),
            user.get("total"),
            user.get("currency")
        ])
    
    # Save to BytesIO
    out = BytesIO()
    wb.save(out)
    out.seek(0)
    return out.read()
```

---

### 4. Concur API Integration

#### Identity API (SCIM v2)

**Endpoint**: `GET /profile/identity/v4.1/Users`

**Purpose**: Search for users matching organizational structure filters

**Implementation**:
```python
def get_users(filter_expression: Optional[str]) -> List[Dict]:
    url = f"{concur_base_url()}/profile/identity/v4.1/Users"
    params = {
        "attributes": (
            "id,displayName,userName,emails.value,"
            "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
        ),
        "filter": filter_expression
    }
    
    resp = requests.get(url, headers=concur_headers(), params=params, timeout=30)
    resp.raise_for_status()
    
    return resp.json().get("Resources", []) or []
```

**Example Filter**:
```
urn:ietf:params:scim:schemas:extension:spend:2.0:User:orgUnit1 eq "Engineering" and
urn:ietf:params:scim:schemas:extension:spend:2.0:User:orgUnit2 eq "Platform"
```

#### Expense Reports API

**Endpoint**: `GET /expensereports/v4/users/{userId}/reports`

**Purpose**: Retrieve all expense reports for a user

**Implementation**:
```python
def get_expense_reports(user_id: str) -> List[Dict]:
    url = f"{concur_base_url()}/expensereports/v4/users/{user_id}/reports"
    resp = requests.get(url, headers=concur_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("Items", []) or []
```

**Payment Status Codes**:
- `P_PAID`: Report paid (exclude from accruals)
- `P_PROC`: Payment processing (exclude from accruals)
- `NOT_PAID`: Not yet paid (include in accruals)

#### Cards API

**Endpoint**: `GET /cards/v4/users/{userId}/transactions`

**Query Parameters**:
- `transactionDateFrom`: YYYY-MM-DD
- `transactionDateTo`: YYYY-MM-DD

**Implementation**:
```python
def get_card_transactions(user_id: str, date_from: str, date_to: str) -> List[Dict]:
    url = f"{concur_base_url()}/cards/v4/users/{user_id}/transactions"
    params = {
        "transactionDateFrom": date_from,
        "transactionDateTo": date_to
    }
    resp = requests.get(url, headers=concur_headers(), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("Items", []) or []
```

**Transaction Object Structure**:
```json
{
  "transactionDate": "2024-03-15T10:30:00Z",
  "postedDate": "2024-03-17T00:00:00Z",
  "postedAmount": {
    "value": 89.50,
    "currencyCode": "USD"
  },
  "account": {
    "paymentType": {"id": "VISA_CORPORATE"},
    "lastSegment": "1234",
    "lastFourDigits": "5678"
  },
  "statement": {
    "billingDate": "2024-04-01T00:00:00Z"
  },
  "employeeId": "user-uuid",
  "expenseId": null,  // null = unassigned
  "reportId": null
}
```

---

### 5. Middleware & CORS

**CORS Configuration**:
```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://covantagenew.sharepoint.com",
        "https://covantagenew-admin.sharepoint.com"
    ],
    allow_credentials=True,  # Allow Authorization header
    allow_methods=["*"],     # POST, GET, OPTIONS
    allow_headers=["*"]      # Authorization, Content-Type, x-request-id
)
```

**Why CORS is Required**:
- SharePoint SPFx web parts run in browser (different origin than API)
- Preflight OPTIONS requests need 200 response
- `Authorization` header requires `allow_credentials=True`

---

### 6. Diagnostic Endpoints

#### GET /health
```python
@app.get("/health")
def health():
    return {"status": "healthy"}
```

#### GET /debug/azure-ad
```python
@app.get("/debug/azure-ad")
def debug_azure_ad():
    return {
        "azure_ad_config": get_azure_ad_config_status(),
        # Returns: validation_enabled, tenant_id, app_id, valid_audiences, valid_issuers
    }
```

#### GET /debug/user-info
```python
@app.get("/debug/user-info")
def debug_user_info(current_user: Dict = Depends(get_current_user)):
    """Requires valid Azure AD token. Returns decoded claims."""
    return {
        "authenticated": True,
        "upn": current_user.get("upn"),
        "oid": current_user.get("oid"),
        "name": current_user.get("name"),
        "x_request_id": current_user.get("x-request-id"),
        "all_claims": current_user
    }
```

#### GET /kv-test
```python
@app.get("/kv-test")
def kv_test():
    """Test Key Vault connectivity."""
    try:
        client_id = kv("concur-client-id")
        return {
            "status": "ok",
            "client_id_exists": bool(client_id),
            "keyvault": keyvault_status()
        }
    except Exception as e:
        raise HTTPException(500, {
            "message": f"Key Vault read failed: {str(e)}",
            "keyvault": keyvault_status()
        })
```

#### GET /api/concur/auth-test
```python
@app.get("/api/concur/auth-test")
def concur_auth_test():
    """End-to-end test: Key Vault → OAuth → Concur API call."""
    # 1. Load secrets
    # 2. Refresh Concur token
    # 3. Call Identity API
    # 4. Return status
```

---

## Configuration

### Environment Variables

#### Required (Azure App Service)

| Variable | Example | Description |
|----------|---------|-------------|
| `KEYVAULT_NAME` | `concur-secrets-kv` | Azure Key Vault name (auto-set by Azure) |
| `AZURE_AD_TENANT_ID` | `xxx-xxx-xxx-xxx` | Azure AD tenant ID |
| `AZURE_AD_APP_ID` | `xxx-xxx-xxx-xxx` | API app registration client ID |
| `AZURE_AD_APP_ID_URI` | `api://concur-accruals-api` | Application ID URI |
| `VALIDATE_AZURE_AD_TOKEN` | `true` | Enable JWT validation |

#### Optional (Fallbacks)

| Variable | Description |
|----------|-------------|
| `CONCUR_API_BASE_URL` | Fallback if Key Vault unavailable |
| `CONCUR_TOKEN_URL` | Fallback OAuth endpoint |
| `CONCUR_CLIENT_ID` | Fallback client ID |
| `CONCUR_CLIENT_SECRET` | Fallback client secret |
| `CONCUR_REFRESH_TOKEN` | Fallback refresh token |
| `SECRET_TTL_SECONDS` | Key Vault cache TTL (default: 300) |

### Azure Key Vault Secrets

| Secret Name | Example Value | Description |
|-------------|---------------|-------------|
| `concur-api-base-url` | `https://us2.api.concursolutions.com` | Concur data center URL |
| `concur-token-url` | `https://us2.api.concursolutions.com/oauth2/v0/token` | OAuth token endpoint |
| `concur-client-id` | `abc123...` | Concur app client ID |
| `concur-client-secret` | `secret456...` | Concur app client secret |
| `concur-refresh-token` | `refresh789...` | OAuth refresh token (rotates) |

---

## Deployment

### Azure App Service Requirements

1. **Runtime**: Python 3.11+
2. **Plan**: B1 or higher (Basic tier minimum)
3. **Managed Identity**: System-assigned enabled
4. **Key Vault Access**: Managed Identity granted `Get` and `List` secret permissions
5. **CORS**: SharePoint origins whitelisted

### Deployment Steps

```bash
# 1. Create deployment package
zip -r deploy.zip . \
  -x "*.git*" \
  -x "venv/*" \
  -x ".venv/*" \
  -x "__pycache__/*" \
  -x "*.pyc" \
  -x ".vscode/*"

# 2. Deploy to Azure
az webapp deployment source config-zip \
  --resource-group concur-accruals-rg \
  --name concur-accruals-api \
  --src deploy.zip

# 3. Configure startup command (App Service → Configuration → General Settings)
gunicorn main:app \
  --workers 4 \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8000 \
  --timeout 120
```

### Managed Identity Setup

```bash
# Enable system-assigned managed identity
az webapp identity assign \
  --name concur-accruals-api \
  --resource-group concur-accruals-rg

# Get principal ID
PRINCIPAL_ID=$(az webapp identity show \
  --name concur-accruals-api \
  --resource-group concur-accruals-rg \
  --query principalId -o tsv)

# Grant Key Vault access
az keyvault set-policy \
  --name concur-secrets-kv \
  --object-id $PRINCIPAL_ID \
  --secret-permissions get list
```

---

## Performance Considerations

### Caching Strategy

1. **Key Vault Secrets**: 5-minute in-memory cache
   - Reduces latency (Key Vault ~50-100ms per call)
   - Reduces cost (Key Vault charges per 10,000 operations)

2. **Concur Access Tokens**: 30-minute cache with 60s buffer
   - Reduces OAuth calls (Concur rate limits apply)
   - Automatic refresh on expiration

3. **Azure AD Signing Keys**: LRU cache (16 keys max)
   - Microsoft keys rotated infrequently
   - `@lru_cache` on `PyJWKClient`

### Rate Limiting (Concur APIs)

- **Identity API**: 200 requests/minute
- **Expense Reports API**: 100 requests/minute per user
- **Cards API**: 100 requests/minute per user

**Mitigation**:
- Implement exponential backoff on 429 responses
- Batch user processing
- Consider async/await for parallel calls (future enhancement)

### Scalability

**Current Implementation**: Synchronous (blocking I/O)

**Bottlenecks**:
- Sequential Concur API calls for each user
- Example: 50 users × 2 API calls × 500ms = 50 seconds response time

**Future Optimization**:
```python
import asyncio
import httpx

async def get_accruals_async(user_ids):
    async with httpx.AsyncClient() as client:
        tasks = [
            fetch_user_data(client, uid) for uid in user_ids
        ]
        results = await asyncio.gather(*tasks)
    return results
```

Expected improvement: 50 users × 500ms / 10 concurrent = 2.5 seconds

---

## Error Handling

### HTTP Status Codes

| Code | Meaning | Example Cause |
|------|---------|---------------|
| 200 | Success | Request completed |
| 400 | Bad Request | Invalid `dateType` parameter |
| 401 | Unauthorized | Missing/invalid Azure AD token |
| 403 | Forbidden | Valid token but insufficient scopes |
| 500 | Server Error | Key Vault secret missing |
| 502 | Bad Gateway | Concur OAuth failed |

### Error Response Format

```json
{
  "detail": {
    "status": "error",
    "error": "missing_config",
    "missing": ["concur-client-id / CONCUR_CLIENT_ID"],
    "hint": "Check Key Vault secrets"
  }
}
```

### Exception Hierarchy

```python
HTTPException(400)  # Client errors (validation)
  └─ Invalid dateType
  └─ Missing required filter

HTTPException(401)  # Authentication errors
  └─ missing_token
  └─ invalid_audience
  └─ invalid_issuer
  └─ token_expired

HTTPException(500)  # Server configuration errors
  └─ missing_config (Key Vault secret not found)
  └─ server_misconfiguration (Azure AD env vars missing)

HTTPException(502)  # External service errors
  └─ concur_oauth_failed
  └─ no_access_token_in_response
```

---

## Security

### Authentication Layers

1. **Transport**: HTTPS (enforced by Azure App Service)
2. **Origin**: CORS whitelist (SharePoint origins only)
3. **Client**: Azure AD JWT validation (RS256 signature)
4. **Service**: Concur OAuth (refresh token → access token)
5. **Credentials**: Azure Key Vault (Managed Identity)

### Token Validation

**Azure AD JWT**:
- ✅ Signature verified (RS256 with Microsoft public keys)
- ✅ Expiration checked (`exp` claim)
- ✅ Audience validated (must equal API app ID)
- ✅ Issuer validated (must be from configured tenant)
- ✅ Not-before checked (`nbf` claim)

**Concur OAuth**:
- ✅ Access token cached securely (in-memory only)
- ✅ Refresh token stored in Key Vault (not in code)
- ✅ Token rotation handled automatically

### Secrets Management

**Never in Code**:
- ❌ No hardcoded credentials
- ❌ No secrets in environment variable defaults
- ❌ No tokens logged

**Key Vault Best Practices**:
- ✅ Managed Identity authentication
- ✅ Secrets cached in memory (5 min TTL)
- ✅ Audit logging enabled on Key Vault
- ✅ Soft-delete enabled for recovery

---

## Local Development

### Setup

```bash
# 1. Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set environment variables (disable Azure AD validation)
export VALIDATE_AZURE_AD_TOKEN=false
export CONCUR_API_BASE_URL=https://us2.api.concursolutions.com
export CONCUR_CLIENT_ID=<from-concur>
export CONCUR_CLIENT_SECRET=<from-concur>
export CONCUR_REFRESH_TOKEN=<from-concur>

# 4. Run locally
uvicorn main:app --reload --port 8000
```

### Testing Endpoints

```bash
# Health check
curl http://localhost:8000/health

# Accruals search (no auth in dev mode)
curl -X POST http://localhost:8000/api/accruals/search \
  -H "Content-Type: application/json" \
  -d '{"orgUnit1": "Engineering"}'

# Card totals export
curl -X POST http://localhost:8000/api/cardtotals/export \
  -H "Content-Type: application/json" \
  -d '{
    "transactionDateFrom": "2024-01-01",
    "transactionDateTo": "2024-03-31",
    "dateType": "POSTED"
  }' \
  --output report.xlsx
```

---

## Dependencies

### Core Framework
```
fastapi==0.115.6        # Web framework
uvicorn==0.32.1         # ASGI server
gunicorn==23.0.0        # Production WSGI server
pydantic==2.10.4        # Data validation
```

### Azure Integration
```
azure-identity==1.19.0           # Managed Identity auth
azure-keyvault-secrets==4.8.0    # Key Vault SDK
```

### Authentication
```
PyJWT==2.8.0            # JWT decoding/validation
cryptography==42.0.5    # RSA signature verification
```

### External APIs
```
requests==2.32.3        # HTTP client (Concur APIs)
```

### Data Processing
```
openpyxl==3.1.5         # Excel file generation
python-dateutil==2.9.0  # Date parsing
```

---

## Monitoring & Observability

### Logging

**Startup Diagnostics** (console output):
```python
print("#### LOADED MAIN FROM:", __file__)
print("RUN_FROM_PACKAGE =", os.getenv("WEBSITE_RUN_FROM_PACKAGE"))
print("PWD =", os.getcwd())
```

**Structured Logging** (add to endpoints):
```python
import logging

logger = logging.getLogger(__name__)

logger.info("Accruals search", extra={
    "user_upn": current_user.get("upn"),
    "org_unit": req.orgUnit1,
    "user_count": len(users),
    "request_id": current_user.get("x-request-id")
})
```

### Metrics to Track

1. **Request Volume**:
   - Requests per endpoint
   - Success vs. error rates

2. **Latency**:
   - P50, P95, P99 response times
   - Key Vault fetch time
   - Concur API call time

3. **Authentication**:
   - Azure AD token validation success rate
   - Concur OAuth refresh frequency
   - 401/403 error rates

4. **Concur API**:
   - Rate limit hits (429 responses)
   - User query sizes
   - Transaction counts

---

## Troubleshooting

### Common Issues

**500 Error: "missing_config"**
- Cause: Key Vault secret not found
- Fix: Verify secret exists in Key Vault
- Check: `/kv-test` endpoint

**401 Error: "invalid_audience"**
- Cause: Azure AD token audience doesn't match `AZURE_AD_APP_ID`
- Fix: Verify app registration client ID matches environment variable
- Check: `/debug/azure-ad` endpoint

**502 Error: "concur_oauth_failed"**
- Cause: Concur refresh token expired/invalid
- Fix: Generate new refresh token from Concur App Center
- Check: Verify `concur-refresh-token` in Key Vault

**CORS Error (Browser Console)**
- Cause: SharePoint origin not whitelisted
- Fix: Add origin to `allow_origins` in `main.py` CORS middleware

**Slow Response Times**
- Cause: Sequential Concur API calls for many users
- Fix: Implement async/await (see Performance section)
- Mitigation: Limit org unit scope to reduce user count

---

## Architecture Decisions

### Why FastAPI?
- Modern async support (future-ready)
- Automatic OpenAPI/Swagger docs
- Pydantic integration for validation
- Dependency injection system

### Why Managed Identity?
- No credentials to manage
- Automatic Azure integration
- Audit trail via Azure AD
- Supports Key Vault rotation

### Why In-Memory Caching?
- No external dependencies (Redis, etc.)
- Sufficient for low-frequency secret reads
- Horizontal scaling: each instance has own cache
- Tradeoff: Cold start slower (first Key Vault call)

### Why Synchronous HTTP Calls?
- Simpler implementation
- Adequate for current load
- Future: migrate to `httpx` + `asyncio`

### Why Separate OAuth Client?
- Reusability across projects
- Testability (no external dependencies)
- Easier to swap implementations

---

## Future Enhancements

1. **Async/Await**: Convert to async Concur API calls (~70% latency reduction)
2. **Redis Caching**: Shared cache across instances for user data
3. **Rate Limiting**: Per-user request throttling
4. **Audit Logging**: Persist all API calls with user context
5. **Retry Logic**: Exponential backoff for 429/500 responses
6. **Pagination**: Handle large user result sets (1000+)
7. **Webhooks**: Real-time notifications from Concur events
8. **Multi-Tenant**: Support multiple Concur entities

---

## Version History

- **v2.0** (2025-01-01): Azure AD integration, CORS, refactored services
- **v1.0** (2024-12-29): Initial production release

Last Updated: 2025-01-01
