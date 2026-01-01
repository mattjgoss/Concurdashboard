# Quick Reference: SharePoint → Concur API Integration

## 🔑 Core Concept

SharePoint sends **Azure AD JWT tokens** → API validates tokens → Processes request

---

## 📋 Configuration Checklist

### Azure App Service (Required)

```bash
AZURE_AD_TENANT_ID=<your-tenant-id>          # Azure AD → Overview → Tenant ID
AZURE_AD_APP_ID=<api-app-registration-id>    # From Azure AD App Registration
AZURE_AD_APP_ID_URI=api://concur-accruals-api # From App Registration → Expose an API
VALIDATE_AZURE_AD_TOKEN=true                 # Enable validation
```

### SharePoint Web Part

```typescript
apiBaseUrl: "https://concur-accruals-api.azurewebsites.net"
apiAppIdUri: "api://concur-accruals-api"
```

### CORS

```bash
az webapp cors add --allowed-origins "https://yourtenant.sharepoint.com"
```

---

## 🧪 Quick Tests

### 1. Check Azure AD Config
```bash
curl https://your-api.azurewebsites.net/debug/azure-ad
```
✅ Should show `validation_enabled: true`, tenant/app IDs configured

### 2. Test from SharePoint (Browser Console)
```typescript
const client = await this.context.aadHttpClientFactory.getClient('api://concur-accruals-api');
const resp = await client.get('https://your-api.azurewebsites.net/debug/user-info', AadHttpClient.configurations.v1);
console.log(await resp.json());
```
✅ Should show your user info (upn, name, oid)

### 3. Test Search Endpoint
```typescript
const client = await this.context.aadHttpClientFactory.getClient('api://concur-accruals-api');
const resp = await client.fetch(
  'https://your-api.azurewebsites.net/api/accruals/search',
  AadHttpClient.configurations.v1,
  { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({orgUnit1: 'Engineering'}) }
);
console.log(await resp.json());
```
✅ Should return accruals search results

---

## 🚨 Common Errors

| Error | Cause | Fix |
|-------|-------|-----|
| **401 - missing_token** | No Authorization header | Check SharePoint admin consent granted |
| **401 - invalid_audience** | Token aud ≠ API app ID | Verify `AZURE_AD_APP_ID` matches app registration |
| **401 - invalid_issuer** | Token from wrong tenant | Verify `AZURE_AD_TENANT_ID` is correct |
| **500 - server_misconfiguration** | Env vars not set | Set AZURE_AD_* variables in App Service |
| **CORS error** | Origin not allowed | Add SharePoint URL to CORS |

---

## 📚 Documentation Files

- **`APPLICATION_REVIEW.md`** - Complete review of changes made
- **`SHAREPOINT_SETUP.md`** - Step-by-step setup guide
- **`SHAREPOINT_INTEGRATION.md`** - Integration summary
- **`README.md`** - Full application documentation

---

## 🔐 What Gets Validated

✅ Token signature (RSA-256 with Microsoft keys)  
✅ Token expiration (`exp` claim)  
✅ Audience (`aud` = your API app ID)  
✅ Issuer (`iss` = your Azure AD tenant)  
✅ Not before time (`nbf` claim)

---

## 🎯 Request Flow

```
SharePoint User → Web Part → AadHttpClient (gets token) →
POST /api/accruals/search + Bearer token →
API validates token → Processes request → Returns JSON
```

---

## 💻 Local Development

Disable validation for testing:
```bash
export VALIDATE_AZURE_AD_TOKEN=false
uvicorn main:app --reload
```

⚠️ Set `VALIDATE_AZURE_AD_TOKEN=true` in production!

---

## 🛠️ Diagnostic Endpoints

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `/debug/azure-ad` | No | Check Azure AD configuration |
| `/debug/user-info` | Yes | Test token validation |
| `/health` | No | Health check |
| `/build` | No | Build/deployment info |

---

Updated: 2024-12-31
