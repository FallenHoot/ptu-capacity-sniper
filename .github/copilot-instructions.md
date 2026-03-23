# PTU Capacity Sniper — Copilot Instructions

## Project Context

This is the **PTU Capacity Sniper** — an Azure Function App that automatically acquires Azure AI Foundry Provisioned Throughput (PTU) capacity as it becomes available. It runs on a 5-minute timer, attempting to grab freed PTU capacity using greedy increments. Works with any model available through Azure AI Foundry that supports Provisioned Throughput deployments.

## Tech Stack

- **Runtime**: Python 3.12 on Azure Functions Flex Consumption (FC1)
- **Infrastructure**: Bicep (IaC), deployed via Azure CLI
- **Authentication**: Managed Identity (system-assigned) — no passwords or service principals
- **Azure APIs**: Cognitive Services REST API (deployments), Azure Management API (config save)
- **Testing**: pytest with unittest.mock — 48 unit tests, zero Azure credentials required

## Architecture

```
function_app/
├── ptu_accumulator/    # Timer trigger (every 5 min) — core sniper logic
│   ├── ptu_accumulator.py   # Main: run_accumulator(), run_multi_region()
│   ├── region_discovery.py  # Model Capacities API + static fallback
│   └── snipe_history.py     # Blob Storage history logging
├── dashboard/          # HTTP trigger — web UI + config editor
│   └── dashboard.py         # Full inline HTML/CSS/JS, MODEL_DATA mapping
├── status/             # HTTP trigger — JSON API + simple HTML
│   └── status.py
├── host.json
└── requirements.txt

infra/
└── main.bicep          # Full deployment: Function App + Storage + RBAC

tests/
└── test_ptu_accumulator.py   # 60 unit tests
```

## Key Design Decisions

### Self-Contained HTML Pattern

The dashboard and status endpoints render ALL HTML/CSS/JS inline in the Python file. **No external imports, no static files, no CDN dependencies.** This is required because Azure Functions Flex Consumption does not reliably serve static files. Every endpoint must be a single Python function that returns a complete HTML response.

### Function-Level Auth

All HTTP endpoints use `authLevel: "function"` in function.json. The function key (`?code=...`) is captured from the request and passed through all internal links and form actions so navigation works seamlessly.

### MODEL_DATA Mapping

The dashboard embeds a static `MODEL_DATA` dictionary mapping model → SKU → zone → regions. This data comes from [MS Learn](https://learn.microsoft.com/azure/ai-services/openai/concepts/models) and drives client-side JavaScript for dynamic model/SKU/zone filtering. When Microsoft updates region availability, this mapping needs manual updating.

### Managed Identity Only

All Azure API calls use `DefaultAzureCredential()`. The Bicep template grants:

- `Cognitive Services Contributor` on the target AI Services account (for deployments)
- `Website Contributor` on itself (for config save via Management API)

### Global Mutable State

The accumulator uses module-level globals (`ACCOUNT_NAME`, `RESOURCE_GROUP`, etc.) read from environment variables. `run_multi_region()` temporarily swaps these globals when cycling through targets. This is intentional for simplicity in a single-instance function.

## Coding Standards

### Python

- **No external imports in HTTP triggers** — dashboard.py and status.py must remain self-contained. Only use `os`, `sys`, `json`, `datetime`, `traceback`, `requests`, and `azure.identity`.
- **String concatenation for HTML** — No f-strings in HTML rendering (they conflict with CSS braces). Use `+` concatenation.
- **Inline CSS classes** — Use short class names (`.c`, `.ch`, `.m`, `.mv`, etc.) to keep the response size small.
- **Error handling** — Every Azure API call must have try/except with meaningful logging. Never let an exception crash the timer trigger.
- **py_compile** — Always verify files compile before deploying: `python -m py_compile <file>`

### Azure Functions

- **Flex Consumption** — FC1 plan, single instance, 2048 MB. Cold start is 15-30s. Timer trigger keeps it warm.
- **Timer trigger** — CRON: `0 */5 * * * *` (every 5 minutes). The function name in the decorator must match the folder name.
- **function.json** — Each trigger has its own folder with `function.json` and a `scriptFile` pointing to the .py file.
- **Remote build** — Deploy with `func azure functionapp publish <name> --python`. This triggers remote build on Kudu.

### Bicep

- **SecurityControl: Ignore** tag on storage account (required for subscription policies that block shared key access).
- **allowSharedKeyAccess: true** on storage (required for Flex Consumption function runtime).
- **uniqueString(resourceGroup().id)** for resource name suffixes.

## Azure AI Foundry / Cognitive Services

### Deployments API

```
PUT/GET https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.CognitiveServices/accounts/{account}/deployments/{name}?api-version=2024-10-01-preview
```

### Key Concepts

- **PTU (Provisioned Throughput Units)**: Reserved capacity. Billed per-hour. Minimum 15 PTU for DataZone. Scale increment: 5 PTU.
- **TPM (Tokens Per Minute)**: Pay-per-use. Measured in thousands (300 = 300K TPM).
- **DataZoneProvisionedManaged**: PTU with EU/US data zone boundary. Data stays in zone.
- **GlobalProvisionedManaged**: PTU with global routing. Higher availability but no zone guarantee.
- **Standard**: Pay-per-token, regional. No reserved capacity.
- **DataZoneStandard**: Pay-per-token with data zone boundary.

### Sniper Logic

1. Quota pre-check via Usages API (skip SKUs at 100% quota)
2. Capacity pre-check via Model Capacities API (skip if 0 available)
3. For each deployment slot (0 to MAX_DEPLOYMENTS-1):
   - If exists: greedy increment (+50, +25, +15, +10, +5 — tries largest first)
   - If empty: attempt `15` PTU (minimum deployment)
4. If primary SKU got nothing: try cross-SKU fallback (DataZone → Global or vice versa)
5. If no PTU capacity landed this cycle and TPM enabled: create TPM fallback deployment
6. DataZone-aware: for DataZone/Global SKUs, only hit one region (same pool)
7. On 409: parse reason (quota_exceeded vs no_capacity) — don't retry if quota blocked
8. On any success: send Teams webhook alert
9. On target reached: alert and log "DISABLE THIS FUNCTION"
10. Reservation reminder when PTUs > 0 but no reservation in place

### HTTP Status Codes from Deployments API

- `200/201`: Success (created or updated)
- `404`: Deployment doesn't exist
- `409`: No capacity available (expected — retry next cycle)

## Deployment

```bash
# Deploy infra
az deployment group create --resource-group <rg> --template-file infra/main.bicep --parameters ...

# Deploy code
cd function_app
func azure functionapp publish <function-app-name> --python

# Get function key for dashboard access
az functionapp keys list --name <name> --resource-group <rg> --query "functionKeys.default" -o tsv
```

## Testing

```bash
cd tests
pytest test_ptu_accumulator.py -v
```

All 60 tests use `unittest.mock` to mock Azure API calls. No credentials needed.
