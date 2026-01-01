# SharePoint Integration Setup Guide

Complete guide for configuring Azure AD authentication between SharePoint SPFx web part and Concur Accruals API.

---

## Overview

Your SharePoint web part uses **AadHttpClient** to send requests with Azure AD JWT tokens. The API validates these tokens to ensure requests come from authorized SharePoint users.

### Authentication Flow

```
SharePoint User
    ↓
SPFx Web Part (AadHttpClient)
    ↓ Authorization: Bearer <Azure AD JWT>
    ↓ x-request-id: <UUID>
Azure App Service (FastAPI)
    ↓ Validate JWT signature
    ↓ Check audience (aud claim)
    ↓ Check issuer (iss claim)
    ↓ Check expiration
API Endpoint (process request)
```

---

## Prerequisites

1. **Azure AD App Registration** for the API
2. **Azure App Service** running the FastAPI application
3. **SharePoint SPFx Web Part** configured with `AadHttpClient`
4. **Azure Key Vault** (optional, for secrets)

---

## Step 1: Register API in Azure AD

### 1.1 Create App Registration

```bash
# Login to Azure
az login

# Create app registration
az ad app create \
  --display-name "Concur Accruals API" \
  --identifier-uris "api://concur-accruals-api" \
  --sign-in-audience "AzureADMyOrg"

# Get the Application (client) ID
APP_ID=$(az ad app list --display-name "Concur Accruals API" --query "[0].appId" -o tsv)
echo "Application ID: $APP_ID"
```

Or via Azure Portal:
- **Azure Active Directory** → **App registrations** → **New registration**
- **Name**: Concur Accruals API
- **Supported account types**: Accounts in this organizational directory only
- **Redirect URI**: Leave blank (not needed for API)

### 1.2 Expose API

**Portal**:
- App registration → **Expose an API**
- **Application ID URI**: `api://concur-accruals-api` (or use auto-generated)
- **Add a scope**:
  - **Scope name**: `access_as_user`
  - **Who can consent**: Admins and users
  - **Display name**: Access Concur Accruals API
  - **Description**: Allows the application to access Concur Accruals API on behalf of the signed-in user

**CLI**:
```bash
# Add delegated permission scope
az ad app update --id $APP_ID \
  --set api.oauth2Permissions='[{
    "adminConsentDescription": "Allows the app to access Concur Accruals API",
    "adminConsentDisplayName": "Access Concur Accruals API",
    "id": "'$(uuidgen)'",
    "isEnabled": true,
    "type": "User",
    "userConsentDescription": "Allows the app to access Concur Accruals API on your behalf",
    "userConsentDisplayName": "Access Concur Accruals API",
    "value": "access_as_user"
  }]'
```

### 1.3 Configure Authentication

**Portal**:
- App registration → **Authentication**
- **Platform**: Web (not SPA)
- **Redirect URIs**: (none needed for API-only app)
- **Implicit grant**: Uncheck all
- **Supported account types**: Single tenant

---

## Step 2: Configure SharePoint Web Part

### 2.1 Update package-solution.json

SPFx web part configuration file:

```json
{
  "solution": {
    "name": "concur-accruals-client-side-solution",
    "id": "...",
    "webApiPermissionRequests": [
      {
        "resource": "Concur Accruals API",
        "scope": "access_as_user"
      }
    ]
  }
}
```

### 2.2 Grant Admin Consent

After deploying the SPFx package to SharePoint:

**Portal**:
- **SharePoint Admin Center** → **Advanced** → **API Access**
- Find pending request for "Concur Accruals API"
- Click **Approve**

**CLI**:
```bash
# List pending requests
m365 spo serviceprincipal permissionrequest list

# Approve request (use ID from list output)
m365 spo serviceprincipal permissionrequest approve --id <request-id>
```

### 2.3 Web Part Properties

Your web part should expose configuration properties:

```typescript
export interface IConcurAccrualsWebPartProps {
  apiBaseUrl: string;          // e.g., "https://concur-accruals-api.azurewebsites.net"
  apiAppIdUri: string;         // e.g., "api://concur-accruals-api"
}
```

---

## Step 3: Configure Azure App Service

### 3.1 Set Environment Variables

