#!/usr/bin/env python3
"""Build a self-contained HTML cost dashboard for a FinOps review (READ-ONLY).

Reads Azure Cost Management through finops_cost.py (same read-only guard), then
writes ONE local HTML file: forecast and budget, daily cost, cumulative spend
against the previous month, cost by subscription and service, biggest changes,
and (optionally) the savings roadmap. No server, no internet, no dependencies.

Examples:
  finops_dashboard.py --tenant <tid> --month 2026-11 --budget 20000 --out dashboard.html
  finops_dashboard.py --subscriptions <id>,<id> --findings findings.json
  finops_dashboard.py --demo --out demo.html            # synthetic data, no Azure access
  finops_dashboard.py --tenant <tid> --anonymize        # subscription names replaced
  finops_dashboard.py --data saved.json --out again.html  # re-render saved data offline

findings.json (written by the reviewer, all fields optional except title):
  [{"id": "STO-01", "title": "Lower premium file share quota", "saving_low": 120,
    "saving_high": 150, "confidence": "verified|estimated|possible",
    "wave": "1 - Now", "owner": "Platform team", "status": "open|done"}]
"""
import argparse
import calendar
import collections
import datetime as dt
import json
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import finops_cost as fc  # noqa: E402

TEMPLATE = pathlib.Path(__file__).resolve().parent.parent / "assets" / "dashboard_template.html"
TOP_SERVICES = 10


def month_bounds(month):
    y, m = map(int, month.split("-"))
    return dt.date(y, m, 1), dt.date(y, m, calendar.monthrange(y, m)[1])


def prev_month(month):
    start, _ = month_bounds(month)
    p = start - dt.timedelta(days=1)
    return f"{p.year}-{p.month:02d}"


def build(records, month, complete, today, scope, currency, cost_type, rate_days=10, one_off=None,
          spike_factor=1.5, budget=None, findings=None, title="Azure cost dashboard", demo=False):
    """records: iterable of (subscription, date, service, cost). Pure function."""
    m_start, m_end = month_bounds(month)
    pm = prev_month(month)
    p_start, p_end = month_bounds(pm)
    tot = collections.defaultdict(float)
    by_sub = collections.defaultdict(lambda: collections.defaultdict(float))
    by_svc = collections.defaultdict(lambda: collections.defaultdict(float))
    for sub, d, svc, cost in records:
        tot[d] += cost
        by_sub[sub][d] += cost
        by_svc[svc or "(unassigned)"][d] += cost
    f = fc.compute_forecast(tot, month, complete, rate_days, one_off, spike_factor)
    complete = f["complete"]
    one_off_days = {}
    for o in one_off or []:
        amt, day = o.split("@")
        one_off_days[dt.date(m_start.year, m_start.month, int(day))] = float(amt)

    daily = []
    d = m_start
    while d <= m_end:
        rate = f["weekend_rate"] if d.weekday() >= 5 else f["weekday_rate"]
        est = rate + (one_off_days.get(d, 0.0) if d > complete else 0.0)
        if d <= complete:
            daily.append({"d": str(d), "v": round(tot.get(d, 0.0), 2), "kind": "actual", "oneoff": d in f["spikes"]})
        elif d <= today and d in tot:
            daily.append({"d": str(d), "v": round(tot[d], 2), "kind": "partial", "est": round(est, 2), "oneoff": False})
        else:
            daily.append({"d": str(d), "v": round(est, 2), "kind": "forecast", "oneoff": d in one_off_days})
        d += dt.timedelta(days=1)

    have_prev = any(p_start <= x <= p_end for x in tot)
    prev_daily, prev = [], {}
    if have_prev:
        d = p_start
        while d <= p_end:
            prev_daily.append({"d": str(d), "v": round(tot.get(d, 0.0), 2)})
            d += dt.timedelta(days=1)
        same_end = dt.date(p_start.year, p_start.month, min(complete.day, p_end.day))
        prev = {"month": pm, "total": round(sum(x["v"] for x in prev_daily), 2),
                "same_period": round(sum(v for x, v in tot.items() if p_start <= x <= same_end), 2)}
    else:
        same_end = None

    def split(series):
        out = []
        for name, days in series.items():
            mtd = sum(v for x, v in days.items() if m_start <= x <= complete)
            pv = sum(v for x, v in days.items() if p_start <= x <= same_end) if have_prev else None
            if mtd or pv:
                out.append({"name": name, "mtd": round(mtd, 2), "prev": None if pv is None else round(pv, 2)})
        return sorted(out, key=lambda r: -r["mtd"])

    subs = split(by_sub)
    services = split(by_svc)
    if len(services) > TOP_SERVICES:
        head, tail = services[:TOP_SERVICES], services[TOP_SERVICES:]
        head.append({"name": "Other", "mtd": round(sum(r["mtd"] for r in tail), 2),
                     "prev": round(sum(r["prev"] or 0 for r in tail), 2) if have_prev else None})
        services = head

    mtd_now = sum(v for x, v in tot.items() if m_start <= x <= m_end)
    fo = {k: round(f[k], 2) for k in ("forecast", "low", "high", "mtd", "weekday_rate", "weekday_low", "weekday_high",
                                      "weekend_rate", "weekend_low", "weekend_high", "one_offs")}
    fo.update(remaining_weekdays=f["remaining_weekdays"], remaining_weekend_days=f["remaining_weekend_days"],
              mtd_now=round(mtd_now, 2))
    return {
        "meta": {"title": title, "month": month, "generated": str(today), "currency": currency, "cost_type": cost_type,
                 "scope": scope, "complete_through": str(complete), "demo": demo, "notes": f["notes"]},
        "forecast": fo, "budget": budget, "prev": prev, "daily": daily, "prev_daily": prev_daily,
        "subs": subs, "services": services, "findings": findings or [],
    }


