---
name: azure-finops-review
description: End-to-end Azure FinOps review of a tenant or subscription set — explain a cost increase or spike, find and verify savings, build a prioritized savings roadmap, check that applied savings show up in billing, produce month-end forecasts. Every run also produces a self-contained HTML cost dashboard automatically, and offers an Excel workbook that compares 1, 2 or 3 years and forecasts the next three. Uses Azure Cost Management, Resource Graph, Log Analytics and Monitor read-only via the Azure CLI. Use this skill whenever the user asks why their Azure bill went up, how much they spend, where they can save, wants a cost forecast or month-close estimate, a year-over-year comparison, a multi-year estimate for a budget or a contract negotiation, a cost dashboard, Excel or report to share, asks to check whether savings are working, mentions Sentinel/Log Analytics ingestion costs, idle or orphaned resources, reservations or savings plans, or wants a FinOps report — even if they don't say "FinOps".
hooks:
  PreToolUse:
    - matcher: "Bash|PowerShell"
      hooks:
        - type: command
          timeout: 15
          # Read-only guard. Finds hooks/readonly_guard.py in the usual skill locations
          # (${CLAUDE_SKILL_DIR} is not expanded in hook commands). If the script is
          # missing, it still blocks Azure / IaC / Kubernetes commands (fail closed).
          command: >-
            IN=$(cat); PY=python3; $PY -c 1 >/dev/null 2>&1 || PY=python;
            for f in "$CLAUDE_PROJECT_DIR/.claude/skills/azure-finops-review/hooks/readonly_guard.py"
            "$HOME/.claude/skills/azure-finops-review/hooks/readonly_guard.py"
            "$USERPROFILE/.claude/skills/azure-finops-review/hooks/readonly_guard.py";
            do if [ -f "$f" ]; then printf '%s' "$IN" | $PY "$f"; exit $?; fi; done;
            if printf '%s' "$IN" | grep -qiE '(^|[^a-z])(az|terraform|tofu|kubectl|helm|azd)([ .]|$)|-Az[A-Z]';
            then echo "azure-finops-review: read-only guard script not found; Azure/IaC command blocked" >&2; exit 2; fi; exit 0
compatibility: Azure CLI (`az`, logged in, with resource-graph and log-analytics extensions), Python 3.9+. Read access (Cost Management Reader + Reader + Log Analytics Reader) on the scope under review. Works with any agent that can run shell commands.
---

# Azure FinOps review

A FinOps review answers four questions, in this order:

1. **What are we paying, and what changed?** (baseline + spike root cause)
2. **Where can we save, and how sure are we?** (verified findings, not guesses)
3. **What do we do, in what order, and who must approve?** (roadmap)
4. **Did it work, and where will we land?** (measurement + forecast)

Every run ends with a **dashboard** (one HTML file, see "Dashboard" in section 7), produced automatically, and with an **offer** to build the Excel comparison for 1, 2 or 3 years (see "Comparison workbook" in section 7).

Users often only ask one of these. Answer the one asked, but keep the others in mind — a forecast is meaningless without knowing which savings are already in the run rate, and a savings list is useless without evidence.

## Read-only, always

This skill **only reads**. During a review you never create, change, delete, stop, start, scale or reconfigure anything in Azure — not even "temporarily", not even to gather evidence, not even if the user is Owner. A FinOps review that causes an outage or a surprise change loses all credibility, and the person running it may not be the one accountable for the resource.

Allowed (read) commands:

- `az account list`, `az account show`, `az account get-access-token`, `az account management-group list/show`
- any `... list`, `... show`, `az graph query`, `az monitor metrics list`, `az monitor activity-log list`, `az monitor log-analytics query`, `az monitor diagnostic-settings list/show`, `az consumption ... list`, `az reservations ... list/show`, `az advisor recommendation list`
- `az rest --method get ...`
- `POST .../providers/Microsoft.CostManagement/query` and `.../forecast` (POST, but they only read) — this is what `scripts/finops_cost.py` does; the script has a guard that refuses anything else.
- `git log/show/grep` and file reads in IaC repos.

Not allowed during the review:

