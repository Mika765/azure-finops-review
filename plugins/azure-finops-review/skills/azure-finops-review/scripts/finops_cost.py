#!/usr/bin/env python3
"""Azure Cost Management helper for FinOps reviews (READ-ONLY).

Calls the Cost Management Query API with a token from `az account get-access-token`,
so it works wherever `az login` works. It never changes anything: a guard allows
only `az account list`, `az account get-access-token` and POST .../CostManagement/query.
Three modes:

  totals    (default)  daily or monthly totals, optionally split by subscription
  --group   DIMS       breakdown by Cost Management dimensions (ServiceName,
                       ResourceGroupName, ResourceId, Meter, ...), optionally
                       comparing two windows with --before/--after
  --forecast YYYY-MM   month-close forecast from weekday/weekend run rates

Examples:
  finops_cost.py --tenant <tid> --from 2026-10-01 --to 2026-10-31 --by subscription
  finops_cost.py --tenant <tid> --from 2026-07-01 --to 2026-10-31 --granularity Monthly --by subscription
  finops_cost.py --tenant <tid> --group ServiceName --before 2026-10-20:2026-10-23 --after 2026-10-27:2026-10-30
  finops_cost.py --subscriptions <id> --from ... --to ... --group ResourceId,Meter --service Sentinel
  finops_cost.py --tenant <tid> --forecast 2026-11 --rate-days 10 --one-off 500@1
"""
import argparse
import calendar
import collections
import datetime as dt
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "2023-11-01"
ARM = "https://management.azure.com"

# READ-ONLY GUARD. This script must never change anything in Azure.
# The only az commands it may run, and the only REST call it may make (the Cost
# Management *query* endpoint, which is a POST but reads data).
ALLOWED_AZ = {("account", "list"), ("account", "get-access-token")}
ALLOWED_POST_SUFFIX = "/providers/Microsoft.CostManagement/query"


def az(args):
    if tuple(args[:2]) not in ALLOWED_AZ:
        raise SystemExit(f"read-only guard: refusing to run 'az {' '.join(args[:2])}'")
    exe = shutil.which("az") or shutil.which("az.cmd")
    if not exe:
        sys.exit("Azure CLI (az) not found on PATH")
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    p = subprocess.run([exe] + args + ["-o", "json"], capture_output=True, text=True, env=env)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip()[:500] or f"az {' '.join(args[:2])} failed")
    return json.loads(p.stdout) if p.stdout.strip() else None


_tokens = {}


def token(tenant):
    """ARM access token for a tenant, cached until 5 minutes before expiry."""
    t = _tokens.get(tenant)
    if t and t[1] > time.time() + 300:
        return t[0]
    args = ["account", "get-access-token", "--resource", ARM]
    if tenant:
        args += ["--tenant", tenant]
    r = az(args)
    exp = r.get("expires_on") or 0
    try:
        exp = float(exp)
    except (TypeError, ValueError):
        exp = time.time() + 1800
    _tokens[tenant] = (r["accessToken"], exp)
    return r["accessToken"]


def retry_after(headers, attempt):
    """Seconds to wait after a 429/503: honour Cost Management's own headers, else back off."""
    for h in ("x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after",
              "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after",
              "x-ms-ratelimit-microsoft.costmanagement-tenant-retry-after",
              "x-ms-ratelimit-microsoft.costmanagement-client-retry-after",
              "Retry-After"):
        v = headers.get(h) if headers else None
        if v:
            try:
                return min(float(v) + 1, 120)
            except ValueError:
                pass
    return min(10 * 2 ** attempt, 120)


