# SharePoint Integration - Changes Summary

## Overview

Your application is now configured to accept Azure AD JWT tokens from SharePoint's `AadHttpClient`. This document summarizes what was added and how it works.

---

## Files Modified/Created

### ✅ New Files

1. **`auth/azure_ad.py`** - Azure AD JWT validation module
   - Token signature verification using Microsoft's JWKS endpoint
   - Audience and issuer validation
   - Scope/role checking
   - FastAPI dependency injection helpers

2. **`SHAREPOINT_SETUP.md`** - Complete setup guide
   - Azure AD app registration steps
   - Environment variable configuration
   - CORS setup
   - Troubleshooting guide

### ✅ Modified Files

1. **`requirements.txt`**
   - Added: `PyJWT==2.8.0` (JWT decoding/validation)
   - Added: `cryptography==42.0.5` (RSA signature verification)

2. **`main.py`**
   - Imported Azure AD authentication module
   - Updated `/api/accruals/search` to require authentication
   - Updated `/api/cardtotals/export` to require authentication
   - Added `/debug/azure-ad` diagnostic endpoint
   - Added `/debug/user-info` test endpoint

---

## How It Works

### Request Flow from SharePoint

```
1. SharePoint User clicks "Search Accruals" in web part
2. SPFx web part calls AadHttpClient.getClient('api://concur-accruals-api')
3. AadHttpClient automatically gets Azure AD token for current user
4. Web part POSTs to /api/accruals/search with:
   - Authorization: Bearer <jwt-token>
   - x-request-id: <uuid>
   - Content-Type: application/json
   - Body: { orgUnit1: "Engineering", ... }
5. FastAPI receives request
6. get_current_user dependency executes:
   a. Extracts Bearer token from Authorization header
   b. Decodes JWT header to get signing key ID (kid)
   c. Fetches Microsoft public key from JWKS endpoint
   d. Verifies signature using RSA-256
   e. Validates expiration (exp claim)
   f. Validates audience matches AZURE_AD_APP_ID or AZURE_AD_APP_ID_URI
   g. Validates issuer matches Azure AD tenant
   h. Returns decoded token payload
7. Endpoint processes request if token valid
8. Returns JSON response (or Excel blob for export)
9. SharePoint web part displays results
```

### Token Validation Details

The `auth/azure_ad.py` module validates:

✅ **Signature**: RSA-256 with Microsoft's public keys  
✅ **Expiration**: Token not expired (exp claim)  
✅ **Not Before**: Token valid (nbf claim)  
✅ **Audience**: Matches your API app ID  
✅ **Issuer**: From your Azure AD tenant  
✅ **Claims**: Required claims present (aud, iss, exp, iat)

### What SharePoint Sends

Your SPFx web part automatically includes:

```typescript
Authorization: Bearer eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiIsIng1dCI6...
x-request-id: 550e8400-e29b-41d4-a716-446655440000
Content-Type: application/json
```

The JWT token contains claims like:
```json
{
  "aud": "api://concur-accruals-api",
  "iss": "https://sts.windows.net/<tenant-id>/",
  "upn": "user@tenant.com",
  "oid": "<user-object-id>",
  "scp": "access_as_user",
  "exp": 1640003600,
  "iat": 1640000000
}
```

---

## Configuration Required

### Environment Variables (Azure App Service)

Set these in Azure App Service → Configuration → Application Settings:

```bash
AZURE_AD_TENANT_ID=<your-tenant-id>           # Required
AZURE_AD_APP_ID=<api-app-id>                  # Required
AZURE_AD_APP_ID_URI=api://concur-accruals-api # Required
VALIDATE_AZURE_AD_TOKEN=true                  # Required for prod
```

### How to Get Values

**AZURE_AD_TENANT_ID**:
```bash
az account show --query tenantId -o tsv
```

**AZURE_AD_APP_ID**:
- Create App Registration in Azure Portal
- Copy Application (client) ID

**AZURE_AD_APP_ID_URI**:
- App Registration → Expose an API
- Set to `api://concur-accruals-api` (or custom value)

### SharePoint Configuration

1. **package-solution.json** in SPFx project:
```json
{
  "webApiPermissionRequests": [
    {
      "resource": "Concur Accruals API",
      "scope": "access_as_user"
    }
  ]
}
```

2. **Grant Admin Consent**:
   - SharePoint Admin Center → Advanced → API Access
   - Approve "Concur Accruals API" request

3. **Web Part Properties**:
```typescript
apiBaseUrl: "https://concur-accruals-api.azurewebsites.net"
apiAppIdUri: "api://concur-accruals-api"
```

---

## Testing

### 1. Verify Azure AD Configuration

```bash
curl https://concur-accruals-api.azurewebsites.net/debug/azure-ad
```

Should return:
```json
{
  "azure_ad_config": {
    "validation_enabled": true,
    "tenant_id_configured": true,
    "app_id_configured": true,
    "valid_audiences": ["api://concur-accruals-api", "..."],
    "valid_issuers": ["https://sts.windows.net/.../", "..."]
  }
}
```

### 2. Test from SharePoint

From SharePoint web part browser console:

```typescript
const client = await this.context.aadHttpClientFactory.getClient('api://concur-accruals-api');
const resp = await client.get(
  'https://concur-accruals-api.azurewebsites.net/debug/user-info',
  AadHttpClient.configurations.v1
);
console.log(await resp.json());
```

Should return your user info:
```json
{
  "authenticated": true,
  "upn": "you@tenant.com",
  "name": "Your Name",
  "oid": "..."
}
```

### 3. Test Full Accruals Search

```typescript
const client = await this.context.aadHttpClientFactory.getClient('api://concur-accruals-api');
const resp = await client.fetch(
  'https://concur-accruals-api.azurewebsites.net/api/accruals/search',
  AadHttpClient.configurations.v1,
  {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ orgUnit1: 'Engineering' })
  }
);
console.log(await resp.json());
```

---

## Error Handling

### Common Errors and Solutions

**401 - "missing_token"**
- SharePoint not sending token
- Check `webApiPermissionRequests` in SPFx manifest
- Verify admin consent granted

**401 - "invalid_audience"**
- Token audience doesn't match configuration
- Verify `AZURE_AD_APP_ID` and `AZURE_AD_APP_ID_URI` match app registration
- Check web part `apiAppIdUri` property

**401 - "invalid_issuer"**
- Token from wrong tenant
- Verify `AZURE_AD_TENANT_ID` matches your Azure AD tenant

**401 - "token_expired"**
- Token expired (tokens valid ~1 hour)
- AadHttpClient should auto-refresh, check SharePoint logs

**403 - "insufficient_scope"**
- User doesn't have required permissions
- Check scope in token: `/debug/user-info` → `scp` claim

**CORS Error**
- SharePoint origin not allowed
- Add to CORS: `az webapp cors add --allowed-origins "https://tenant.sharepoint.com"`

**500 - "server_misconfiguration"**
- Environment variables not set
- Run `/debug/azure-ad` to check configuration

---

## Local Development

### Option 1: Disable Validation

For local testing without SharePoint tokens:

```bash
export VALIDATE_AZURE_AD_TOKEN=false
export KEYVAULT_NAME=your-kv
uvicorn main:app --reload --port 8000
```

Test with curl:
```bash
curl -X POST http://localhost:8000/api/accruals/search \
  -H "Content-Type: application/json" \
  -d '{"orgUnit1": "Engineering"}'
```

### Option 2: Use Real Tokens

Get token from browser:
1. Open SharePoint in browser
2. F12 → Console
3. Run:
```javascript
const client = await this.context.aadHttpClientFactory.getClient('api://concur-accruals-api');
const token = await client._getAccessToken();
console.log(token);
```
4. Copy token and use in Postman/curl:
```bash
curl -X POST http://localhost:8000/api/accruals/search \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"orgUnit1": "Engineering"}'
```

---

## Security Benefits

✅ **No API Keys**: Uses Azure AD identity instead of shared secrets  
✅ **User Context**: Know which SharePoint user made the request  
✅ **Automatic Expiration**: Tokens expire after 1 hour  
✅ **Signature Verification**: Cryptographically verified using Microsoft's keys  
✅ **Tenant Isolation**: Only your Azure AD tenant can issue valid tokens  
✅ **Scope Control**: Can require specific permissions (roles/scopes)  
✅ **Audit Trail**: Can log user UPN for compliance

---

## Optional Enhancements

### 1. Require Specific Scopes

```python
from auth.azure_ad import require_scope

@app.post("/api/accruals/search", dependencies=[Depends(require_scope("Admin.Write"))])
def accruals_search(req: AccrualsSearchRequest):
    # Only users with Admin.Write scope can access
    pass
```

### 2. Log User Activity

```python
@app.post("/api/accruals/search")
def accruals_search(
    req: AccrualsSearchRequest,
    current_user: Dict = Depends(get_current_user)
):
    logger.info(f"Accruals search by {current_user.get('upn')}", extra={
        "user_oid": current_user.get("oid"),
        "org_unit": req.orgUnit1,
        "request_id": current_user.get("x-request-id")
    })
    # ... process request
```

### 3. Role-Based Access Control

```python
def require_admin(current_user: Dict = Depends(get_current_user)):
    roles = current_user.get("roles", [])
    if "Admin" not in roles:
        raise HTTPException(status_code=403, detail="Admin role required")
    return current_user

@app.delete("/api/admin/purge", dependencies=[Depends(require_admin)])
def admin_purge():
    # Only admin role can access
    pass
```

---

## Next Steps

1. ✅ **Deploy Updated Code**
   ```bash
   zip -r deploy.zip . -x "*.git*" -x "venv/*"
   az webapp deployment source config-zip --src deploy.zip ...
   ```

2. ✅ **Set Environment Variables** (see Configuration section)

3. ✅ **Test Configuration**
   ```bash
   curl https://your-api.azurewebsites.net/debug/azure-ad
   ```

4. ✅ **Update SharePoint Web Part**
   - Set `apiAppIdUri` property
   - Deploy and test

5. ✅ **Grant Admin Consent** in SharePoint Admin Center

6. ✅ **Configure CORS** for SharePoint origin

7. ✅ **Test End-to-End** from SharePoint

---

## Support

See `SHAREPOINT_SETUP.md` for detailed setup instructions and troubleshooting.

---

Last Updated: 2024-12-31
