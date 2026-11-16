# FinOps report template

Adapt headings to the user's language. Numbers in the summary must match the tables below.

```markdown
# FinOps report — <scope> — <month year>

Scope: <tenant / subscriptions>, currency <EUR/USD>, cost type <ActualCost|AmortizedCost>.
Method: <queries used, dates, what could not be read (e.g. billing account)>.

## Executive summary
- **Bill before:** <previous month total> (<weekday rate>/day).
- **Run rate now:** <weekday>/<weekend> per day, measured <dates>.
- **Savings in place:** <low-high>/month (<yearly>, <% of before>); <how much is measured in billing vs estimated>.
- **Month close:** <actual or forecast, range>; vs budget <x%>.
- **Next month forecast:** <point> (<range>), assumptions: <...>.
- **Remaining potential:** <verified range> verified, <possible range> possible.
- **Watch items:** <top 1-3 risks>.

## Part 1 — Activities done and savings
| # | Date (UTC) | Action | Resources | How (PR/script) | Saving/month | Measured in billing |

### Measured effect in billing
<daily table before/after by subscription and by service; one-off days flagged>

### Cost check <date>
<daily table since last check, explanation of deltas, updated forecast table>

### Corrections made along the way
1. <what was stated, what is true, why it changed>

### Open follow-ups
- [ ] <date>: <action> (<owner>)

## Part 2 — Roadmap
### Wave 1 — Now … Wave 6 — Hygiene
| ID | Finding | Evidence | Saving/month | Confidence | Risk | Owner/approval | How |

### Roadmap totals (no double counting)
### Guardrails — what we do NOT cut
### Governance
### KPIs to watch
| KPI | Current | Target / alert |

## Sources
<files, queries, PRs, commits>
```

## Forecast table pattern

| | Calculation | Amount |
|---|---|---|
| Actual <1..N> | Cost Management, complete days | x |
| Remaining | <n> weekdays × <rate> + <m> weekend days × <rate> | y |
| One-off charges not yet billed | reservation on day <d> | z |
| **Month close** | | **x+y+z** (low-high) |
