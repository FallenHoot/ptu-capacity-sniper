# ⚡ PTU Capacity Sniper

**Automatically acquire Azure AI Foundry Provisioned Throughput (PTU) capacity as it becomes available.**

PTU capacity is scarce and released unpredictably. This tool runs as an Azure Function on a 5-minute timer, attempting to grab freed PTU capacity using greedy increments across configurable regions. When capacity is found, it's immediately claimed before others can take it.

Works with any model available through Azure AI Foundry that supports Provisioned Throughput deployments (GPT-5.x, GPT-4.x, etc.).

![Dashboard Screenshot](docs/dashboard.png)

## How It Works

```
Every 5 minutes:
  ┌──────────────────────────────────────────────┐
  │  1. Quota pre-check (Usages API)             │
  │  2. Capacity pre-check (Model Capacities API)│
  │  3. For each deployment slot:                 │
  │     • Existing: greedy +50/+25/+15/+10/+5    │
  │     • Empty: create at 15 PTU minimum         │
  │  4. If primary SKU failed:                    │
  │     • Cross-SKU fallback (DataZone ↔ Global)  │
  │  5. If no PTU at all: TPM fallback            │
  │  6. Alert via Teams on success                │
  └──────────────────────────────────────────────┘
  DataZone-aware: shared pool = one endpoint only.
  Stop when target PTUs reached.
```

### Strategy

| Phase                  | Action                              | Size                                             |
| ---------------------- | ----------------------------------- | ------------------------------------------------ |
| **Snipe**              | Scale existing deployments (greedy) | +50, +25, +15, +10, +5 PTU (tries largest first) |
| **Create**             | New deployment in empty slot        | 15 PTU (minimum deployment)                      |
| **Cross-SKU Fallback** | Try alternate PTU SKU               | DataZone → Global (or vice versa)                |
| **TPM Fallback**       | Regional Standard TPM               | Configurable (default 300K TPM)                  |
| **Alert**              | Teams webhook notification          | On any successful acquisition                    |

> **DataZone-aware**: DataZone and Global SKUs share one capacity pool per zone. The sniper detects this and only hits one region endpoint instead of redundantly cycling through all regions.

### Architecture

```
┌──────────────────────────────────────────────────────┐
│                  Azure Function App                   │
│               (Flex Consumption FC1)                  │
│                                                       │
│  ┌─────────────────┐  ┌──────────────┐  ┌─────────┐ │
│  │ ptu_accumulator  │  │  dashboard   │  │ status  │ │
│  │ (Timer: 5 min)   │  │ (HTTP: GET/  │  │ (HTTP:  │ │
│  │                   │  │  POST)       │  │  GET)   │ │
│  │ • Snipe PTUs     │  │ • Live view  │  │ • JSON  │ │
│  │ • Scale up       │  │ • Config UI  │  │ • HTML  │ │
│  │ • TPM fallback   │  │ • Run manual │  │         │ │
│  │ • Teams alerts   │  │ • Save config│  │         │ │
│  └────────┬─────────┘  └──────┬───────┘  └────┬────┘ │
│           │                    │                │      │
│           └────────────┬───────┘                │      │
│                        ▼                        │      │
│              Managed Identity                   │      │
│     (Cognitive Services Contributor)            │      │
└────────────────────────┬────────────────────────┘      │
                         │                                │
                         ▼                                │
            ┌─────────────────────────┐                  │
            │  Azure AI Services /    │                  │
            │  Foundry Account        │◄─────────────────┘
            │                         │
            │  Deployments:           │
            │  • gpt52-ptu-accum-0    │
            │  • gpt52-ptu-accum-1    │
            │  • gpt52-ptu-accum-2    │
            │  • gpt52-ptu-accum-3    │
            │  • gpt52-tpm-fallback   │
            └─────────────────────────┘
```

## Features

- **Model-aware region filtering** — Regions auto-filter by Model + SKU using MS Learn data
- **DataZone-aware sniping** — Knows DataZone/Global SKUs share one pool; skips redundant multi-region calls
- **Greedy increments** — Tries +50, +25, +15, +10, +5 PTU (grabs as much as possible per cycle)
- **Capacity pre-check** — Queries Model Capacities API before blind PUT attempts
- **Quota pre-check** — Queries Usages API to detect quota ceilings; skips SKUs at 100%
- **409 reason parsing** — Distinguishes quota-exceeded from no-capacity for smarter retries
- **Reservation reminders** — Alerts when PTUs are eligible for Azure Reservation discounts
- **Cross-SKU fallback** — If DataZone PTU unavailable, tries Global PTU before TPM (opt-in, disabled by default)
- **TPM fallback** — Creates a pay-per-token deployment if no PTU capacity available
- **Live dashboard** — Real-time deployment status, progress bar, config editor
- **Function-key auth** — Dashboard and API locked with Azure Functions key
- **Teams alerts** — Webhook notifications on successful PTU acquisition
- **Auto-stop** — Halts when target PTU count is reached
- **Snipe history** — Logs each cycle to Azure Blob Storage
- **Infrastructure as Code** — Full Bicep template for one-command deployment

