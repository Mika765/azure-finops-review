# Queries

Everything here is **read-only**. Pass `--subscription <id>` on each command; never run `az account set`.
On Windows Git Bash prefix `az` calls with `MSYS_NO_PATHCONV=1` and pipe tsv output through `tr -d '\r'`.
On Windows, `az rest --url` with a URL that contains `&` breaks (az.cmd treats `&` as a command separator) — quote it for cmd (`"\"<url>\""`) or use `scripts/finops_cost.py`, which calls the API directly.

## Activity Log — who changed what (Q-ACT)

```bash
az monitor activity-log list --subscription <id> \
  --start-time 2026-11-01T00:00:00Z --end-time 2026-11-16T00:00:00Z --max-events 5000 \
  --query "[?status.value=='Succeeded' && !contains(operationName.value,'/read') && !contains(operationName.value,'listKeys')].{t:eventTimestamp, op:operationName.value, caller:caller, res:resourceId}" -o table
```
Without `--max-events` only **50** events come back. Narrow with `--resource-group` or `--resource-id`. Activity Log keeps 90 days. Write operations show the *change*; the command reading them is still read-only.

## Budgets at every scope (Q-BUD)

```bash
az consumption budget list --subscription <id> -o table          # subscription scope only
az account management-group list --query "[].{name:name, display:displayName}" -o table
az rest --method get --url "https://management.azure.com/providers/Microsoft.Management/managementGroups/<mg>/providers/Microsoft.Consumption/budgets?api-version=2023-05-01" \
  --query "value[].{name:name, amount:properties.amount, spent:properties.currentSpend.amount, forecast:properties.forecastSpend.amount, period:properties.timeGrain}"
```
Repeat for the root management group (its name is the tenant ID) and each parent of the subscriptions in scope.

## Live state (before stating "running/stopped")

```bash
az aks show -g <rg> -n <cluster> --subscription <id> \
  --query "{power:powerState.code, pools:agentPoolProfiles[].{n:name,count:count,min:minCount,max:maxCount,auto:enableAutoScaling,power:powerState.code}}"
az vm list -d --subscription <id> --query "[].{n:name, rg:resourceGroup, power:powerState, size:hardwareProfile.vmSize}" -o table
az reservations reservation-order list --query "[].{name:displayName, term:term, expiry:expiryDateTime}" -o table
```

## Log Analytics (run with `az monitor log-analytics query -w <workspace customerId> --analytics-query "<kql>"`)

Get the customerId: `az monitor log-analytics workspace show --ids <resourceId> --query customerId -o tsv`.
Find Sentinel workspaces: Resource Graph Q-RG-6.

**Q-LA-1 — billable GB/day per table (pivot by day)**
```kql
Usage
| where TimeGenerated > ago(14d) and IsBillable
| summarize gb = round(sum(Quantity) / 1024, 2) by DataType, d = format_datetime(startofday(TimeGenerated), 'MM-dd')
| evaluate pivot(d, sum(gb))
```

**Q-LA-2 — AzureDiagnostics by category and resource**
```kql
AzureDiagnostics
| where TimeGenerated > ago(7d)
| summarize gb = round(sum(_BilledSize) / 1e9, 2) by Category, Resource = tolower(Resource), d = startofday(TimeGenerated)
| summarize avg_gb_day = round(avg(gb), 2) by Category, Resource
| order by avg_gb_day desc
| take 20
```
For resource-specific tables (e.g. `AKSAuditAdmin`, `StorageBlobLogs`) use the same pattern with `_BilledSize` grouped by `_ResourceId`.

**Q-LA-3 — before/after a change date**
```kql
let change = datetime(2026-10-15);
Usage
| where TimeGenerated between (change - 7d .. change + 7d) and IsBillable
| summarize gb = sum(Quantity) / 1024 by DataType, period = iff(TimeGenerated < change, "before", "after"), d = startofday(TimeGenerated)
| summarize avg_gb_day = round(avg(gb), 2) by DataType, period
| evaluate pivot(period, sum(avg_gb_day))
```
(Avoid column names like `first`/`last` — they are reserved.)