- any `create`, `update`, `set`, `delete`, `purge`, `start`, `stop`, `deallocate`, `restart`, `scale`, `resize`, `upgrade`, `enable`, `disable`, `assign`, `lock`, `tag` operation; `az rest` with PUT/PATCH/DELETE, or POST to anything but the two Cost Management read endpoints; Terraform `apply`/`import`/`state rm`; `kubectl apply/delete/scale`.
- **`az account set`** — it changes the user's default subscription for every other terminal and tool. Pass `--subscription <id>` on each command instead.
- **Turning on diagnostic/resource logs** to see who uses a resource — that is a change (and it costs money). Recommend it as an action for the user.
- **Granting yourself a role.** If access is missing, say what is missing and let the user decide.
- Installing az extensions or changing `az config` — ask the user first (it changes their machine, not Azure, but it is still theirs).

When a change is warranted, it becomes a **recommendation** in the report. It is executed later, only after the user explicitly approves that specific item, preferably through their Infrastructure-as-Code, and destructive steps are run by the human (see "Executing changes"). If you are unsure whether a command writes, don't run it — look it up or ask.

In Claude Code this is also enforced: `hooks/readonly_guard.py` runs before every shell command and denies write operations (az, Az PowerShell, az rest, Terraform, kubectl, helm). If a command is denied, **do not retry it another way** (a different tool, a script, a variable) — record the change as a recommendation. The guard reads command text, so it is a safety net; the rule above is what you follow.

## 0. Scope and access

Before any query, settle:

- **Scope**: which tenant and which subscriptions. `az account list` can return subscriptions from *several tenants* (guest access, other companies in a group). Filter by `tenantId` and confirm the list with the user; mixing tenants silently produces absurd totals.
- **Permissions**: Cost Management Reader + Reader on the subscriptions, Log Analytics Reader on workspaces. If something is denied, report what is missing; the user decides whether to grant it (and should note a follow-up to remove elevated access afterwards). Never grant it yourself.
- **Budgets live at several scopes**: subscription, resource group *and management group* (a tenant-wide budget usually sits on the root or a top management group). `az consumption budget list` only shows the subscription scope — also list management groups and read their budgets (`queries.md`, Q-BUD). Never write "there is no budget" before checking all three. A budget's `forecastSpend` is Azure's own month-close forecast — a free cross-check for yours.
- **Previous reports**: earlier FinOps reports or notes in the working folder are useful for context (change dates, what was already done), but every number you report must be recomputed from live data. Say which ones you reused.
- **Billing account (EA/MCA)**: subscription-scope ActualCost works without billing access, but reservation purchases, price sheets and amortized views at billing scope need billing-account roles. Say what you could not see.
- **Currency** and **cost type**: use ActualCost for "what is on the invoice", AmortizedCost to spread reservation purchases. State which one you used.

## 1. Baseline — what are we paying

Run `scripts/finops_cost.py` (see `--help`). Typical sequence:

```bash
# Monthly totals per subscription for the last 3-4 months
python scripts/finops_cost.py --tenant <tenantId> --from 2026-07-01 --to 2026-10-31 --granularity Monthly --by subscription

# Daily totals for the current + previous month, with weekday/weekend split
python scripts/finops_cost.py --tenant <tenantId> --from 2026-10-01 --to 2026-11-16 --by subscription

# Service breakdown, then drill into one service by resource and meter
python scripts/finops_cost.py --tenant <tenantId> --from ... --to ... --group ServiceName --top 25
python scripts/finops_cost.py --subscriptions <id> --from ... --to ... --group ResourceId,Meter --service "Sentinel"
```

Use the script rather than writing your own REST helper: it handles the multi-tenant filter, paging, Cost Management throttling (HTTP 429, honouring the retry headers) and late meters. Expect 30 s to a few minutes for several subscriptions — throttling pauses are normal, wait for them. If a subscription still fails, the script **stops with "INCOMPLETE DATA"** rather than printing totals that are silently too low; re-run it, or pass `--allow-partial` and say in the answer which subscription is missing. Only fall back to your own calls if the script itself is broken, and say so. For long windows grouped by `ResourceId`, use `--granularity Monthly` (daily rows can run to tens of thousands).

Read the daily series before drawing conclusions:

- **One-off days** (one day 2-3x the others) are usually reservation/savings-plan purchases or marketplace charges, billed on the same day each month. Exclude them from run rates and add them back once in forecasts.
- **Weekday vs weekend** rates differ (dev/test shutdown schedules, traffic-driven logging). Never average them together.
- **Late meters**: Sentinel, Log Analytics, Azure DevOps and some others post 24-48 h late. The last 1-2 days are always incomplete; treat yesterday as partial and the day before as "almost final" (+ a few currency units).