def demo_records(month, today):
    """Synthetic, deterministic data: no relation to any real tenant."""
    rnd = random.Random(7)
    m_start, _ = month_bounds(month)
    p_start, _ = month_bounds(prev_month(month))
    subs = {"Platform-Core": 1.0, "Production": 1.35, "Test": 0.45, "Data-AI": 0.3}
    services = {"Log Analytics": 62, "Microsoft Sentinel": 48, "Virtual Machines": 95, "Azure Kubernetes Service": 40,
                "API Management": 66, "Storage": 34, "Azure Cosmos DB": 28, "Application Gateway": 22, "Azure SQL Database": 30,
                "Virtual Network": 16, "Container Registry": 9, "Key Vault": 3, "Azure DNS": 2, "Event Hubs": 7}
    cut = dt.date(p_start.year, p_start.month, 16)     # log clean-up mid previous month
    stop = dt.date(m_start.year, m_start.month, 9)     # test clusters stopped
    out, d = [], p_start
    while d <= today:
        partial = 1.0 if d < today - dt.timedelta(days=1) else (0.93 if d < today else 0.22)
        for sub, w in subs.items():
            for svc, base in services.items():
                v = base * w * (0.93 if d.weekday() >= 5 else 1.0) * rnd.uniform(0.96, 1.04)
                if svc in ("Log Analytics", "Microsoft Sentinel") and d >= cut:
                    v *= 0.55
                if sub == "Test" and svc in ("Virtual Machines", "Azure Kubernetes Service") and d >= stop:
                    v *= 0.35
                if svc == "Event Hubs" and d >= m_start:
                    v *= 1.6
                out.append((sub, d, svc, v * partial / 4.2))
        if d.day == 5:
            out.append(("Production", d, "Virtual Machines", 310.0))  # monthly reservation charge
        d += dt.timedelta(days=1)
    return out


