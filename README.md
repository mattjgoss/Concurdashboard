# Concur Accruals API

A comprehensive FastAPI-based REST API service for managing SAP Concur expense accruals, card transaction reporting, and user expense management. This application integrates with SAP Concur's Identity, Expense Reports, and Cards APIs to provide consolidated reporting and analytics capabilities.

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Features](#features)
4. [Technical Stack](#technical-stack)
5. [Project Structure](#project-structure)
6. [How It Works](#how-it-works)
7. [API Endpoints](#api-endpoints)
8. [Authentication & Authorization](#authentication--authorization)
9. [Configuration](#configuration)
10. [Local Development](#local-development)
11. [Deployment Considerations](#deployment-considerations)
12. [Security Best Practices](#security-best-practices)
13. [Monitoring & Logging](#monitoring--logging)
14. [Troubleshooting](#troubleshooting)
15. [API Rate Limits](#api-rate-limits)
16. [Future Enhancements](#future-enhancements)

---

## Overview

The Concur Accruals API is designed to streamline financial reporting and accruals management for organizations using SAP Concur. It provides functionality to:

- **Search for unsubmitted expense reports** across organizational units
- **Identify unassigned card transactions** that haven't been expensed
- **Generate card transaction totals** grouped by card program and user
- **Export comprehensive reports** to Excel format

This service acts as a middleware layer between your organization's financial systems and SAP Concur, providing simplified access to complex multi-step API workflows.

---

## Architecture

### High-Level Architecture

```
┌─────────────────┐
│   Client App    │ (Web UI, PowerBI, etc.)
└────────┬────────┘
         │ HTTP/REST
         ▼
┌─────────────────┐
│  FastAPI Server │ (This application)
└────────┬────────┘
         │
         ├─► OAuth Token Management
         │   (ConcurOAuthClient)
         │
         ├─► Identity Service
         │   (User Search via SCIM)
         │
         ├─► Expense Reports Service
         │   (Fetch & Filter Reports)
         │
         ├─► Cards Service
         │   (Transaction Queries)
         │
         └─► Excel Export Service
             (Generate Reports)
         
         ▼
┌─────────────────┐
│  SAP Concur     │
│  APIs           │
│  - Identity v4.1│
│  - Expenses v4  │
│  - Cards v4     │
└─────────────────┘
```

### Component Flow

1. **Client Request**: External system calls REST endpoint
2. **Authentication**: OAuth client refreshes access token if needed
3. **User Resolution**: Identity API filters users by org units/custom fields
4. **Data Aggregation**: For each user, fetch expenses/cards
5. **Filtering**: Apply business logic to exclude paid/assigned items
6. **Report Generation**: Format data and optionally export to Excel
7. **Response**: Return JSON or Excel file stream

---

## Features

### 1. Accruals Search (`/api/accruals/search`)

Searches for financial accruals across organizational units and returns:

- **Unsubmitted Expense Reports**: Reports created but not yet submitted or still in approval
- **Unassigned Card Transactions**: Card charges not yet assigned to an expense report

**Filtering Capabilities**:
- Organization units 1-6
- Custom field 21 (e.g., cost center, department code)

### 2. Card Totals Export (`/api/cardtotals/export`)

Generates detailed card transaction totals with:

- **Aggregation by Card Program**: Total spend per card type
- **Aggregation by User**: Total spend per employee or card account
- **Flexible Date Types**: Filter by transaction date, posted date, or billing date
- **Excel Export**: Pre-formatted workbook ready for financial analysis

---

## Technical Stack

### Core Framework
- **FastAPI**: Modern, high-performance Python web framework
- **Uvicorn/Gunicorn**: ASGI server for production deployment
- **Pydantic**: Data validation and serialization

### External Integrations
- **SAP Concur API**: Identity v4.1, Expense Reports v4, Cards v4
- **OAuth 2.0**: Refresh token flow for authentication

### Data Processing
- **OpenPyXL**: Excel file manipulation
- **python-dateutil**: Date parsing and handling
- **Requests**: HTTP client library

### Python Version
- **Python 3.8+** recommended (uses type hints, f-strings, etc.)

---

## Project Structure

```
Accural/
├── main.py                      # Main FastAPI application (monolithic)
├── requirements.txt             # Python dependencies
│
├── auth/
│   └── concur_oauth.py         # OAuth token management with Key Vault
│
├── services/
│   ├── identity_service.py     # Concur Identity API wrapper
│   ├── cards_service.py        # Concur Cards API wrapper
│   └── expense_service.py      # (Empty - future expenses service)
│
├── models/
│   ├── requests.py             # Pydantic request models
│   └── responses.py            # (Future response models)
│
├── logic/
│   ├── accruals.py             # (Empty - future business logic)
│   └── card_totals.py          # Card totals computation logic
│
├── exports/
│   └── excel_export.py         # Excel report generation
│
└── reports/
    └── accrual report.xlsx     # Excel template for exports
```

### Key Files

- **`main.py`**: Contains the complete FastAPI application including OAuth, API endpoints, and business logic
- **`auth/concur_oauth.py`**: Production-ready OAuth client with Azure Key Vault integration
- **`exports/excel_export.py`**: Comprehensive Excel export with template-based formatting
- **`logic/card_totals.py`**: Card transaction aggregation algorithms

---

## How It Works

### Authentication Flow

1. **Initial Setup**: Application starts with Concur refresh token stored in Key Vault
2. **Token Request**: When an API call is needed, `ConcurOAuthClient` checks token expiry
3. **Token Refresh**: If expired, exchanges refresh token for new access token
4. **Token Caching**: Caches access token in memory with 60-second buffer before expiry
5. **Request Authorization**: Includes `Bearer {access_token}` in all Concur API calls

### Accruals Search Workflow

```
POST /api/accruals/search
  ↓
Build SCIM filter from orgUnit1-6, custom21
  ↓
Call Identity v4.1 /Users with filter
  ↓
For each user:
  ├─► Get expense reports (Expense v4)
  │   └─► Filter out P_PAID, P_PROC status
  │
  └─► Get card transactions (Cards v4)
      └─► Filter out assigned (has expenseId/reportId)
  ↓
Return aggregated JSON response
```

### Card Totals Export Workflow

```
POST /api/cardtotals/export
  ↓
Build SCIM filter from orgUnit1-6, custom21
  ↓
Call Identity v4.1 /Users with filter
  ↓
For each user:
  └─► Get card transactions in date range
  ↓
Filter by dateType (TRANSACTION|POSTED|BILLING)
  ↓
Aggregate by:
  ├─► Card Program (payment type ID)
  └─► User (employee ID or account number)
  ↓
Load Excel template
  ↓
Populate sheets with totals
  ↓
Return streaming Excel response
```

### Date Type Logic

The system supports three different date types for card transaction filtering:

- **TRANSACTION**: The date the card was swiped/used
- **POSTED**: The date the charge cleared with the bank
- **BILLING**: The date on the card statement period

This flexibility allows finance teams to align reports with their accrual methodology.

---

## API Endpoints

### 1. Accruals Search

**Endpoint**: `POST /api/accruals/search`

**Description**: Searches for unsubmitted reports and unassigned cards

**Request Body**:
```json
{
  "orgUnit1": "Engineering",
  "orgUnit2": "Platform",
  "orgUnit3": null,
  "orgUnit4": null,
  "orgUnit5": null,
  "orgUnit6": null,
  "custom21": "CC-12345"
}
```

**Response**:
```json
{
  "summary": {
    "unsubmittedReportCount": 5,
    "unassignedCardCount": 12
  },
  "unsubmittedReports": [
    {
      "lastName": "Smith",
      "firstName": "John",
      "reportName": "March Travel",
      "submitted": false,
      "reportCreationDate": "2024-03-15T10:30:00Z",
      "reportSubmissionDate": null,
      "paymentStatusId": "NOT_PAID",
      "totalAmount": 1250.50
    }
  ],
  "unassignedCards": [
    {
      "cardProgramName": "VISA_CORPORATE",
      "accountKey": "1234",
      "lastFourDigits": "5678",
      "postedAmount": 89.99
    }
  ]
}
```

### 2. Card Totals Export

**Endpoint**: `POST /api/cardtotals/export`

**Description**: Generates Excel report with card transaction totals

**Request Body**:
```json
{
  "orgUnit1": "Sales",
  "transactionDateFrom": "2024-01-01",
  "transactionDateTo": "2024-03-31",
  "dateType": "POSTED"
}
```

**Response**: Excel file download
- **Content-Type**: `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`
- **Filename**: `Concur_Card_Totals_20240329_1430.xlsx`

**Excel Structure**:
- Sheet "Card totals" with:
  - Metadata (generation time, date range)
  - Totals by card program table
  - Totals by user table

---

## Authentication & Authorization

### OAuth 2.0 Refresh Token Flow

The application uses **Company-level Refresh Token** authentication:

1. **One-time Setup**: Obtain refresh token via Concur App Center
2. **Runtime**: Application exchanges refresh token for access tokens
3. **Token Rotation**: Concur may issue new refresh tokens (handle gracefully)

### Required Concur Scopes

The application requires the following API scopes:

- `identity.user.coreentitlements.read`
- `identity.user.read`
- `expense.report.read`
- `expense.report.readwrite` (if future features add report updates)
- `cards.transaction.read`

### Token Storage

**Development** (`main.py`):
```python
CONCUR_CLIENT_ID = "<FROM_KEY_VAULT>"
CONCUR_CLIENT_SECRET = "<FROM_KEY_VAULT>"
CONCUR_REFRESH_TOKEN = "<FROM_KEY_VAULT>"
```

**Production** (`auth/concur_oauth.py`):
```python
from app.keyvault import get_secret

self.client_id = get_secret("CONCUR_CLIENT_ID")
self.client_secret = get_secret("CONCUR_CLIENT_SECRET")
self.refresh_token = get_secret("CONCUR_REFRESH_TOKEN")
```

---

## Configuration

### Environment Variables

Create a `.env` file or set environment variables:

```bash
# Concur API Configuration
CONCUR_BASE_URL=https://us2.api.concursolutions.com
CONCUR_CLIENT_ID=<your-client-id>
CONCUR_CLIENT_SECRET=<your-client-secret>
CONCUR_REFRESH_TOKEN=<your-refresh-token>

# Application Configuration
TEMPLATE_PATH=reports/accrual report.xlsx
LOG_LEVEL=INFO
```

### Data Center URLs

Concur has multiple data centers. Ensure you use the correct base URL:

- **US**: `https://us.api.concursolutions.com`
- **US2**: `https://us2.api.concursolutions.com`
- **EMEA**: `https://emea.api.concursolutions.com`
- **PSCC**: `https://usg.api.concursolutions.com` (US Government)

Check your Concur instance's data center before deployment.

### Excel Template

The application requires an Excel template file at `reports/accrual report.xlsx` with sheets:

- **"unsubnitted reports"**: Column headers at row 1
- **"Unassigned cards"**: Column headers at row 1
- **"Card totals"** (optional): Created dynamically if not present

Ensure the template file is included in your deployment package.

---

## Local Development

### Prerequisites

- Python 3.8 or higher
- pip package manager
- Access to Concur API credentials

### Setup

1. **Clone the repository**:
   ```bash
   git clone <repository-url>
   cd Accural
   ```

2. **Create virtual environment**:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure credentials**:
   - Update `main.py` lines 21-23 with your Concur credentials
   - Or set environment variables

5. **Verify template exists**:
   ```bash
   # Ensure reports/accrual report.xlsx exists
   ls reports/
   ```

### Running the Application

**Development server** (with auto-reload):
```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

**Production server**:
```bash
gunicorn main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000
```

### Testing Endpoints

**Interactive API docs**:
- Open browser to `http://localhost:8000/docs`
- FastAPI provides automatic Swagger UI

**Manual testing**:
```bash
curl -X POST http://localhost:8000/api/accruals/search \
  -H "Content-Type: application/json" \
  -d '{"orgUnit1": "Engineering"}'
```

---

## Deployment Considerations

### Platform Options

1. **Azure App Service** (Recommended)
   - Managed PaaS with built-in scaling
   - Easy integration with Azure Key Vault
   - Deployment slots for blue/green deployments

2. **Docker Container**
   - Package application with dependencies
   - Deploy to Azure Container Instances, AKS, or AWS ECS

3. **AWS Lambda + API Gateway**
   - Serverless option (requires code modifications)
   - Potentially lower costs for infrequent use

4. **Traditional VM/Server**
   - Full control over environment
   - Requires manual security hardening

### Azure App Service Deployment

#### Step 1: Create App Service

```bash
az webapp create \
  --resource-group concur-accruals-rg \
  --plan concur-accruals-plan \
  --name concur-accruals-api \
  --runtime "PYTHON:3.11"
```

#### Step 2: Configure Key Vault

```bash
# Create Key Vault
az keyvault create \
  --name concur-secrets-kv \
  --resource-group concur-accruals-rg \
  --location eastus

# Add secrets
az keyvault secret set --vault-name concur-secrets-kv \
  --name CONCUR-CLIENT-ID --value "<client-id>"

az keyvault secret set --vault-name concur-secrets-kv \
  --name CONCUR-CLIENT-SECRET --value "<client-secret>"

az keyvault secret set --vault-name concur-secrets-kv \
  --name CONCUR-REFRESH-TOKEN --value "<refresh-token>"

az keyvault secret set --vault-name concur-secrets-kv \
  --name CONCUR-BASE-URL --value "https://us2.api.concursolutions.com"
```

#### Step 3: Enable Managed Identity

```bash
az webapp identity assign \
  --name concur-accruals-api \
  --resource-group concur-accruals-rg

# Get the principal ID
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

#### Step 4: Configure Startup Command

In App Service Configuration → GeneralSettings:
```bash
gunicorn main:app --workers 4 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000
```

#### Step 5: Deploy Application

```bash
# Zip deployment
zip -r app.zip . -x "*.git*" -x "venv/*" -x "__pycache__/*"

az webapp deployment source config-zip \
  --resource-group concur-accruals-rg \
  --name concur-accruals-api \
  --src app.zip
```

### Docker Deployment

Create `Dockerfile`:

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Expose port
EXPOSE 8000

# Run with gunicorn
CMD ["gunicorn", "main:app", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8000"]
```

Build and run:
```bash
docker build -t concur-accruals-api .
docker run -p 8000:8000 \
  -e CONCUR_BASE_URL="https://us2.api.concursolutions.com" \
  -e CONCUR_CLIENT_ID="..." \
  -e CONCUR_CLIENT_SECRET="..." \
  -e CONCUR_REFRESH_TOKEN="..." \
  concur-accruals-api
```

### Database Considerations

**Current State**: The application is stateless and doesn't require a database.

**Future Enhancements** might include:
- **Caching layer** (Redis) for user lookups
- **Audit logging** (PostgreSQL/MongoDB) for compliance
- **Report history** tracking

If adding a database:
- Use connection pooling (SQLAlchemy, asyncpg)
- Store connection strings in Key Vault
- Implement proper migration strategy (Alembic)

---

## Security Best Practices

### 1. Secret Management

**NEVER** commit secrets to version control:
- Use `.gitignore` to exclude `.env` files
- Store secrets in Azure Key Vault, AWS Secrets Manager, or similar
- Rotate secrets regularly (quarterly recommended)

### 2. Network Security

**Production deployments should**:
- Use HTTPS only (TLS 1.2+)
- Implement IP whitelisting if possible
- Use Azure Private Link for PaaS services
- Enable Web Application Firewall (WAF)

### 3. Authentication & Authorization

**API-level security**:
- Add API key authentication to FastAPI endpoints
- Implement role-based access control (RBAC)
- Log all API access for audit trails

Example API key middleware:
```python
from fastapi import Security, HTTPException
from fastapi.security import APIKeyHeader

API_KEY_HEADER = APIKeyHeader(name="X-API-Key")

def verify_api_key(api_key: str = Security(API_KEY_HEADER)):
    if api_key != os.getenv("INTERNAL_API_KEY"):
        raise HTTPException(status_code=403, detail="Invalid API key")
```

### 4. Input Validation

**Pydantic models** provide validation, but also:
- Validate date formats strictly
- Sanitize filter expressions to prevent injection
- Limit array sizes to prevent DoS attacks

### 5. Error Handling

**Do NOT expose**:
- Internal stack traces to clients
- Database/API connection details
- Secret values in logs or responses

Implement custom exception handlers:
```python
@app.exception_handler(Exception)
async def generic_exception_handler(request, exc):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"}
    )
```

---

## Monitoring & Logging

### Application Insights (Azure)

Integrate with Azure Application Insights:

```python
from opencensus.ext.azure.log_exporter import AzureLogHandler
import logging

logger = logging.getLogger(__name__)
logger.addHandler(AzureLogHandler(
    connection_string='InstrumentationKey=<your-key>'
))
```

**Key metrics to track**:
- Request latency (p50, p95, p99)
- Error rates by endpoint
- Concur API call durations
- Token refresh frequency
- User count per search request

### Logging Best Practices

```python
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)

# Good logging examples
logger.info(f"Processing accruals search for {len(users)} users")
logger.warning(f"Token refresh failed, retrying... (attempt {retry_count})")
logger.error(f"Concur API error: {resp.status_code}", exc_info=True)
```

**DO NOT log**:
- Access tokens or refresh tokens
- Client secrets
- Personally identifiable information (PII) unless necessary

### Health Checks

Add a health endpoint:

```python
@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "version": "1.0.0"
    }
```

Configure liveness/readiness probes in Kubernetes:
```yaml
livenessProbe:
  httpGet:
    path: /health
    port: 8000
  initialDelaySeconds: 30
  periodSeconds: 10
```

---

## Troubleshooting

### Common Issues

#### 1. OAuth Token Errors

**Error**: `401 Unauthorized` from Concur API

**Solutions**:
- Verify refresh token hasn't expired (Concur tokens expire after 6 months of inactivity)
- Check client ID/secret are correct
- Ensure correct data center URL
- Request new refresh token from Concur App Center

#### 2. SCIM Filter Failures

**Error**: `400 Bad Request` on Identity API

**Solutions**:
- Validate filter syntax (must be SCIM v2 compliant)
- Escape special characters in filter values
- Ensure custom field IDs match your Concur configuration
- Test filters in Concur's API Explorer

#### 3. Excel Export Issues

**Error**: `FileNotFoundError: reports/accrual report.xlsx`

**Solutions**:
- Verify template file is in deployment package
- Check file path is relative to application root
- Ensure proper file permissions

**Error**: `Invalid sheet name`

**Solutions**:
- Verify template has required sheets with exact names
- Check for typos ("unsubnitted reports" is intentional per template)

#### 4. Performance Problems

**Symptom**: Slow response times for large user sets

**Solutions**:
- Implement pagination for user search
- Use asyncio for parallel API calls
- Add Redis caching for user data
- Limit date ranges for card transactions

#### 5. Memory Issues

**Symptom**: Container/app crashes with large exports

**Solutions**:
- Stream Excel generation instead of loading in memory
- Limit concurrent request processing
- Increase worker memory allocation
- Implement request throttling

### Debug Mode

Enable verbose logging:

```python
# In main.py
import logging
logging.basicConfig(level=logging.DEBUG)

# Add request/response logging
@app.middleware("http")
async def log_requests(request, call_next):
    logger.debug(f"Request: {request.method} {request.url}")
    response = await call_next(request)
    logger.debug(f"Response: {response.status_code}")
    return response
```

---

## API Rate Limits

### Concur API Limits

SAP Concur enforces rate limits per API:

- **Identity API**: 200 requests/minute
- **Expense Reports API**: 100 requests/minute per user
- **Cards API**: 100 requests/minute per user

### Mitigation Strategies

1. **Implement exponential backoff**:
```python
import time

def call_with_retry(func, max_retries=3):
    for attempt in range(max_retries):
        try:
            return func()
        except requests.HTTPError as e:
            if e.response.status_code == 429:
                wait_time = 2 ** attempt
                logger.warning(f"Rate limited, waiting {wait_time}s")
                time.sleep(wait_time)
            else:
                raise
    raise Exception("Max retries exceeded")
```

2. **Batch user processing**:
   - Process users in chunks of 10-20
   - Add small delays between batches

3. **Response caching**:
   - Cache user lookups for 5-10 minutes
   - Cache card transactions for 1 hour

4. **Monitor rate limit headers**:
```python
resp = requests.get(url)
remaining = resp.headers.get('X-RateLimit-Remaining')
if int(remaining) < 10:
    logger.warning(f"Approaching rate limit: {remaining} remaining")
```

---

## Future Enhancements

### Planned Features

1. **Async API Calls**
   - Convert to FastAPI async/await
   - Use `httpx` for concurrent Concur API calls
   - Reduce response times by 50-70%

2. **Advanced Filtering**
   - Support date ranges for unsubmitted reports
   - Add amount thresholds
   - Filter by approval status

3. **Scheduled Reports**
   - Background job scheduler (Celery/APScheduler)
   - Email report distribution
   - Recurring export to SharePoint/OneDrive

4. **Dashboard UI**
   - React/Vue.js frontend
   - Interactive charts (Chart.js)
   - Real-time accrual metrics

5. **Webhook Support**
   - Receive Concur event notifications
   - Auto-process new expense submissions
   - Alert on high-value unassigned cards

6. **Multi-tenant Support**
   - Support multiple Concur entities
   - Tenant-specific configurations
   - Separate data isolation

### Code Refactoring Opportunities

1. **Service Layer Extraction**
   - Move all Concur API calls to `services/`
   - Create proper service classes
   - Implement dependency injection

2. **Configuration Management**
   - Use Pydantic `BaseSettings` for config
   - Support multiple environments (dev/staging/prod)
   - Externalize all magic strings

3. **Testing Suite**
   - Unit tests with pytest
   - Mock Concur API responses
   - Integration tests with test credentials
   - 80%+ code coverage target

4. **API Versioning**
   - Implement `/v1/` prefix
   - Support backward compatibility
   - Deprecation warnings

---

## Support & Contributing

### Getting Help

For issues or questions:
1. Check this README thoroughly
2. Review Concur API documentation: https://developer.concur.com
3. Contact your Concur TMC or account representative
4. Review application logs for error details

### Contributing

If extending this application:
1. Follow PEP 8 style guidelines
2. Add type hints to all functions
3. Update this README with new features
4. Write tests for new functionality

### API Documentation

**Concur Developer Resources**:
- Developer Center: https://developer.concur.com
- API Reference: https://developer.concur.com/api-reference/
- Authentication Guide: https://developer.concur.com/api-reference/authentication/

---

## Appendix

### Sample SCIM Filters

**Filter by single org unit**:
```
urn:ietf:params:scim:schemas:extension:spend:2.0:User:orgUnit1 eq "Engineering"
```

**Filter by multiple org units**:
```
urn:ietf:params:scim:schemas:extension:spend:2.0:User:orgUnit1 eq "Engineering" and urn:ietf:params:scim:schemas:extension:spend:2.0:User:orgUnit2 eq "Platform"
```

**Filter by custom field**:
```
urn:ietf:params:scim:schemas:extension:spend:2.0:User:customData[id eq "custom21" and value eq "CC-12345"]
```

### Date Format Reference

All dates in Concur API responses use **ISO 8601 format**:
- `2024-03-29T14:30:00Z` (UTC)
- `2024-03-29T14:30:00-05:00` (with timezone offset)

For request parameters, use:
- `YYYY-MM-DD` for date-only fields (e.g., `2024-03-29`)

### Excel Template Specifications

Required sheets and columns:

**Sheet: "unsubnitted reports"**
| Column | Header | Data Type |
|--------|--------|-----------|
| A | Last Name | String |
| B | First Name | String |
| C | Report Name | String |
| D | Submitted | Boolean |
| E | Creation Date | DateTime |
| F | Submission Date | DateTime |
| G | Total Amount | Number |

**Sheet: "Unassigned cards"**
| Column | Header | Data Type |
|--------|--------|-----------|
| A | Card Program | String |
| B | Account Key | String |
| C | Last Four Digits | String |
| D | Posted Amount | Number |

---

## License

*Add your organization's license information here*

---

## Version History

- **v1.0.0** (2024-03-29): Initial production release
  - Accruals search endpoint
  - Card totals export endpoint
  - Azure Key Vault integration
  - Excel template support

---

**Document Version**: 1.0  
**Last Updated**: 2024-12-29  
**Maintained By**: [Your Team Name]