## 2. Root cause of a cost increase

Go top-down and compare a "before" window with an "after" window of the same weekday mix:

subscription → service → resource group → resource → meter

Then confirm the cause *inside* the resource, not just in billing. Common patterns and how to prove them are in `references/checks.md` (Log Analytics/Sentinel ingestion is the most frequent culprit; the KQL is in `references/queries.md`).

Find the change event and **who made it**: Activity Log (`caller`, `operationName`, timestamp), IaC commit history, pipeline runs, diagnostic-setting changes around the start date. `az monitor activity-log list` returns only **50 events by default** — always pass `--max-events` (e.g. 5000) and a `--start-time/--end-time` window, or the change you are looking for will not be in the output (queries.md, Q-ACT). A caller that is a service principal means a pipeline/IaC; a person means a manual change that the next deploy may revert. Also look for automation that will undo it (start/stop schedules, autoscale, runbooks, Logic Apps).

Confirm the **current state** directly before stating it (e.g. `az aks show --query "{power:powerState.code, pools:agentPoolProfiles[].{n:name,count:count,min:minCount,max:maxCount,power:powerState.code}}"`, VM power state). Billing tells you what was charged, not what is running now.

**Moved is not saved.** When a cost disappears from one place (a log stream, a node pool, a resource group), check where it went before calling it a saving: diagnostic settings re-pointed to another workspace, workloads rescheduled onto another pool, a resource re-created in another subscription. Report the **net** effect across all subscriptions in scope. Look for offsetting increases in the same window (e.g. an extra node added the day before a cluster was stopped).

A root cause is "table X from resource Y went from A to B GB/day on date D because of change C made by W", not "logs got more expensive".

## 3. Finding savings

Work through `references/checks.md` category by category. For each candidate, record a finding with:

| Field | Meaning |
|---|---|
| id | short code, e.g. OBS-01, STO-03 |
| resource(s) | exact names/IDs |
| evidence | the query/metric that proves it (numbers, dates) |
| saving | per month, as a range (low-high) |
| confidence | **verified** (measured cost of the exact resource + proof of non-use), **estimated** (price x quantity), **possible** (depends on a decision or a design change) |
| risk / impact | what breaks if wrong; who uses it |
| owner / approval | who must agree (app team, SOC, DBA, security, management) |
| how | IaC change, portal/CLI change, or decision |

Rules that prevent the most common mistakes:

- **"Unused" needs proof of non-use**, not absence of evidence. Check metrics over 30+ days, connections/transactions, flow logs or diagnostic logs for client IPs, and ask who owns it. A storage account no workload in your clusters references may still be polled by an on-premises VM over VPN.
- **Check IaC ownership before recommending deletion.** Search the IaC repos (Terraform/Bicep/ARM, GitOps manifests like Flux/Argo) for the resource name. If it is managed there, the change must go through a PR, or the next deploy will recreate it (or fail).
- **Check dependencies**: locks (CanNotDelete from backup vaults), private endpoints, DNS links, role assignments, alerts and diagnostic settings that reference it, backup items.
- **Don't double count**: a resource deleted in one finding can't also be right-sized in another; reservations reduce the base that other findings apply to.
- **Compute prices from billing**, not list prices, when billing data exists (negotiated EA prices differ).
- **Check reservation / savings-plan coverage before claiming a compute saving.** If the VMs or nodes you would stop or shrink are covered by a reservation, stopping them saves nothing (the reservation is paid anyway) until the reserved capacity is used elsewhere or exchanged. Compare the resource's PricingModel/ChargeType in billing (`--group ResourceId,PricingModel`) and the reservation's utilisation. Conversely, losing coverage (resizing to another SKU) can *raise* cost.
- **Monitor metrics with dimension filters return only the top 10 series by default** — use `--top 200` or you will see zeros for real usage.

## 4. Roadmap

Group findings into waves so the user can act:

1. **Now** — verified, low risk, reversible (diagnostic settings, idle features, oversized quotas).
2. **30 days** — verified but needs an owner's OK or a maintenance window.
3. **30-90 days** — right-sizing, tier changes, architecture tweaks.
4. **Commitments** — reservations/savings plans, sized on the *post-cleanup* run rate, never before.
5. **Strategic** — decommissions, re-platforming, data retention policy.
6. **Hygiene/governance** — tags, budgets, anomaly alerts, policy; usually no direct saving.

