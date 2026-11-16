# Savings checks by category

For each check: **signal** (how to spot it), **proof** (what makes it verified), **typical action**, **watch out**.
Queries referenced here are in `queries.md`.

## Contents
1. Logs: Log Analytics, Sentinel, diagnostics
2. Compute: VMs, AKS, App Service, Functions
3. Storage
4. Networking
5. Databases and caches
6. PaaS / integration / AI
7. Commitments: reservations and savings plans
8. Orphans and leftovers
9. Governance

## 1. Logs (most frequent cause of spikes)

- **Duplicate or verbose diagnostic categories**
  - Signal: workspace GB/day jumps; one `AzureDiagnostics` Category dominates.
  - Proof: `Usage` by DataType and `AzureDiagnostics` by Category/Resource (Q-LA-1, Q-LA-2), before/after a date.
  - Typical: AKS sending both `kube-audit` and `kube-audit-admin` (the second is a subset without get/list) — keep only `kube-audit-admin`; App Gateway/Firewall/MySQL audit logs at full verbosity.
  - Action: change the diagnostic setting (in IaC), or send to a cheaper destination (Basic/Auxiliary table plan, storage account, separate non-Sentinel workspace).
  - Watch out: security logs the SOC relies on — get SOC sign-off; Sentinel analytics rules that query the table.
- **Sentinel on a workspace with non-security data**: every GB costs Log Analytics + Sentinel. Move container/app logs to a separate workspace.
- **Non-prod logs at production levels**: dev/test clusters sending audit or container logs to the analytics tier. Reduce categories, use Basic logs, shorter retention.
- **Retention**: interactive retention beyond what's needed; archive tier instead.
- **Commitment tiers**: workspaces stably above 100 GB/day can use commitment tiers — only after cleanup.
- **Container Insights**: ContainerLogV2 volume; namespace filtering via ConfigMap/DCR.

## 2. Compute

- **Stopped-but-allocated or idle VMs**: CPU < 5% p95 for 30 days, power state. Deallocate, schedule, or right-size.
- **Over-provisioned VM sizes / AKS node pools**: node CPU/memory requests vs allocatable; cluster autoscaler min counts; system pools larger than needed. Check node-count changes in the Activity Log (`agentPools/write`) — an extra node added quietly can cancel another saving.
- **Non-prod running 24/7**: start/stop schedules (AKS stop, VM auto-shutdown). Check the *live* power state and any schedule (automation runbooks, Logic Apps, pipelines named like `sched-*-start`) that would restart it.
- **Reservation coverage**: before counting a saving from stopping or resizing VMs/nodes, check whether a reservation covers them (billing PricingModel = Reservation, reservation utilisation). Stopping reserved capacity saves nothing until the reservation is reused or exchanged.
- **App Service / Functions premium plans (EP1+, P-series)** with stopped or idle apps: plans bill even when apps are stopped. Check app state and requests; consumption/flex alternatives. Before deleting, inventory identities' role assignments, Key Vault access policies, alerts, diagnostic settings and content file shares.
- **Old generation SKUs** (Dv3 → Dv5/Dasv5, etc.) cost more per performance.

## 3. Storage

- **Premium file shares** bill on *provisioned* size, not used size. Compare quota vs `shareUsageBytes`; lower quota (premium allows one decrease per 24 h).
- **SFTP enabled** on storage accounts is billed per hour per account ("SSH File Transfer Protocol" meter) even with no usage. Check SFTP metrics/transactions over 30+ days (`--top 200` with dimension filters) and local users' last logon.
- **Access tiers / lifecycle**: hot data never read; missing lifecycle rules; old snapshots and soft-deleted blobs.
- **Redundancy**: GRS/GZRS on non-prod or on reproducible data.
- **Unattached managed disks** and old disk snapshots.
- Watch out: unknown clients (on-prem over VPN, partners). If existing logs/metrics can't show the callers, **recommend** (don't do it yourself — it is a change) that the owner enables resource logs (StorageRead/Write) to a workspace for 24-48 h, then analyse them with Q-LA-4.

## 4. Networking

- **Private DNS Resolver, VPN/ExpressRoute gateways, Firewall, Bastion, NAT Gateway, App Gateway** in non-prod or with no traffic: fixed hourly cost. Check metrics (queries, tunnels bytes, throughput) and IaC ownership.
- **Unused public IPs** (Standard SKU bills when unattached) and idle load balancers.
- **Private endpoints** no longer needed (each bills hourly + per GB).
- **Gateway SKU** larger than needed (bandwidth metrics vs SKU).
- **Cross-region / internet egress** spikes: by meter "Data Transfer Out".

## 5. Databases and caches

- **Cosmos DB autoscale max** far above observed peak RU/s (normalized RU consumption < 30%). Lower max; serverless for spiky low-volume. Coordinate with DBAs — throughput may be set outside IaC.
- **Azure SQL / elastic pools**: DTU/vCore utilization; serverless auto-pause for dev; pools with few DBs.
- **MySQL/PostgreSQL flexible**: right-size compute, stop non-prod, storage auto-grow surprises, audit log volume going to Log Analytics.
- **Redis**: tier/size vs used memory and connections.

## 6. PaaS / integration / AI

- **API Management**: tier vs requests; multiple instances for non-prod (Developer tier).
- **Event Hubs / Service Bus namespaces** with no messages for 30 days.
- **SignalR / Web PubSub units** vs connections.
- **Container Registry**: Premium only if geo-replication/private link needed; retention policies for untagged manifests.
- **AI / OpenAI / Foundry**: provisioned throughput unused; token-based spend spikes (check by deployment and meter).
- **Data platforms (Fabric, Synapse, Databricks, ADF)**: capacities running 24/7, pause schedules, reservations.

## 7. Commitments

- Read existing reservations first (`az reservations` or reservationSummaries at reservation-order scope) — utilisation, expiry, scope. Users often believe there are none.
- Size new reservations / savings plans on the **post-cleanup** stable baseline (min of the last 30 days of eligible compute, not the average).
- Savings plan for mixed/changing compute; reservations for stable SKUs (VMs, SQL, Cosmos, Redis, App Service, Log Analytics commitment tiers).
- Needs billing-account rights to purchase; say so.

## 8. Orphans and leftovers

Resource Graph queries (Q-RG-*) for: unattached disks, unattached public IPs, NICs without VM, empty App Service plans, empty resource groups, NSGs without subnet/NIC, orphaned snapshots, Advisor recommendations pointing to resources that no longer exist, diagnostic settings/alerts/role assignments targeting deleted resources.

After a decommission, also clean: role assignments of deleted managed identities (show as "Unknown" principals), Key Vault access policies, backup items and their alert rules, DNS records, private endpoints.

## 9. Governance (no direct saving, prevents the next spike)

- Budgets at the right scope with realistic thresholds (re-baseline after savings). Read budgets at subscription, resource-group **and management-group** scope (Q-BUD) before saying one is missing.
- Cost anomaly alerts on every subscription.
- Required tags (owner, cost center, environment) with policy; measure tag coverage by cost.
- Log ingestion caps / alerts on workspaces (daily cap for non-prod, alert on GB/day).
- Remove elevated access granted for the review.