DEMO_FINDINGS = [
    {"id": "OBS-01", "title": "Send only kube-audit-admin from non-production clusters", "saving_low": 180, "saving_high": 220,
     "confidence": "verified", "wave": "1 - Now", "owner": "Platform team", "status": "done"},
    {"id": "STO-02", "title": "Lower premium file share quota to used size + 20%", "saving_low": 90, "saving_high": 110,
     "confidence": "verified", "wave": "1 - Now", "owner": "Platform team", "status": "open"},
    {"id": "NET-01", "title": "Remove idle Event Hubs namespaces (0 messages in 30 days)", "saving_low": 40, "saving_high": 60,
     "confidence": "verified", "wave": "2 - 30 days", "owner": "App team sign-off", "status": "open"},
    {"id": "DB-01", "title": "Lower Cosmos DB autoscale max (1% of RU/s used)", "saving_low": 120, "saving_high": 200,
     "confidence": "estimated", "wave": "3 - 30-90 days", "owner": "DBA", "status": "open"},
    {"id": "CMP-03", "title": "Stop test clusters outside working hours", "saving_low": 150, "saving_high": 260,
     "confidence": "estimated", "wave": "2 - 30 days", "owner": "Test lead", "status": "open"},
    {"id": "COM-01", "title": "Savings plan sized on the post-clean-up baseline", "saving_low": 200, "saving_high": 450,
     "confidence": "possible", "wave": "4 - Commitments", "owner": "Finance + billing admin", "status": "open"},
]


def anonymize(data):
    """Replace subscription names with neutral labels so the file can be shared more widely.

    Service names are Azure product names and stay. Findings are free text written by the
    reviewer: subscription names inside them are replaced, anything else is left as written.
    """
    names = [s["name"] for s in data["subs"]]
    names += [n for n in data["meta"]["scope"] if n not in names]
    alias = {n: f"Subscription {i + 1}" for i, n in enumerate(names)}
    data["meta"]["scope"] = [alias[n] for n in data["meta"]["scope"]]
    data["meta"]["anonymized"] = True
    for s in data["subs"]:
        s["name"] = alias[s["name"]]

    def scrub(text):
        for real in sorted(alias, key=len, reverse=True):
            text = text.replace(real, alias[real])
        return text
    data["meta"]["notes"] = [scrub(n) for n in data["meta"].get("notes", [])]
    for f in data.get("findings") or []:
        for k, v in list(f.items()):
            if isinstance(v, str):
                f[k] = scrub(v)
    return data