def post_query(url, body, tenant, retries=8):
    """POST to the Cost Management query endpoint (read-only) with throttling-aware retries.

    Uses urllib with an az-issued token instead of `az rest`: nextLink URLs contain '&',
    which `az.cmd` on Windows splits as a command separator, and urllib exposes the
    Retry-After headers that `az rest` hides.
    """
    path = urllib.parse.urlsplit(url).path
    if not path.endswith(ALLOWED_POST_SUFFIX):
        raise SystemExit(f"read-only guard: refusing POST to {path}")
    if url.startswith("/"):
        url = ARM + url
    data = json.dumps(body).encode()
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": f"Bearer {token(tenant)}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:400]
            if e.code in (429, 503) and attempt < retries - 1:
                wait = retry_after(e.headers, attempt)
                print(f"  throttled ({e.code}), waiting {wait:.0f}s ...", file=sys.stderr)
                time.sleep(wait)
                continue
            if e.code == 401 and attempt == 0:
                _tokens.pop(tenant, None)
                continue
            raise RuntimeError(f"HTTP {e.code}: {msg}")
        except urllib.error.URLError as e:
            if attempt < retries - 1:
                time.sleep(min(5 * 2 ** attempt, 60))
                continue
            raise RuntimeError(f"network error: {e.reason}")
    raise RuntimeError("gave up after repeated throttling")


def subscriptions(tenant, ids):
    subs = az(["account", "list", "--all"]) or []
    out = []
    for s in subs:
        if s.get("state") != "Enabled":
            continue
        if tenant and s.get("tenantId") != tenant:
            continue
        if ids and s["id"] not in ids:
            continue
        out.append((s["id"], s["name"], s.get("tenantId")))
    if not out:
        sys.exit("No matching enabled subscriptions (check --tenant / --subscriptions / az login)")
    if ids:
        missing = ids - {i for i, _, _ in out}
        if missing:
            print(f"WARNING: not found or not enabled: {', '.join(sorted(missing))}", file=sys.stderr)
    tenants = {t for _, _, t in out}
    if len(tenants) > 1:
        print(f"WARNING: subscriptions span {len(tenants)} tenants; pass --tenant to avoid mixing them", file=sys.stderr)
    return out


def query(sub, tenant, start, end, granularity="Daily", group=None, service=None, cost_type="ActualCost"):
    dataset = {"granularity": granularity, "aggregation": {"c": {"name": "Cost", "function": "Sum"}}}
    if group:
        dataset["grouping"] = [{"type": "Dimension", "name": g} for g in group]
    if service:
        dataset["filter"] = {"dimensions": {"name": "ServiceName", "operator": "In", "values": service}}
    body = {"type": cost_type, "timeframe": "Custom",
            "timePeriod": {"from": f"{start}T00:00:00Z", "to": f"{end}T23:59:59Z"}, "dataset": dataset}
    rows, url = [], f"/subscriptions/{sub}/providers/Microsoft.CostManagement/query?api-version={API}"
    while url:
        props = post_query(url, body, tenant)["properties"]
        cols = [c["name"] for c in props["columns"]]
        for r in props["rows"]:
            rows.append(dict(zip(cols, r)))
        url = props.get("nextLink")
        if url:
            time.sleep(1)  # pages count against the same rate limit
    return rows


def day_of(row):
    v = row.get("UsageDate") or row.get("BillingMonth")
    s = str(v)
    if s.isdigit():
        return dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    return dt.date.fromisoformat(s[:10])


PAUSE = 2.0          # seconds between subscriptions (set by --pause)
ALLOW_PARTIAL = False


def collect(subs, start, end, **kw):
    """Return list of (sub_name, row) across subscriptions.

    A failed subscription makes every total wrong (too low), so by default the
    script stops instead of printing incomplete numbers. --allow-partial overrides.
    """
    out, failed = [], []
    for i, (sid, name, tenant) in enumerate(subs):
        if i:
            time.sleep(PAUSE)
        try:
            for r in query(sid, tenant, start, end, **kw):
                out.append((name, r))
        except RuntimeError as e:
            first = (str(e).splitlines() or ["unknown error"])[0][:300]
            print(f"ERROR {name}: {first}", file=sys.stderr)
            failed.append(name)
    if failed:
        msg = f"{len(failed)} of {len(subs)} subscription(s) failed: {', '.join(failed)}"
        if not ALLOW_PARTIAL:
            sys.exit(f"INCOMPLETE DATA - {msg}. Totals would be too low; re-run "
                     "(throttling usually clears in a minute) or pass --allow-partial.")
        print(f"WARNING: PARTIAL TOTALS - {msg}", file=sys.stderr)
    return out