## Quick Start

### Prerequisites

- Azure subscription with an Azure AI Foundry resource (Cognitive Services account)
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) installed
- [Azure Functions Core Tools v4](https://learn.microsoft.com/azure/azure-functions/functions-run-local) installed
- Default PTU quota on subscription (300 PTU for MCA/EA agreements — no additional approval needed)

### 1. Deploy Infrastructure

```bash
az deployment group create \
  --resource-group <your-rg> \
  --template-file infra/main.bicep \
  --parameters \
    cognitiveServicesAccountId="/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<account>" \
    targetSubscriptionId="<sub>" \
    targetResourceGroup="<rg>" \
    targetAccountName="<account>" \
    targetPtus=50 \
    modelName="gpt-5.2" \
    modelVersion="2025-12-11" \
    skuName="DataZoneProvisionedManaged"
```

This creates:

- Storage Account (for Function App)
- App Service Plan (Flex Consumption FC1)
- Function App (Python 3.12, Linux)
- RBAC: Cognitive Services Contributor on target account
- RBAC: Website Contributor on itself (for config save)

### 2. Deploy Function Code

```bash
cd function_app
func azure functionapp publish <function-app-name> --python
```

### 3. Get Your Dashboard URL

```bash
# Get the function key
az functionapp keys list \
  --name <function-app-name> \
  --resource-group <your-rg> \
  --query "functionKeys.default" -o tsv
```

Your dashboard URL:

```
https://<function-app-name>.azurewebsites.net/api/dashboard?code=<function-key>
```

### 4. Configure via Dashboard

Open the dashboard in your browser and configure:

- **Model & SKU** — Select your target model and deployment type
- **Target Regions** — Check the regions where you want to snipe capacity
- **Target PTUs** — Set your total PTU goal
- **TPM Fallback** — Enable/disable and set capacity
- **Teams Webhook** — (Optional) Get notified on success

Click **Save Configuration** — the function restarts with new settings.

## Configuration

All configuration is via Azure App Settings (environment variables), editable from the dashboard or directly in the Azure Portal.

| Variable                  | Description                                                   | Default                      |
| ------------------------- | ------------------------------------------------------------- | ---------------------------- |
| `AZURE_SUBSCRIPTION_ID`   | Target subscription                                           | _required_                   |
| `AZURE_RESOURCE_GROUP`    | Target resource group                                         | _required_                   |
| `AZURE_ACCOUNT_NAME`      | AI Services account name                                      | _required_                   |
| `PTU_MODEL_NAME`          | Model to deploy                                               | `gpt-5.2`                    |
| `PTU_MODEL_VERSION`       | Model version                                                 | `2025-12-11`                 |
| `PTU_SKU_NAME`            | Deployment SKU                                                | `DataZoneProvisionedManaged` |
| `PTU_TARGET`              | Total PTU goal                                                | `74`                         |
| `PTU_MAX_DEPLOYMENTS`     | Max parallel deployments                                      | `4`                          |
| `TPM_SKU_NAME`            | TPM fallback SKU                                              | `Standard`                   |
| `TPM_CAPACITY`            | TPM capacity in thousands                                     | `300`                        |
| `TPM_ENABLED`             | Enable TPM fallback                                           | `true`                       |
| `DATA_ZONE`               | Data zone filter (`eu`, `us`, `all`)                          | `eu`                         |
| `SELECTED_REGIONS`        | JSON array of region names                                    | _auto from model_            |
| `CROSS_SKU_FALLBACK`      | Try alternate PTU SKU if primary fails (⚠️ see warning below) | `false`                      |
| `AZURE_FUNCTION_APP_NAME` | Function app name (for config save)                           | _set by Bicep_               |
| `TEAMS_WEBHOOK_URL`       | Teams webhook for alerts                                      | _optional_                   |

> ⚠️ **Data Sovereignty Warning**: `CROSS_SKU_FALLBACK` is **disabled by default**. Enabling it may fall back from DataZone to Global SKUs, which route data **outside your EU/US data boundary**. Only enable this if your organization's compliance policy allows global data routing. DataZone SKUs guarantee data stays within the zone; Global does not.

## Supported Models & SKUs

The dashboard includes a built-in model-to-region mapping (sourced from [MS Learn](https://learn.microsoft.com/azure/ai-services/openai/concepts/models), March 2026):

| Model   | SKUs Available                                                                                       |
| ------- | ---------------------------------------------------------------------------------------------------- |
| GPT-5.4 | GlobalProvisionedManaged                                                                             |
| GPT-5.2 | DataZoneProvisionedManaged, GlobalProvisionedManaged, Standard                                       |
| GPT-5.1 | DataZoneProvisionedManaged, GlobalProvisionedManaged, DataZoneStandard, Standard                     |
| GPT-5   | DataZoneProvisionedManaged, GlobalProvisionedManaged, ProvisionedManaged, DataZoneStandard, Standard |
| GPT-4.1 | DataZoneProvisionedManaged, GlobalProvisionedManaged, ProvisionedManaged, DataZoneStandard, Standard |

When you select a model and SKU, the dashboard automatically shows only the regions where that combination is available.

## Endpoints

| Endpoint                         | Auth         | Method | Description                         |
| -------------------------------- | ------------ | ------ | ----------------------------------- |
| `/api/dashboard`                 | Function Key | GET    | Web dashboard with live status      |
| `/api/dashboard?run=true`        | Function Key | GET    | Trigger manual snipe + show results |
| `/api/dashboard`                 | Function Key | POST   | Save configuration                  |
| `/api/status`                    | Function Key | GET    | HTML status page                    |
| `/api/status?json=true`          | Function Key | GET    | JSON API                            |
| `/api/status?json=true&run=true` | Function Key | GET    | JSON API + trigger snipe            |

## Testing

```bash
# Run unit tests (60 tests, no Azure credentials needed)
cd tests
pip install pytest
pytest test_ptu_accumulator.py -v
```

Tests cover:

- Configuration validation
- URL construction
- Deployment API interactions (mocked)
- PTU accumulation logic
- Teams alerting
- Quota pre-check and blocked-SKU detection
- 409 reason parsing (quota vs capacity)
- Edge cases

## Project Structure

```
ptu-capacity-sniper/
├── README.md
├── LICENSE
├── .gitignore
├── function_app/              # Azure Function App
│   ├── host.json
│   ├── requirements.txt
│   ├── ptu_accumulator/       # Timer trigger (core sniper logic)
│   │   ├── function.json
│   │   ├── ptu_accumulator.py
│   │   ├── region_discovery.py
│   │   └── snipe_history.py
│   ├── dashboard/             # HTTP trigger (web UI + config editor)
│   │   ├── function.json
│   │   └── dashboard.py
│   └── status/                # HTTP trigger (JSON API + simple HTML)
│       ├── function.json
│       └── status.py
├── infra/                     # Infrastructure as Code
│   └── main.bicep             # Full deployment template
├── tests/                     # Unit tests
│   └── test_ptu_accumulator.py
└── docs/                      # Documentation assets
    └── dashboard.png
```

## Security

- **Managed Identity** — No passwords or service principals. The function authenticates to Azure using its system-assigned managed identity.
- **Function Key Auth** — All HTTP endpoints require a function key (`?code=...`). The key is auto-passed through all dashboard links.
- **RBAC** — The Bicep template grants exactly two roles:
  - `Cognitive Services Contributor` on the target AI Services account
  - `Website Contributor` on itself (for saving config via the Management API)
- **TLS 1.2+** — Enforced on the Function App and Storage Account
- **No Shared Key** — Storage uses connection string only for Function runtime; blob access uses the connection string from app settings.

## Cost

- **Function App (FC1 Flex Consumption)**: ~$0.20/GB execution + $0/month idle. At 5-minute intervals, expect < $5/month.
- **Storage Account (Standard LRS)**: ~$0.02/GB/month. Negligible for snipe history.
- **PTU capacity**: Billed per-hour once acquired. [See PTU pricing](https://azure.microsoft.com/pricing/details/cognitive-services/openai-service/).

> ⚠️ **Important**: PTU capacity is billed immediately once deployed. Make sure your budget and approval are in place before running the sniper with a real target.

## Troubleshooting

| Symptom                                       | Cause                                              | Fix                                                                                                 |
| --------------------------------------------- | -------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| 401 on dashboard                              | Missing or wrong function key                      | Get key: `az functionapp keys list ...`                                                             |
| "No capacity available" every cycle           | No PTU capacity free in the data zone              | Normal — DataZone/Global SKUs share one pool across regions. Keep waiting for hardware to be added. |
| "Configuration errors"                        | Missing env vars                                   | Set `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`, `AZURE_ACCOUNT_NAME`                           |
| Dashboard shows "Could not query deployments" | Managed identity missing RBAC                      | Grant `Cognitive Services Contributor` on the target account                                        |
| Config save fails                             | Missing `Website Contributor` role on function app | Run the Bicep template or grant manually                                                            |

## Contributing

1. Fork the repo
2. Create a feature branch
3. Run tests: `pytest tests/ -v`
4. Submit a PR

## License

MIT License — see [LICENSE](LICENSE).

## References

- [Azure AI Foundry Provisioned Throughput](https://learn.microsoft.com/azure/ai-services/openai/concepts/provisioned-throughput)
- [Azure AI Foundry Model Availability](https://learn.microsoft.com/azure/ai-services/openai/concepts/models)
- [Azure Functions Flex Consumption](https://learn.microsoft.com/azure/azure-functions/flex-consumption-plan)
- [Cognitive Services REST API](https://learn.microsoft.com/rest/api/cognitiveservices/)