def render(data, out):
    html = TEMPLATE.read_text(encoding="utf-8")
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # keep the JSON inert inside <script>: no closing tags, comments or line separators
    blob = blob.replace("<", "\\u003c").replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    if "/*__DATA__*/null" not in html:
        sys.exit("dashboard template is missing its data placeholder")
    pathlib.Path(out).write_text(html.replace("/*__DATA__*/null", blob, 1), encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tenant", help="tenantId to restrict subscriptions to (recommended)")
    p.add_argument("--subscriptions", help="comma-separated subscription IDs")
    p.add_argument("--month", help="YYYY-MM (default: current month)")
    p.add_argument("--budget", type=float, help="monthly budget amount to compare against")
    p.add_argument("--findings", help="JSON file with savings findings (see --help)")
    p.add_argument("--title", default="Azure cost dashboard")
    p.add_argument("--out", help="output HTML file (default: finops-dashboard-YYYY-MM.html)")
    p.add_argument("--cost-type", default="ActualCost", choices=["ActualCost", "AmortizedCost"])
    p.add_argument("--rate-days", type=int, default=10, help="complete days used for run rates")
    p.add_argument("--complete-through", help="last complete day (default: today-2)")
    p.add_argument("--one-off", action="append", help="AMOUNT@DAY charge in the month, e.g. 500@1")
    p.add_argument("--spike-factor", type=float, default=1.5)
    p.add_argument("--pause", type=float, default=2.0, help="seconds between subscriptions (throttling)")
    p.add_argument("--allow-partial", action="store_true", help="continue if some subscriptions fail (noted in the report)")
    p.add_argument("--save-data", help="also write the dashboard data to this JSON file")
    p.add_argument("--data", help="render from a JSON file saved with --save-data (no Azure calls)")
    p.add_argument("--demo", action="store_true", help="render synthetic demo data (no Azure calls)")
    p.add_argument("--anonymize", action="store_true",
                   help="replace subscription names with 'Subscription 1..n' (for sharing outside the team)")
    a = p.parse_args()

    findings = json.load(open(a.findings, encoding="utf-8")) if a.findings else None
    if a.data:
        data = json.load(open(a.data, encoding="utf-8"))
        if findings is not None:
            data["findings"] = findings
        if a.budget is not None:
            data["budget"] = a.budget
    elif a.demo:
        today = dt.date(2026, 3, 18)
        month = a.month or "2026-03"
        data = build(demo_records(month, today), month, today - dt.timedelta(days=2), today,
                     ["Platform-Core", "Production", "Test", "Data-AI"], "EUR", a.cost_type, a.rate_days,
                     a.one_off or ["310@5"], a.spike_factor, a.budget or 12000, findings or DEMO_FINDINGS, a.title, demo=True)
    else:
        today = dt.date.today()
        month = a.month or f"{today.year}-{today.month:02d}"
        m_start, m_end = month_bounds(month)
        p_start, _ = month_bounds(prev_month(month))
        _, q_end, complete = fc.forecast_window(month, a.complete_through, a.rate_days)
        fc.PAUSE, fc.ALLOW_PARTIAL = a.pause, a.allow_partial
        ids = {s.strip() for s in a.subscriptions.split(",")} if a.subscriptions else None
        subs = fc.subscriptions(a.tenant, ids)
        rows = fc.collect(subs, p_start.isoformat(), q_end.isoformat(), group=["ServiceName"], cost_type=a.cost_type)
        if not rows:
            sys.exit("No cost data returned for this scope and period")
        currency = collections.Counter(r.get("Currency", "") for _, r in rows).most_common(1)[0][0] or "EUR"
        records = [(name, fc.day_of(r), r.get("ServiceName", ""), r["Cost"]) for name, r in rows]
        data = build(records, month, complete, min(today, m_end), [n for _, n, _ in subs], currency, a.cost_type,
                     a.rate_days, a.one_off, a.spike_factor, a.budget, findings, a.title)
        got = {name for name, _ in rows}
        missing = [n for _, n, _ in subs if n not in got]
        if missing:
            data["meta"]["notes"].append("No cost data for: " + ", ".join(missing) + " (failed or no spend).")

    if a.anonymize:
        data = anonymize(data)
    out = a.out or f"finops-dashboard-{data['meta']['month']}.html"
    render(data, out)
    if a.save_data:
        json.dump(data, open(a.save_data, "w", encoding="utf-8"), indent=1)
    f = data["forecast"]
    print(f"Dashboard written: {pathlib.Path(out).resolve()}")
    cur = data["meta"]["currency"]
    print(f"  Scope: {', '.join(data['meta']['scope'])}  [{data['meta']['cost_type']}, {cur}]")
    print(f"  Complete days through {data['meta']['complete_through']}; month to date {f['mtd']:,.1f} "
          f"(posted so far incl. partial days {f['mtd_now']:,.1f})")
    print(f"  Run rates: weekday {f['weekday_rate']:,.1f}/day ({f['weekday_low']:,.1f}-{f['weekday_high']:,.1f}), "
          f"weekend {f['weekend_rate']:,.1f}/day ({f['weekend_low']:,.1f}-{f['weekend_high']:,.1f})")
    print(f"  Remaining: {f['remaining_weekdays']} weekdays + {f['remaining_weekend_days']} weekend days; "
          f"one-offs not yet billed {f['one_offs']:,.1f}")
    print(f"  FORECAST {data['meta']['month']}: {f['forecast']:,.1f} (range {f['low']:,.1f} - {f['high']:,.1f})")
    if data.get("prev"):
        pv = data["prev"]
        print(f"  Previous month {pv['month']}: {pv['total']:,.1f}; same days last month {pv['same_period']:,.1f}")
    if data.get("budget"):
        print(f"  Budget {data['budget']:,.0f}: forecast is {f['forecast'] / data['budget'] * 100:.0f}% of it")
    print("  Top services month to date: " + "; ".join(f"{x['name']} {x['mtd']:,.0f}" for x in data["services"][:5]))
    for n in data["meta"].get("notes", []):
        print("  note: " + n)


if __name__ == "__main__":
    main()