def fmt(x):
    return f"{x:,.1f}"


def cmd_totals(a, subs):
    data = collect(subs, a.date_from, a.date_to, granularity=a.granularity, cost_type=a.cost_type)
    tot = collections.defaultdict(float)
    by = collections.defaultdict(lambda: collections.defaultdict(float))
    for name, r in data:
        d = day_of(r)
        key = d.strftime("%Y-%m") if a.granularity == "Monthly" else d
        tot[key] += r["Cost"]
        by[key][name] += r["Cost"]
    names = [n for _, n, _ in subs]
    head = ["date", "day", "total"] + (names if a.by == "subscription" else [])
    print("\t".join(head))
    for k in sorted(tot):
        wd = k.strftime("%a") if isinstance(k, dt.date) else ""
        row = [str(k), wd, fmt(tot[k])] + ([fmt(by[k].get(n, 0)) for n in names] if a.by == "subscription" else [])
        print("\t".join(row))
    if a.granularity == "Daily" and tot:
        months = collections.defaultdict(float)
        for k, v in tot.items():
            months[k.strftime("%Y-%m")] += v
        print("\nMonth totals: " + ", ".join(f"{m} {fmt(v)}" for m, v in sorted(months.items())))
        flag_spikes(tot)
    if a.json:
        json.dump({str(k): {"total": tot[k], **by[k]} for k in sorted(tot)}, open(a.json, "w"), indent=1)


def flag_spikes(tot, factor=1.5, quiet=False):
    """Days far above the median of the same day type (weekday/weekend): likely one-off charges."""
    spikes = {}
    for weekend in (False, True):
        vals = [v for d, v in tot.items() if (d.weekday() >= 5) == weekend]
        if len(vals) < 3:
            continue
        med = statistics.median(vals)
        for d, v in tot.items():
            if (d.weekday() >= 5) == weekend and v > med * factor:
                spikes[d] = v - med
    if spikes and not quiet:
        print("One-off days (excluded from run rates): " +
              ", ".join(f"{d} +{fmt(x)} over typical" for d, x in sorted(spikes.items())))
    return spikes


def parse_window(s):
    a, b = s.split(":")
    return dt.date.fromisoformat(a), dt.date.fromisoformat(b)


def cmd_group(a, subs):
    dims = [g.strip() for g in a.group.split(",")]
    if a.before and a.after:
        b0, b1 = parse_window(a.before)
        f0, f1 = parse_window(a.after)
        start, end = min(b0, f0), max(b1, f1)
    else:
        start, end = dt.date.fromisoformat(a.date_from), dt.date.fromisoformat(a.date_to)
    # Comparisons need daily rows; plain breakdowns can use --granularity Monthly (far fewer rows/pages).
    gran = "Daily" if (a.before and a.after) else a.granularity
    data = collect(subs, start.isoformat(), end.isoformat(), granularity=gran, group=dims,
                   service=a.service, cost_type=a.cost_type)
    per = collections.defaultdict(lambda: collections.defaultdict(float))
    for name, r in data:
        key = (name,) + tuple(str(r.get(d, "")).split("/")[-1] if d == "ResourceId" else str(r.get(d, "")) for d in dims)
        per[key][day_of(r)] += r["Cost"]
    label = "\t".join(["subscription"] + dims)
    if a.before and a.after:
        def avg(days, w0, w1):
            n = (w1 - w0).days + 1
            return sum(v for d, v in days.items() if w0 <= d <= w1) / n
        rows = [(avg(v, f0, f1) - avg(v, b0, b1), k, avg(v, b0, b1), avg(v, f0, f1)) for k, v in per.items()]
        rows.sort(key=lambda x: -abs(x[0]))
        print(f"{label}\tbefore/day\tafter/day\tdelta/day")
        for delta, k, bv, av in rows[: a.top]:
            if abs(delta) >= a.min_delta:
                print("\t".join(k) + f"\t{fmt(bv)}\t{fmt(av)}\t{delta:+,.1f}")
    else:
        ndays = (end - start).days + 1
        rows = sorted(((sum(v.values()), k) for k, v in per.items()), reverse=True)
        print(f"{label}\ttotal\tavg/day")
        for t, k in rows[: a.top]:
            print("\t".join(k) + f"\t{fmt(t)}\t{fmt(t / ndays)}")