**Q-LA-4 — who calls a storage account (only if StorageRead/Write logs are already enabled — enabling them is a change the owner must make)**
```kql
StorageFileLogs   // or StorageBlobLogs
| where TimeGenerated > ago(2d) and AccountName == "<account>"
| extend ip = tostring(split(CallerIpAddress, ":")[0])
| summarize ops = count(), first_seen = min(TimeGenerated), last_seen = max(TimeGenerated),
            operations = make_set(OperationName, 10), auth = make_set(AuthenticationType, 3) by ip
| order by ops desc
```

## Resource Graph (`az graph query -q "<kql>" --first 1000`)

**Q-RG-1 — unattached managed disks**
```kql
resources | where type =~ 'microsoft.compute/disks' and properties.diskState =~ 'Unattached'
| project name, resourceGroup, subscriptionId, sku = sku.name, sizeGb = properties.diskSizeGB, timeCreated = properties.timeCreated
```

**Q-RG-2 — unattached public IPs**
```kql
resources | where type =~ 'microsoft.network/publicipaddresses' and isnull(properties.ipConfiguration) and isnull(properties.natGateway)
| project name, resourceGroup, subscriptionId, sku = sku.name
```

**Q-RG-3 — App Service plans with no apps / premium plans**
```kql
resources | where type =~ 'microsoft.web/serverfarms'
| project name, resourceGroup, subscriptionId, sku = sku.name, tier = sku.tier, apps = toint(properties.numberOfSites)
| order by apps asc
```

**Q-RG-4 — storage accounts with SFTP enabled**
```kql
resources | where type =~ 'microsoft.storage/storageaccounts' and properties.isSftpEnabled == true
| project name, resourceGroup, subscriptionId
```

**Q-RG-5 — map a private IP to a NIC / VM**
```kql
resources | where type =~ 'microsoft.network/networkinterfaces'
| mv-expand c = properties.ipConfigurations
| where tostring(c.properties.privateIPAddress) == '<ip>'
| project name, resourceGroup, vm = tostring(properties.virtualMachine.id)
```
No match → check subnet ranges, AKS node ranges, VPN/on-prem address spaces (local network gateways), and VNet flow logs.

**Q-RG-6 — Sentinel-enabled workspaces**
```kql
resources | where type =~ 'microsoft.operationsmanagement/solutions' and name startswith 'SecurityInsights'
| project workspace = tostring(properties.workspaceResourceId)
```

**Q-RG-7 — diagnostic settings sending to a workspace** (one resource at a time):
`az monitor diagnostic-settings list --resource <resourceId>`; at scale use Azure Policy compliance or loop over resources of the suspect type.

## Monitor metrics

```bash
az monitor metrics list --resource <id> --metric Transactions --interval P1D \
  --start-time <iso> --end-time <iso> --aggregation Total \
  --filter "ApiName eq '*'" --top 200
```
Valid intervals: PT1M … PT12H, P1D (not P30D). Without `--top`, dimension splits return only 10 series.

## Cost Management REST (what the script wraps)

`POST /subscriptions/{id}/providers/Microsoft.CostManagement/query?api-version=2023-11-01`

```json
{"type":"ActualCost","timeframe":"Custom",
 "timePeriod":{"from":"2026-10-01T00:00:00Z","to":"2026-10-31T23:59:59Z"},
 "dataset":{"granularity":"Daily",
   "aggregation":{"c":{"name":"Cost","function":"Sum"}},
   "grouping":[{"type":"Dimension","name":"ServiceName"}],
   "filter":{"dimensions":{"name":"ServiceName","operator":"In","values":["Sentinel"]}}}}
```
`filter` must sit inside `dataset` (outside it is silently ignored). Group dimensions: ServiceName, ResourceGroupName, ResourceId, Meter, MeterCategory, ResourceType, ChargeType, PricingModel. Results page via `properties.nextLink` (POST the same body to it). The API throttles hard (HTTP 429): wait for the seconds in the `x-ms-ratelimit-microsoft.costmanagement-*-retry-after` headers, query subscriptions one after another with a pause, never in parallel.