```bash
RG="concur-accruals-rg"
APP_NAME="concur-accruals-api"
TENANT_ID="<your-azure-ad-tenant-id>"    # From Azure Portal → Azure Active Directory → Overview
APP_ID="<api-app-id-from-step-1>"        # From App Registration

# Set Azure AD configuration
az webapp config appsettings set \
  --resource-group $RG \
  --name $APP_NAME \
  --settings \
    AZURE_AD_TENANT_ID=$TENANT_ID \
    AZURE_AD_APP_ID=$APP_ID \
    AZURE_AD_APP_ID_URI="api://concur-accruals-api" \
    VALIDATE_AZURE_AD_TOKEN="true"
```

**Required Environment Variables**:

| Variable | Value | Description |
|----------|-------|-------------|
| `AZURE_AD_TENANT_ID` | `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` | Your Azure AD tenant ID |
| `AZURE_AD_APP_ID` | `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` | API app registration client ID |
| `AZURE_AD_APP_ID_URI` | `api://concur-accruals-api` | Application ID URI from app registration |
| `VALIDATE_AZURE_AD_TOKEN` | `true` | Enable token validation (set `false` for local dev only) |

### 3.2 Find Your Tenant ID

**Portal**:
- Azure Portal → **Azure Active Directory** → **Overview**
- Copy **Tenant ID**

**CLI**:
```bash
az account show --query tenantId -o tsv
```

**PowerShell**:
```powershell
(Get-AzContext).Tenant.Id
```

---

## Step 4: Configure CORS

SharePoint web parts run in the browser, so CORS must allow SharePoint origin.

```bash
# Add your SharePoint tenant URL
az webapp cors add \
  --resource-group $RG \
  --name $APP_NAME \
  --allowed-origins "https://yourtenant.sharepoint.com"

# If using SharePoint Online modern pages
az webapp cors add \
  --resource-group $RG \
  --name $APP_NAME \
  --allowed-origins "https://yourtenant-admin.sharepoint.com"
```

**Important**: Replace `yourtenant` with your actual SharePoint tenant name.

---

## Step 5: Test Authentication

### 5.1 Test Configuration

```bash
# Check Azure AD config is loaded
curl https://concur-accruals-api.azurewebsites.net/debug/azure-ad
```

Expected response:
```json
{
  "azure_ad_config": {
    "validation_enabled": true,
    "tenant_id_configured": true,
    "tenant_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "app_id_configured": true,
    "app_id_uri_configured": true,
    "valid_audiences": [
      "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      "api://concur-accruals-api"
    ],
    "valid_issuers": [
      "https://sts.windows.net/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx/",
      "https://login.microsoftonline.com/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx/v2.0"
    ]
  }
}
```

### 5.2 Test with SharePoint Token

From SharePoint SPFx web part browser console:

```typescript
// Get token
const client = await this.context.aadHttpClientFactory.getClient('api://concur-accruals-api');
const response = await client.get(
  'https://concur-accruals-api.azurewebsites.net/debug/user-info',
  AadHttpClient.configurations.v1
);
const userInfo = await response.json();
console.log('User info:', userInfo);
```

Expected response:
```json
{
  "authenticated": true,
  "upn": "user@yourtenant.onmicrosoft.com",
  "oid": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "name": "John Smith",
  "x_request_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
}
```

### 5.3 Test Full Flow

```typescript
// In SharePoint web part
const client = await this.context.aadHttpClientFactory.getClient('api://concur-accruals-api');

const response = await client.fetch(
  'https://concur-accruals-api.azurewebsites.net/api/accruals/search',
  AadHttpClient.configurations.v1,
  {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'x-request-id': crypto.randomUUID()
    },
    body: JSON.stringify({
      orgUnit1: 'Engineering'
    })
  }
);

if (!response.ok) {
  const error = await response.text();
  console.error('API error:', error);
  return;
}

const results = await response.json();
console.log('Accruals found:', results.summary);
```

---

## Troubleshooting

### Issue: 401 Unauthorized - "missing_token"

**Cause**: SharePoint not sending Authorization header

**Solution**:
1. Verify `apiAppIdUri` in web part properties matches app registration
2. Check admin consent granted in SharePoint Admin Center
3. Verify `webApiPermissionRequests` in `package-solution.json`

### Issue: 401 Unauthorized - "invalid_audience"

**Cause**: Token audience doesn't match API configuration