Also list **guardrails** (what not to cut: security logging the SOC needs, backups, DR, production redundancy) — this keeps the review credible.

## 5. Executing changes

This section is *not* part of the review. It applies only after the user has read the findings and explicitly approved a specific item ("yes, do STO-03"). Approval for one item doesn't extend to others. Even then:

- Prefer an **IaC pull request** (one change per PR, plan output reviewed, clear rollback). Respect the repo's PR template, merge strategy and size limits; don't enable auto-complete/merge on the user's behalf.
- Watch module quirks: some modules ignore changes to certain attributes (e.g. diagnostic-setting log categories), so a plan can show "no changes" — renaming the resource or adjusting the module may be needed. Read the plan, don't assume.
- For things outside IaC, write a **script** the user runs themselves, with a backup step (export the resource JSON first), a before/after print, and the undo command in comments. Destructive commands should be run by the human.
- Record date/time (UTC) of each change: you need it to measure the effect.

## 6. Measuring and forecasting

After changes, measure with full days only:

- Compare the same weekday types before/after; report the delta per service so each action's effect is visible.
- Wait for late meters before declaring a day's total.
- Re-check log ingestion directly (GB/day per table) — billing lags, `Usage` doesn't.

Forecast with the script:

```bash
python scripts/finops_cost.py --tenant <tenantId> --forecast 2026-11 --rate-days 10
```

It computes: actual month-to-date (complete days) + remaining weekdays x weekday rate + remaining weekend days x weekend rate + known one-off charges not yet billed (e.g. a monthly reservation charge, `--one-off 500@1`). Report the forecast as a **point estimate with a range** (low = quiet-day rate, high = busy-day rate) and state the assumptions. When a later check moves the number, say so explicitly and explain why (e.g. "the baseline days were unusually quiet") rather than silently replacing it.

Also compare against the budget and the previous months, and give the annualized run rate.

## 7. Reporting

Use `references/report-template.md`. Keep the executive summary to numbers a manager can repeat: bill before, run rate now, savings in place (monthly + yearly + %), month close, next-month forecast, remaining potential, top risks. Keep a **corrections** section: when an earlier statement turns out wrong, record it — it is what makes the report trustworthy.

### Dashboard (always produced)

**Every run of this skill ends with the dashboard**, whatever was asked (a forecast, a cost check, a spike analysis, a savings review). The user should not have to ask for it. Skip it only if the user says they don't want it, or if cost data could not be read at all.

```bash
python scripts/finops_dashboard.py --tenant <tenantId> --month 2026-11 --budget 20000 --findings findings.json --out finops-dashboard-2026-11.html
```

It writes **one self-contained HTML file** (no server, no internet, works offline and by email): forecast with range and budget, daily cost (actual / still posting / forecast), cumulative spend against the previous month and the budget, cost by subscription and by service, biggest changes by service, and the savings roadmap. It is read-only like the cost script and uses the same forecast function, so its numbers match what you report in text.

How to fit it into the work without querying twice:

- **For "how much are we spending / where will we close" requests, run this script instead of `finops_cost.py --forecast`.** One data pull gives both: it prints the forecast, run rates, previous month and top services to the console, and writes the dashboard. Expect a few minutes on several subscriptions (throttling pauses are normal).
- For deeper work (root cause, savings), do the analysis first, then build the dashboard last so it can include the findings.
- `--budget`: pass the amount you found with Q-BUD (the script does not read budgets itself). Leave it out if there is no budget.
- `--one-off AMOUNT@DAY`: known monthly charges, same as the forecast.
- `--findings findings.json`: your findings from section 3, as a list of `{"id", "title", "saving_low", "saving_high", "confidence": "verified|estimated|possible", "wave", "owner", "status": "open|done"}`. Write this file yourself from the review; leave it out if you did not look for savings.
- `--save-data data.json` keeps the numbers; `--data data.json` re-renders without calling Azure (e.g. to add findings or `--anonymize` afterwards).
- `--anonymize`: replaces subscription names with "Subscription 1..n". Use it when the user wants to share the dashboard outside the team or publicly. Resource names you wrote into findings are *not* detected automatically: write findings without resource names for an anonymised dashboard.
- `--demo` renders synthetic data, to show the layout without any tenant access.