def cmd_forecast(a, subs):
    y, m = map(int, a.forecast.split("-"))
    m_start = dt.date(y, m, 1)
    m_end = dt.date(y, m, calendar.monthrange(y, m)[1])
    today = dt.date.today()
    complete = dt.date.fromisoformat(a.complete_through) if a.complete_through else today - dt.timedelta(days=2)
    complete = min(complete, m_end)
    q_start = min(m_start, complete - dt.timedelta(days=max(a.rate_days * 2, 21)))
    data = collect(subs, q_start.isoformat(), min(today, m_end).isoformat(), cost_type=a.cost_type)
    tot = collections.defaultdict(float)
    for _, r in data:
        tot[day_of(r)] += r["Cost"]
    hist = {d: v for d, v in tot.items() if d <= complete}
    spikes = flag_spikes(hist, a.spike_factor)
    window = sorted(d for d in hist if d not in spikes)[-a.rate_days:]
    wk = [hist[d] for d in window if d.weekday() < 5]
    we = [hist[d] for d in window if d.weekday() >= 5]
    if not wk:
        sys.exit("No complete weekdays in the rate window; widen --rate-days")
    if not we:
        we_hist = [hist[d] for d in sorted(hist) if d.weekday() >= 5 and d not in spikes][-4:]
        we = we_hist or wk

    def band(vals):
        mean = statistics.mean(vals)
        if len(vals) >= 4:
            q = statistics.quantiles(vals, n=4)
            return mean, q[0], q[2]
        return mean, min(vals), max(vals)

    wk_r, wk_lo, wk_hi = band(wk)
    we_r, we_lo, we_hi = band(we)
    mtd = sum(v for d, v in hist.items() if m_start <= d <= complete)
    rem = [m_start + dt.timedelta(days=i) for i in range((m_end - m_start).days + 1)]
    rem = [d for d in rem if d > complete]
    n_wk = sum(1 for d in rem if d.weekday() < 5)
    n_we = len(rem) - n_wk
    one_offs = 0.0
    notes = []
    for o in a.one_off or []:
        amt, day = o.split("@")
        dday = dt.date(y, m, int(day))
        if dday > complete:
            one_offs += float(amt)
            notes.append(f"one-off {amt} on {dday} (not yet billed)")
        else:
            notes.append(f"one-off {amt} on {dday} already in actuals")
    point = mtd + n_wk * wk_r + n_we * we_r + one_offs
    low = mtd + n_wk * wk_lo + n_we * we_lo + one_offs
    high = mtd + n_wk * wk_hi + n_we * we_hi + one_offs

    print(f"Scope: {len(subs)} subscription(s): {', '.join(n for _, n, _ in subs)}  [{a.cost_type}]")
    print(f"Complete days through {complete} (later days are partial: late meters post 24-48 h)")
    print(f"Rate window: {window[0]} .. {window[-1]} ({len(window)} days, one-offs excluded)")
    print(f"  weekday rate {fmt(wk_r)}/day (range {fmt(wk_lo)}-{fmt(wk_hi)}, n={len(wk)})")
    print(f"  weekend rate {fmt(we_r)}/day (range {fmt(we_lo)}-{fmt(we_hi)}, n={len(we)})")
    print(f"\nActual {m_start}..{complete}: {fmt(mtd)}")
    print(f"Remaining: {n_wk} weekdays x {fmt(wk_r)} + {n_we} weekend days x {fmt(we_r)} = {fmt(n_wk * wk_r + n_we * we_r)}")
    for n in notes:
        print("  " + n)
    print(f"\nFORECAST {a.forecast}: {fmt(point)}  (range {fmt(low)} - {fmt(high)})")
    prev_end = m_start - dt.timedelta(days=1)
    print("\nRecent days:")
    for d in sorted(tot)[-10:]:
        tag = " partial" if d > complete else (" one-off" if d in spikes else "")
        print(f"  {d} {d.strftime('%a')} {fmt(tot[d])}{tag}")
    if a.json:
        json.dump({"month": a.forecast, "forecast": point, "low": low, "high": high, "mtd": mtd,
                   "complete_through": str(complete), "weekday_rate": wk_r, "weekend_rate": we_r,
                   "remaining_weekdays": n_wk, "remaining_weekend_days": n_we, "one_offs": one_offs,
                   "previous_month_end": str(prev_end)}, open(a.json, "w"), indent=1)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tenant", help="tenantId to restrict subscriptions to (recommended)")
    p.add_argument("--subscriptions", help="comma-separated subscription IDs")
    p.add_argument("--from", dest="date_from", help="YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", help="YYYY-MM-DD")
    p.add_argument("--granularity", default="Daily", choices=["Daily", "Monthly"])
    p.add_argument("--by", default="total", choices=["total", "subscription"])
    p.add_argument("--cost-type", default="ActualCost", choices=["ActualCost", "AmortizedCost"])
    p.add_argument("--group", help="dimensions, e.g. ServiceName or ResourceId,Meter")
    p.add_argument("--service", action="append", help="filter on ServiceName (repeatable)")
    p.add_argument("--before", help="FROM:TO window for comparison (with --group)")
    p.add_argument("--after", help="FROM:TO window for comparison (with --group)")
    p.add_argument("--top", type=int, default=25)
    p.add_argument("--min-delta", type=float, default=0.5, help="hide comparison rows below this delta/day")
    p.add_argument("--forecast", help="YYYY-MM month to forecast")
    p.add_argument("--rate-days", type=int, default=10, help="complete days used for run rates")
    p.add_argument("--complete-through", help="last complete day (default: today-2)")
    p.add_argument("--one-off", action="append", help="AMOUNT@DAY charge in the forecast month, e.g. 500@1")
    p.add_argument("--spike-factor", type=float, default=1.5)
    p.add_argument("--json", help="also write results to this JSON file")
    p.add_argument("--pause", type=float, default=2.0, help="seconds between subscriptions (throttling)")
    p.add_argument("--allow-partial", action="store_true",
                   help="print totals even if some subscriptions failed (marked PARTIAL)")
    a = p.parse_args()
    global PAUSE, ALLOW_PARTIAL
    PAUSE, ALLOW_PARTIAL = a.pause, a.allow_partial
    ids = {s.strip() for s in a.subscriptions.split(",")} if a.subscriptions else None
    subs = subscriptions(a.tenant, ids)
    if a.forecast:
        cmd_forecast(a, subs)
    elif a.group:
        if not (a.before and a.after) and not (a.date_from and a.date_to):
            sys.exit("--group needs --from/--to or --before/--after")
        cmd_group(a, subs)
    else:
        if not (a.date_from and a.date_to):
            sys.exit("--from and --to are required")
        cmd_totals(a, subs)


if __name__ == "__main__":
    main()