**Solution**:
```bash
# Check what audiences API expects
curl https://your-api.azurewebsites.net/debug/azure-ad

# Verify AZURE_AD_APP_ID and AZURE_AD_APP_ID_URI match app registration
az ad app show --id $APP_ID --query "{appId:appId, identifierUris:identifierUris}"
```

### Issue: 401 Unauthorized - "invalid_issuer"

**Cause**: Token from wrong Azure AD tenant

**Solution**:
```bash
# Verify AZURE_AD_TENANT_ID matches your tenant
az account show --query tenantId -o tsv

# Update app setting if wrong
az webapp config appsettings set \
  --name $APP_NAME \
  --resource-group $RG \
  --settings AZURE_AD_TENANT_ID="<correct-tenant-id>"
```

### Issue: CORS Error in Browser Console

**Cause**: SharePoint origin not allowed

**Solution**:
```bash
# Add SharePoint origin to CORS
az webapp cors add \
  --name $APP_NAME \
  --resource-group $RG \
  --allowed-origins "https://yourtenant.sharepoint.com"

# Verify CORS settings
az webapp cors show --name $APP_NAME --resource-group $RG
```

### Issue: 500 Error - "server_misconfiguration"

**Cause**: Azure AD environment variables not set

**Solution**:
```bash
# Check current settings
az webapp config appsettings list \
  --name $APP_NAME \
  --resource-group $RG \
  --query "[?name=='AZURE_AD_TENANT_ID' || name=='AZURE_AD_APP_ID'].{name:name, value:value}"

# Set missing variables (see Step 3.1)
```

---

## Local Development

### Disable Token Validation

For local testing without SharePoint:

```bash
# Run locally with validation disabled
export VALIDATE_AZURE_AD_TOKEN=false
export KEYVAULT_NAME=your-kv-name   # Still need Concur secrets

uvicorn main:app --reload
```

Or in `.env` file:
```
VALIDATE_AZURE_AD_TOKEN=false
KEYVAULT_NAME=your-kv-name
CONCUR_API_BASE_URL=https://us2.api.concursolutions.com
```

### Test with Postman

Get a token manually:

1. Azure Portal → App Registration → Overview → Copy Application ID
2. Postman → Authorization → Type: OAuth 2.0
3. Configure:
   - **Grant Type**: Authorization Code
   - **Auth URL**: `https://login.microsoftonline.com/<tenant-id>/oauth2/v2.0/authorize`
   - **Access Token URL**: `https://login.microsoftonline.com/<tenant-id>/oauth2/v2.0/token`
   - **Client ID**: SharePoint app client ID (not API app ID)
   - **Scope**: `api://concur-accruals-api/access_as_user`
4. Get Token
5. Use token in Authorization header for API calls

---

## Security Checklist

- [ ] Azure AD app registration created
- [ ] API scope (`access_as_user`) defined
- [ ] SharePoint admin consent granted
- [ ] CORS configured for SharePoint origin only
- [ ] AZURE_AD_TENANT_ID environment variable set
- [ ] AZURE_AD_APP_ID environment variable set
- [ ] AZURE_AD_APP_ID_URI environment variable set
- [ ] VALIDATE_AZURE_AD_TOKEN=true in production
- [ ] HTTPS enforced (automatic in Azure App Service)
- [ ] Token expiration validated (automatic)
- [ ] Token signature verified (automatic)

---

## Reference

### JWT Token Claims

SharePoint tokens include these claims:

| Claim | Description | Example |
|-------|-------------|---------|
| `aud` | Audience (API app ID) | `api://concur-accruals-api` |
| `iss` | Issuer (Azure AD endpoint) | `https://sts.windows.net/<tenant-id>/` |
| `iat` | Issued at time | `1640000000` |
| `exp` | Expiration time | `1640003600` |
| `upn` | User principal name | `user@tenant.com` |
| `oid` | Object ID (user) | `uuid` |
| `tid` | Tenant ID | `uuid` |
| `scp` | Scopes granted | `access_as_user` |

### API Endpoints

| Endpoint | Method | Auth Required | Purpose |
|----------|--------|---------------|---------|
| `/api/accruals/search` | POST | Yes | Search for accruals |
| `/api/cardtotals/export` | POST | Yes | Export card totals Excel |
| `/debug/azure-ad` | GET | No | Check Azure AD config |
| `/debug/user-info` | GET | Yes | Test token validation |
| `/health` | GET | No | Health check |

---

Last Updated: 2024-12-31