Write the file to the user's working folder, tell them the full path, and say in one line what it contains. The file holds the tenant's cost data and (unless anonymised) subscription names: never upload, publish or commit it, and never put real data from it into a repository or a public place. The user decides who gets it.

### Comparison workbook (Excel, always offered)

**Every analysis ends with an offer**: after you give the answer and the dashboard, ask the user in one short question whether they also want the Excel comparison, and **for how many years**:

- **1 year**: the current year only, month by month, with the forecast to December.
- **2 years**: the current year against the previous one.
- **3 years**: the last three years side by side.

Build it only after a yes. Do not ask again in the same session once the user has answered. If the user already asked for a **comparison across years**, a **yearly or multi-year forecast**, numbers for a **budget** or a **contract / discount negotiation**, or simply "an Excel", skip the offer and build it; ask only for the number of years if they did not give it (default 3). Then run:

```bash
python scripts/finops_workbook.py --tenant <tenantId> --years 3 --one-off 500@13:Production \
  --scenario "Main:19000:10,15,20" --out azure-cost-2024-2026.xlsx
```

It writes one `.xlsx` with live formulas: the years side by side, every month compared, cost by subscription, the forecast for the rest of the current year, and scenarios for the next three years. It is read-only like the other scripts and prints the same totals to the console, so you can report them without opening the file. Unlike the dashboard it is not built automatically (it takes extra queries, one per subscription per year): it is offered, and built on a yes.

- Needs the `openpyxl` package. If the script says it is missing, tell the user (`pip install openpyxl`); do not install packages without asking.
- `--years N`: the number the user chose (1, 2 or 3; up to 6 is possible), ending with the current year. Older years are only available if the subscriptions existed and cost data is retained.
- `--one-off AMOUNT@DAY:SUBSCRIPTION NAME`: monthly charges (reservations, marketplace), found as one-off days in section 1. Without a subscription name the amount goes into a "Not split" column.
- `--scenario "NAME:BASE:G1,G2,G3"` (repeatable): monthly base cost and growth % for each of the next three years, compounded. **Growth rates are the user's assumptions, not something you can measure**: ask for them, or show what the history supports (the year-on-year changes in the workbook) and let the user choose. Without `--scenario` the sheet uses a 5% placeholder and says so; never present that as a prediction. The sheet always adds a "minimum" scenario (today's consumption, no growth).
- `--anonymize` replaces subscription names; `--demo` writes synthetic data without any tenant access.
- The forecast months use averaged run rates. If something was switched off in the last few days, the average still contains the old cost: say so, or correct the blue rate cells on the "Forecast inputs" sheet.

Same rule as the dashboard: the file holds the tenant's cost data, so never upload, publish or commit it.

## Pitfalls checklist

- Any command that writes → don't (see "Read-only, always"); `az account set` counts.
- Multi-tenant CLI context → filter by tenantId.
- "No budget" after checking only the subscription → check management groups too.
- Activity Log without `--max-events` → only 50 events, change missed.
- Resource state taken from billing or old notes → check the live state.
- Cost "saved" that only moved (other workspace, pool, subscription) → report the net.
- Scale-down of reserved compute → no saving while the reservation is paid.
- Multi-year scenario presented as a prediction → growth rates are assumptions; name whose they are.
- Finished without offering the Excel comparison (and asking how many years) → offer it once.
- Finished without the dashboard → build it; it is part of every run.
- Dashboard with real data shared, uploaded or committed → never; use `--anonymize` or `--demo` for anything public.
- Script stopped with INCOMPLETE DATA → re-run; never report totals with a missing subscription.
- Partial last day / late meters → exclude or flag.
- Reservation purchase day → exclude from run rate, add back once.
- Weekday/weekend mixing → separate rates.
- Quiet days chosen as baseline → use at least a full week of weekdays.
- Truncated command output (`head`, top-N defaults) hiding a match → re-run without truncation before concluding "not found".
- Service names in billing differ from product names (e.g. SFTP is billed as "SSH File Transfer Protocol"; Log Analytics and Sentinel are separate meters on the same workspace).
- Windows Git Bash: set `MSYS_NO_PATHCONV=1` before `az` calls with resource IDs, and strip `\r` from `-o tsv` output.
- Basic/Auxiliary log tables don't answer normal queries — use the search job API or `Usage` table.
