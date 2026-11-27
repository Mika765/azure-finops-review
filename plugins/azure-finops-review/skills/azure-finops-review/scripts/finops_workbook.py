#!/usr/bin/env python3
"""Multi-year Azure cost workbook (Excel) for FinOps reviews (READ-ONLY).

Writes one .xlsx file with live formulas:

  Summary              the years side by side, cost by subscription, year-end run rate
  Monthly comparison   every month of every year, differences, quarters, a chart
  By subscription      month by month for each subscription
  Forecast <years>     scenarios for the next three years (editable assumptions)
  Forecast inputs      daily rates and day counts behind the forecast months

Past months are billed amounts. The current month and the rest of the current year
are forecast from weekday/weekend run rates (same method as finops_cost.py --forecast).
The scenario growth rates are ASSUMPTIONS: pass the ones the user gave you.

It reuses finops_cost.py, so it can only call `az account list`,
`az account get-access-token` and POST .../CostManagement/query.

Needs the `openpyxl` package (pip install openpyxl). Formulas are calculated when
the file is opened in Excel or LibreOffice; the console output gives the same totals.

Examples:
  finops_workbook.py --tenant <tid> --out azure-cost.xlsx
  finops_workbook.py --tenant <tid> --years 3 --one-off 500@13:Production \\
      --scenario "Main:19000:10,15,20" --scenario "High:21000:10,15,10"
  finops_workbook.py --tenant <tid> --anonymize        # subscription names replaced
  finops_workbook.py --demo --out demo.xlsx             # synthetic data, no Azure access
"""
import argparse
import calendar
import collections
import datetime as dt
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import finops_cost as fc  # noqa: E402

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]
NOT_SPLIT = "Not split"


def day_counts(year, month, after=None):
    """(weekdays, weekend days) in a month, optionally only the days after `after`."""
    last = calendar.monthrange(year, month)[1]
    days = [dt.date(year, month, d) for d in range(1, last + 1)]
    if after:
        days = [d for d in days if d > after]
    wk = sum(1 for d in days if d.weekday() < 5)
    return wk, len(days) - wk


def year_day_counts(year):
    wk = we = 0
    for m in range(1, 13):
        a, b = day_counts(year, m)
        wk, we = wk + a, we + b
    return wk, we


def parse_one_offs(specs, subs):
    """['500@13:Prod', '80@1'] -> ({sub: monthly amount}, {sub: [(amount, day)]}), adding NOT_SPLIT if needed."""
    monthly, items = collections.defaultdict(float), collections.defaultdict(list)
    for s in specs or []:
        head, _, name = s.partition(":")
        amt, _, day = head.partition("@")
        name = name.strip() or NOT_SPLIT
        if name != NOT_SPLIT and name not in subs:
            sys.exit(f"--one-off: subscription '{name}' not in scope ({', '.join(subs)})")
        monthly[name] += float(amt)
        items[name].append((float(amt), int(day or 1)))
    return monthly, items


def parse_scenarios(specs):
    out = []
    for s in specs or []:
        parts = s.split(":")
        if len(parts) != 3:
            sys.exit(f'--scenario must be "NAME:BASE:G1,G2,G3" (got {s!r})')
        g = [float(x) / 100 for x in parts[2].split(",")]
        if len(g) != 3:
            sys.exit("--scenario needs three growth rates, e.g. 10,15,20")
        base = None if parts[1].strip().lower() in ("", "auto") else float(parts[1])
        out.append({"name": parts[0].strip(), "base": base, "growth": g,
                    "basis": "Assumptions passed with --scenario. Base is the monthly cost before the first year's growth."})
    return out


def build_model(monthly, daily, subs, today, complete, n_years=3, currency="EUR", cost_type="ActualCost",
                rate_days=10, one_off=None, spike_factor=1.5, scenarios=None, demo=False):
    """Pure function (no Azure calls).

    monthly: {(year, month): {sub: cost}} for complete months; daily: {sub: {date: cost}}.
    """
    subs = list(subs)
    one_monthly, one_items = parse_one_offs(one_off, subs)
    if NOT_SPLIT in one_monthly:
        subs.append(NOT_SPLIT)
    cy, cm = complete.year, complete.month
    years = list(range(cy - n_years + 1, cy + 1))
    month = f"{cy}-{cm:02d}"
    m_start = dt.date(cy, cm, 1)
    mtd, wk, we, pending = [], [], [], []
    for s in subs:
        tot = daily.get(s, {})
        hist = {d: v for d, v in tot.items() if d <= complete}
        mtd.append(sum(v for d, v in hist.items() if d >= m_start))
        if any(d.weekday() < 5 for d in hist):
            f = fc.compute_forecast(tot, month, complete, rate_days, None, spike_factor)
            wk.append(f["weekday_rate"])
            we.append(f["weekend_rate"])
        else:
            wk.append(0.0)
            we.append(0.0)
        pending.append(sum(a for a, d in one_items.get(s, []) if d > complete.day))
    table = {y: [[float(monthly.get((y, m), {}).get(s, 0.0)) for s in subs] if (y, m) < (cy, cm) else None
                 for m in range(1, 13)] for y in years}
    days = [(cm,) + day_counts(cy, cm, after=complete)] + [(m,) + day_counts(cy, m) for m in range(cm + 1, 13)]
    if not scenarios:
        scenarios = [{"name": "Main scenario", "base": None, "growth": [0.05, 0.05, 0.05],
                      "basis": "EXAMPLE ASSUMPTIONS, replace with your own: base = this year's average month, growth 5% a year."}]
    return {
        "currency": currency, "cost_type": cost_type, "today": today, "complete": complete, "years": years,
        "subs": subs, "table": table, "cur_year": cy, "cur_month": cm, "mtd": mtd, "weekday": wk, "weekend": we,
        "one_off": [one_monthly.get(s, 0.0) for s in subs], "one_off_pending": pending, "days": days,
        "next_year_days": year_day_counts(cy + 1), "scenarios": scenarios, "demo": demo, "anonymized": False,
    }


def totals(model):
    """The numbers the workbook formulas will show, computed in Python (for the console and for tests)."""
    n = len(model["subs"])
    fmonth = {}
    for i, (m, wd, wed) in enumerate(model["days"]):
        fmonth[m] = [(model["mtd"][j] + model["one_off_pending"][j] if i == 0 else model["one_off"][j])
                     + wd * model["weekday"][j] + wed * model["weekend"][j] for j in range(n)]
    by_month = {y: [sum(row) if row is not None else sum(fmonth[m + 1]) for m, row in enumerate(model["table"][y])]
                for y in model["years"]}
    year = {y: sum(v) for y, v in by_month.items()}
    wd, wed = model["next_year_days"]
    minimum = (sum(model["weekday"]) * wd + sum(model["weekend"]) * wed) / 12 + sum(model["one_off"])
    scen = []
    for s in model["scenarios"] + [{"name": "Minimum", "base": minimum, "growth": [0, 0, 0]}]:
        base = s["base"] if s["base"] is not None else year[model["cur_year"]] / 12
        v, out = base, []
        for g in s["growth"]:
            v *= 1 + g
            out.append(v * 12)
        scen.append((s["name"], base, out))
    return {"forecast_months": {m: sum(v) for m, v in fmonth.items()}, "by_month": by_month, "year": year, "scenarios": scen}


def anonymize(model):
    """Replace subscription names with 'Subscription 1..n' (the 'Not split' column keeps its name)."""
    i = 0
    names = []
    for s in model["subs"]:
        if s == NOT_SPLIT:
            names.append(s)
        else:
            i += 1
            names.append(f"Subscription {i}")
    model["subs"], model["anonymized"] = names, True
    return model


def demo_inputs(today):
    """Synthetic, deterministic data: three years, six subscriptions."""
    rnd = random.Random(42)
    subs = ["Platform", "Production", "Test", "UAT", "Data", "Sandbox"]
    cy = today.year
    level = {cy - 2: [(1500, 1450), (4300, 4100), (1700, 1500), (900, 850), (0, 0), (0, 0)],
             cy - 1: [(1450, 1600), (4100, 5600), (1500, 2300), (850, 1500), (150, 420), (20, 60)],
             cy: [(1600, 1900), (5600, 4700), (2300, 1100), (1500, 700), (420, 380), (60, 90)]}
    monthly = {}
    for y, lv in level.items():
        for m in range(1, 13):
            monthly[(y, m)] = {s: (round((a + (b - a) * (m - 1) / 11) * rnd.uniform(0.94, 1.06), 1) if a or b else 0.0)
                               for s, (a, b) in zip(subs, lv)}
    wk = [62.0, 151.0, 36.5, 23.0, 12.4, 3.0]
    we = [58.5, 144.0, 31.0, 22.5, 12.0, 3.0]
    daily = {}
    for s, a, b in zip(subs, wk, we):
        daily[s] = {}
        d = today - dt.timedelta(days=45)
        while d <= today:
            v = (a if d.weekday() < 5 else b) * rnd.uniform(0.985, 1.015)
            if s == "Production" and d.day == 13:
                v += 300
            daily[s][d] = round(v, 1)
            d += dt.timedelta(days=1)
    scenarios = [
        {"name": "Main scenario", "base": 9500, "growth": [0.05, 0.08, 0.10],
         "basis": "Example assumptions: base = average month of the last year; growth rates are placeholders."},
        {"name": "Alternative (higher base)", "base": 10500, "growth": [0.08, 0.10, 0.12],
         "basis": "Example assumptions: base = the highest recent spend level, with faster growth."},
    ]
    return monthly, daily, subs, scenarios


# ------------------------------------------------------------------ workbook

def render(model, out):
    try:
        from openpyxl import Workbook
        from openpyxl.chart import BarChart, LineChart, Reference
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter as L
    except ImportError:
        sys.exit("This script needs the openpyxl package: pip install openpyxl")

    subs, years, cur = model["subs"], model["years"], model["currency"]
    n, k, cy, cm = len(subs), len(years), model["cur_year"], model["cur_month"]
    fyears = [cy + 1, cy + 2, cy + 3]
    fname = f"Forecast {fyears[0]}-{fyears[-1]}"
    origin = ("Synthetic demo data, not a real tenant." if model["demo"]
              else f"Source: Azure Cost Management, read on {model['today']:%d %b %Y}.")
    if model["anonymized"]:
        origin += " Subscription names are anonymised."
    basis = f"{cur}, {'actual' if model['cost_type'] == 'ActualCost' else 'amortized'} cost, as returned by Cost Management (taxes not included)."

    F = "Arial"
    BLUE, BLACK, GREEN = (Font(name=F, size=10, color=c) for c in ("0000FF", "000000", "008000"))
    HEAD = Font(name=F, size=10, bold=True, color="FFFFFF")
    TITLE = Font(name=F, size=14, bold=True)
    NOTE = Font(name=F, size=9, italic=True, color="595959")
    HFILL, TFILL = PatternFill("solid", fgColor="1F3864"), PatternFill("solid", fgColor="D9E1F2")
    FFILL, YFILL = PatternFill("solid", fgColor="FFF2CC"), PatternFill("solid", fgColor="FFFF00")
    EUR, EUR1, PCT = '#,##0;(#,##0);"-"', '#,##0.0;(#,##0.0);"-"', '0.0%;(0.0%);"-"'
    TOP = Border(top=Side(style="thin", color="000000"))
    wb = Workbook()

    def header(ws, row, labels, widths=None):
        for i, t in enumerate(labels, 1):
            c = ws.cell(row=row, column=i, value=t)
            c.font, c.fill = HEAD, HFILL
            c.alignment = Alignment(horizontal="center" if i > 1 else "left", vertical="center", wrap_text=True)
        ws.row_dimensions[row].height = 32
        for i, w in enumerate(widths or [], 1):
            ws.column_dimensions[L(i)].width = w

    def put(ws, ref, value, font=BLACK, fmt=None, fill=None, bold=False, align=None):
        c = ws[ref]
        c.value = value
        c.font = Font(name=F, size=10, bold=True, color=font.color) if bold else font
        if fmt:
            c.number_format = fmt
        if fill:
            c.fill = fill
        if align:
            c.alignment = Alignment(horizontal=align)

    def band(ws, row, last_col, border=False):
        for col in range(1, last_col + 1):
            ws.cell(row=row, column=col).fill = TFILL
            if border:
                ws.cell(row=row, column=col).border = TOP

    def finish(chart, axes=True):
        chart.roundedCorners = False
        if chart.title is not None:
            chart.title.overlay = False
        if chart.legend is not None:
            chart.legend.overlay = False
        if axes:
            chart.x_axis.delete = False
            chart.y_axis.delete = False
            chart.y_axis.number_format = "#,##0"

    # ---- Forecast inputs
    fi = wb.active
    fi.title = "Forecast inputs"
    TOT, SRC = L(2 + n), L(3 + n)
    cur_label = f"{MONTHS[cm - 1]} {cy}"
    put(fi, "A1", f"Forecast inputs: {cur_label} to December {cy}", TITLE)
    put(fi, "A2", "Blue = input you can change. Yellow = key assumption. Black = formula. " + basis, NOTE)
    header(fi, 4, [f"Input ({cur})"] + subs + ["Total", "Source / assumption"], [46] + [13] * (n + 1) + [90])
    rows = [
        (5, f"{cur_label} billed, complete days (through day {model['complete'].day})", model["mtd"], None,
         origin + " Later days are still posting and are forecast instead."),
        (6, "Daily rate, weekday", model["weekday"], YFILL,
         "Average of the latest complete weekdays, one-off days excluded."),
        (7, "Daily rate, weekend day", model["weekend"], YFILL,
         "Average of the latest complete weekend days, one-off days excluded."),
        (8, "Monthly one-off charge (reservations, marketplace)", model["one_off"], None,
         "Charges billed once a month, passed with --one-off. Zero if none were given."),
        (9, f"One-off charge still to bill in {cur_label}", model["one_off_pending"], None,
         "The part of row 8 not yet billed this month (already billed charges are in row 5)."),
    ]
    for r, label, vals, fill, src in rows:
        put(fi, f"A{r}", label)
        for i, v in enumerate(vals):
            put(fi, f"{L(2 + i)}{r}", round(v, 4), BLUE, EUR1, fill)
        put(fi, f"{TOT}{r}", f"=SUM(B{r}:{L(1 + n)}{r})", BLACK, EUR1, bold=True)
        put(fi, f"{SRC}{r}", src, NOTE)
    header(fi, 11, ["Days still to bill", "Weekdays", "Weekend days"])
    DROW = {}
    for i, (m, wd, wed) in enumerate(model["days"]):
        r = 12 + i
        DROW[m] = r
        put(fi, f"A{r}", f"{MONTHS[m - 1]} {cy}" + (" (remaining days)" if i == 0 else ""))
        put(fi, f"B{r}", wd, BLUE, "0")
        put(fi, f"C{r}", wed, BLUE, "0")
    NY = 12 + len(model["days"])
    put(fi, f"A{NY}", f"{cy + 1}, full year (used by the minimum scenario)")
    put(fi, f"B{NY}", model["next_year_days"][0], BLUE, "0")
    put(fi, f"C{NY}", model["next_year_days"][1], BLUE, "0")
    put(fi, f"D{NY}", "Calendar days. Public holidays are counted as normal days.", NOTE)
    FH = NY + 2
    header(fi, FH, [f"Forecast ({cur})"] + subs + ["Total", "How it is calculated"])
    FROW = {}
    for i, (m, _, _) in enumerate(model["days"]):
        r = FH + 1 + i
        FROW[m] = r
        put(fi, f"A{r}", f"{MONTHS[m - 1]} {cy} forecast")
        for j in range(n):
            c = L(2 + j)
            rate = f"$B${DROW[m]}*{c}$6+$C${DROW[m]}*{c}$7"
            put(fi, f"{c}{r}", f"={c}$5+{rate}+{c}$9" if i == 0 else f"={rate}+{c}$8", BLACK, EUR, FFILL)
        put(fi, f"{TOT}{r}", f"=SUM(B{r}:{L(1 + n)}{r})", BLACK, EUR, FFILL, bold=True)
        put(fi, f"{SRC}{r}", ("Billed days + remaining weekdays x weekday rate + remaining weekend days x weekend rate + one-off still to bill."
                              if i == 0 else "Weekdays x weekday rate + weekend days x weekend rate + monthly one-off. Assumes nothing changes."), NOTE)
    fi.freeze_panes = "B5"

    # ---- By subscription
    bs = wb.create_sheet("By subscription")
    BT, BST = L(3 + n), L(4 + n)
    put(bs, "A1", f"Azure cost by subscription and month, {years[0]}-{years[-1]}", TITLE)
    put(bs, "A2", f"{basis} {origin} Blue = billed amount. Shaded = forecast (see 'Forecast inputs').", NOTE)
    header(bs, 4, ["Year", "Month"] + subs + ["Total", "Status"], [8, 12] + [13] * (n + 1) + [11])
    ROW, r = {}, 5
    for y in years:
        for m in range(1, 13):
            ROW[(y, m)] = r
            vals = model["table"][y][m - 1]
            put(bs, f"A{r}", str(y), align="left")
            put(bs, f"B{r}", MONTHS[m - 1])
            for j in range(n):
                c = L(3 + j)
                if vals is None:
                    put(bs, f"{c}{r}", f"='Forecast inputs'!{L(2 + j)}{FROW[m]}", GREEN, EUR, FFILL)
                else:
                    put(bs, f"{c}{r}", round(vals[j], 2), BLUE, EUR)
            put(bs, f"{BT}{r}", f"=SUM(C{r}:{L(2 + n)}{r})", BLACK, EUR, FFILL if vals is None else None, bold=True)
            put(bs, f"{BST}{r}", "Forecast" if vals is None else "Actual", align="center")
            if m == 1 and y != years[0]:
                for col in range(1, 5 + n):
                    bs.cell(row=r, column=col).border = TOP
            r += 1
    LAST = r - 1
    YH = LAST + 3
    header(bs, YH, ["Year", "Yearly total"] + subs + ["Total", ""])
    YROW = {}
    for i, y in enumerate(years):
        rr = YH + 1 + i
        YROW[y] = rr
        put(bs, f"A{rr}", str(y), align="left", bold=True)
        put(bs, f"B{rr}", "Jan-Dec" + (" (incl. forecast)" if y == cy else ""))
        for j in range(n + 1):
            c = L(3 + j)
            put(bs, f"{c}{rr}", f"=SUMIFS({c}$5:{c}${LAST},$A$5:$A${LAST},$A{rr})", BLACK, EUR, bold=(j == n))
    if k > 1:
        CH = YH + k + 2
        header(bs, CH, ["", "Change"] + subs + ["Total", ""])
        rr = CH + 1
        for i in range(1, k):
            a, b = YROW[years[i]], YROW[years[i - 1]]
            put(bs, f"B{rr}", f"{years[i]} vs {years[i - 1]}, {cur}")
            put(bs, f"B{rr + 1}", f"{years[i]} vs {years[i - 1]}, %")
            for j in range(n + 1):
                c = L(3 + j)
                put(bs, f"{c}{rr}", f"={c}{a}-{c}{b}", BLACK, EUR)
                put(bs, f"{c}{rr + 1}", f'=IF({c}{b}=0,"n/a",{c}{a}/{c}{b}-1)', BLACK, PCT, align="right")
            rr += 2
        put(bs, f"A{rr + 1}", "'n/a' = no cost in the earlier year.", NOTE)
    bs.freeze_panes = "C5"

    # ---- Monthly comparison
    mo = wb.create_sheet("Monthly comparison")
    YC = {y: L(2 + i) for i, y in enumerate(years)}
    SC = L(2 + k)
    pairs = [(years[i], years[i - 1]) for i in range(1, k)] + ([(years[-1], years[0])] if k > 2 else [])
    put(mo, "A1", "Azure cost by month: " + ", ".join(map(str, years)), TITLE)
    put(mo, "A2", f"{basis} Shaded cells are forecasts.", NOTE)
    labels = ["Month"] + [str(y) for y in years] + [f"{cy} status"]
    for a, b in pairs:
        labels += [f"{a} vs {b} ({cur})", f"{a} vs {b} (%)"]
    header(mo, 4, labels, [16] + [12] * (len(labels) - 1))

    def deltas(r):
        for i, (a, b) in enumerate(pairs):
            ce, cp = L(3 + k + 2 * i), L(4 + k + 2 * i)
            put(mo, f"{ce}{r}", f"={YC[a]}{r}-{YC[b]}{r}", BLACK, EUR)
            put(mo, f"{cp}{r}", f"=IF({YC[b]}{r}=0,0,{YC[a]}{r}/{YC[b]}{r}-1)", BLACK, PCT)

    for m in range(1, 13):
        r = 4 + m
        fcst = model["table"][cy][m - 1] is None
        put(mo, f"A{r}", MONTHS[m - 1])
        for y in years:
            put(mo, f"{YC[y]}{r}", f"='By subscription'!{BT}{ROW[(y, m)]}", GREEN, EUR, FFILL if (fcst and y == cy) else None)
        put(mo, f"{SC}{r}", "Forecast" if fcst else "Actual", align="center")
        deltas(r)
    put(mo, "A17", "Year total", bold=True)
    put(mo, "A18", "Monthly average", bold=True)
    for y in years:
        put(mo, f"{YC[y]}17", f"=SUM({YC[y]}5:{YC[y]}16)", BLACK, EUR, bold=True)
        put(mo, f"{YC[y]}18", f"=AVERAGE({YC[y]}5:{YC[y]}16)", BLACK, EUR, bold=True)
    for r in (17, 18):
        deltas(r)
        band(mo, r, len(labels), border=(r == 17))
    put(mo, "A20", "Quarters and half years", bold=True)
    header(mo, 21, ["Period"] + labels[1:1 + k] + [""] + labels[2 + k:])
    for i, (label, a, b) in enumerate([("Q1 (Jan-Mar)", 5, 7), ("Q2 (Apr-Jun)", 8, 10), ("Q3 (Jul-Sep)", 11, 13),
                                       ("Q4 (Oct-Dec)", 14, 16), ("First half", 5, 10), ("Second half", 11, 16)]):
        r = 22 + i
        put(mo, f"A{r}", label)
        for y in years:
            put(mo, f"{YC[y]}{r}", f"=SUM({YC[y]}{a}:{YC[y]}{b})", BLACK, EUR)
        deltas(r)
    put(mo, "A28", f"Periods of {cy} that contain forecast months include those forecasts.", NOTE)
    mo.freeze_panes = "B5"
    ch = LineChart()
    ch.title = f"Monthly Azure cost ({cur})"
    ch.height, ch.width = 9, 24
    ch.add_data(Reference(mo, min_col=2, max_col=1 + k, min_row=4, max_row=16), titles_from_data=True)
    ch.set_categories(Reference(mo, min_col=1, min_row=5, max_row=16))
    palette = ["A6A6A6", "ED7D31", "1F3864", "70AD47", "7030A0", "00B0F0"]
    for s, color in zip(ch.series, palette[-k:] if k <= 3 else palette):
        s.graphicalProperties.line.solidFill = color
        s.graphicalProperties.line.width = 28000
        s.smooth = False
    ch.legend.position = "b"
    finish(ch)
    mo.add_chart(ch, "A30")

    # ---- Summary
    sm = wb.create_sheet("Summary")
    put(sm, "A1", f"Azure cost {years[0]}-{years[-1]}: comparison by year", TITLE)
    put(sm, "A2", f"{origin} {basis} {cy} includes forecasts from {cur_label}.", NOTE)
    header(sm, 4, ["Year", f"Total ({cur})", "Monthly average", f"Change vs year before ({cur})", "Change vs year before (%)",
                   "Lowest month", "Highest month", "December"], [30] + [15] * max(7, k + 3))
    for i, y in enumerate(years):
        r, c = 5 + i, YC[y]
        put(sm, f"A{r}", f"{y} (incl. forecast)" if y == cy else str(y), bold=True, align="left")
        put(sm, f"B{r}", f"='Monthly comparison'!{c}17", GREEN, EUR, bold=True)
        put(sm, f"C{r}", f"='Monthly comparison'!{c}18", GREEN, EUR)
        if i:
            put(sm, f"D{r}", f"=B{r}-B{r - 1}", BLACK, EUR)
            put(sm, f"E{r}", f"=IF(B{r - 1}=0,0,B{r}/B{r - 1}-1)", BLACK, PCT)
        put(sm, f"F{r}", f"=MIN('Monthly comparison'!{c}5:{c}16)", GREEN, EUR)
        put(sm, f"G{r}", f"=MAX('Monthly comparison'!{c}5:{c}16)", GREEN, EUR)
        put(sm, f"H{r}", f"='Monthly comparison'!{c}16", GREEN, EUR, FFILL if y == cy else None)
    TR = 5 + k
    put(sm, f"A{TR}", "Total of all years", bold=True)
    put(sm, f"B{TR}", f"=SUM(B5:B{TR - 1})", BLACK, EUR, bold=True)
    band(sm, TR, 8, border=True)
    S0 = TR + 2
    put(sm, f"A{S0}", f"Yearly cost by subscription ({cur})", bold=True)
    extra = ([f"{years[-1]} vs {years[-2]} ({cur})", f"{years[-1]} vs {years[-2]} (%)"] if k > 1 else []) + [f"Share of {cy}"]
    header(sm, S0 + 1, ["Subscription"] + [str(y) for y in years] + extra)
    lastc, prevc = L(1 + k), L(k)
    STOT = S0 + 2 + n
    for j, s in enumerate(subs + ["Total"]):
        r, c = S0 + 2 + j, L(3 + j)
        put(sm, f"A{r}", s, bold=(j == n))
        for i, y in enumerate(years):
            put(sm, f"{L(2 + i)}{r}", f"='By subscription'!{c}{YROW[y]}", GREEN, EUR, bold=(j == n))
        col = 2 + k
        if k > 1:
            put(sm, f"{L(col)}{r}", f"={lastc}{r}-{prevc}{r}", BLACK, EUR)
            put(sm, f"{L(col + 1)}{r}", f'=IF({prevc}{r}=0,"n/a",{lastc}{r}/{prevc}{r}-1)', BLACK, PCT, align="right")
            col += 2
        put(sm, f"{L(col)}{r}", f"=IF(${lastc}${STOT}=0,0,{lastc}{r}/${lastc}${STOT})", BLACK, PCT)
    band(sm, STOT, len(extra) + k + 1, border=True)
    R0 = STOT + 2
    put(sm, f"A{R0}", f"Run rate at year end ({cur})", bold=True)
    header(sm, R0 + 1, ["Measure"] + [str(y) for y in years])
    for off, label, f in ((2, "Average month of the last quarter (Oct-Dec)", "='Monthly comparison'!{c}25/3"),
                          (3, "Last quarter as a full year (x 4)", "='Monthly comparison'!{c}25*4"),
                          (4, "December as a full year (x 12)", "='Monthly comparison'!{c}16*12")):
        put(sm, f"A{R0 + off}", label)
        for y in years:
            put(sm, f"{L(2 + years.index(y))}{R0 + off}", f.format(c=YC[y]), GREEN, EUR)
    N0 = R0 + 6
    put(sm, f"A{N0}", "Notes", bold=True)
    notes = [
        f"Scope: {n - (NOT_SPLIT in subs)} subscription(s). Not included: licences, support plans and anything billed outside these subscriptions.",
        f"Billed months: up to {MONTHS[cm - 2] if cm > 1 else 'December'} {cy if cm > 1 else cy - 1}. {cur_label}: billed through day {model['complete'].day} plus forecast days. Later months of {cy}: forecast.",
        "Forecast method and inputs are on the 'Forecast inputs' sheet; change the blue cells there and every sheet updates.",
        f"The '{fname}' sheet holds scenarios, not predictions: its growth rates are assumptions.",
        "Colours: blue = data or input, black = formula, green = taken from another sheet, shaded = forecast.",
    ]
    for i, t in enumerate(notes):
        put(sm, f"A{N0 + 1 + i}", t, NOTE)
    bar = BarChart()
    bar.type = "col"
    bar.title = f"Yearly Azure cost ({cur})"
    bar.height, bar.width = 8, 14
    bar.add_data(Reference(sm, min_col=2, min_row=4, max_row=4 + k), titles_from_data=True)
    bar.set_categories(Reference(sm, min_col=1, min_row=5, max_row=4 + k))
    bar.series[0].graphicalProperties.solidFill = "1F3864"
    bar.legend = None
    finish(bar)
    sm.add_chart(bar, f"{L(max(9, k + len(extra) + 2) + 1)}4")

    # ---- Scenarios
    sc = wb.create_sheet(fname)
    put(sc, "A1", f"Azure cost forecast {fyears[0]}-{fyears[-1]}: scenarios", TITLE)
    put(sc, "A2", "Blue on yellow = assumption you can change; everything else recalculates. Growth is applied to the "
                  f"year before (compounded). {basis}", NOTE)
    header(sc, 4, ["Scenario", f"Base monthly cost ({cur})"] + [f"Growth {y}" for y in fyears] + [f"{y} per month" for y in fyears]
           + [str(y) for y in fyears] + ["Three-year total", "Yearly average", "Basis of the assumptions"],
           [36, 14, 10, 10, 10, 12, 12, 12, 12, 12, 12, 14, 13, 100])
    minimum = {"name": "Minimum (today's consumption, no growth)", "base": "min", "growth": [0, 0, 0],
               "basis": "Today's daily rates from 'Forecast inputs' over the next full year, plus the monthly one-off charge. "
                        "The floor if nothing changes."}
    for i, s in enumerate(model["scenarios"] + [minimum]):
        r = 5 + i
        put(sc, f"A{r}", s["name"], bold=(i == 0))
        if s["base"] == "min":
            put(sc, f"B{r}", f"=('Forecast inputs'!{TOT}6*'Forecast inputs'!B{NY}+'Forecast inputs'!{TOT}7*'Forecast inputs'!C{NY})/12"
                             f"+'Forecast inputs'!{TOT}8", GREEN, EUR)
        elif s["base"] is None:
            put(sc, f"B{r}", f"='Monthly comparison'!{YC[cy]}18", GREEN, EUR, YFILL)
        else:
            put(sc, f"B{r}", s["base"], BLUE, EUR, YFILL)
        for col, g in zip("CDE", s["growth"]):
            put(sc, f"{col}{r}", g, BLUE, "0.0%", YFILL)
        put(sc, f"F{r}", f"=B{r}*(1+C{r})", BLACK, EUR)
        put(sc, f"G{r}", f"=F{r}*(1+D{r})", BLACK, EUR)
        put(sc, f"H{r}", f"=G{r}*(1+E{r})", BLACK, EUR)
        for col, src in zip("IJK", "FGH"):
            put(sc, f"{col}{r}", f"={src}{r}*12", BLACK, EUR, bold=(i == 0))
        put(sc, f"L{r}", f"=SUM(I{r}:K{r})", BLACK, EUR, TFILL, bold=True)
        put(sc, f"M{r}", f"=L{r}/3", BLACK, EUR)
        put(sc, f"N{r}", s["basis"], NOTE)
    H0 = 5 + len(model["scenarios"]) + 2
    put(sc, f"A{H0}", f"{model['scenarios'][0]['name']} against history", bold=True)
    header(sc, H0 + 1, ["Year", f"Yearly cost ({cur})", "Per month", f"Change vs year before ({cur})",
                        "Change vs year before (%)", "Type"])
    hist = [(str(y), f"=Summary!B{5 + i}", "Actual + forecast" if y == cy else "Actual") for i, y in enumerate(years)]
    hist += [(str(y), f"={col}5", "Scenario") for y, col in zip(fyears, "IJK")]
    for i, (y, f, kind) in enumerate(hist):
        r = H0 + 2 + i
        scn = kind == "Scenario"
        put(sc, f"A{r}", y, align="left", bold=True)
        put(sc, f"B{r}", f, BLACK if scn else GREEN, EUR, FFILL if scn else None, bold=True)
        put(sc, f"C{r}", f"=B{r}/12", BLACK, EUR)
        if i:
            put(sc, f"D{r}", f"=B{r}-B{r - 1}", BLACK, EUR)
            put(sc, f"E{r}", f"=IF(B{r - 1}=0,0,B{r}/B{r - 1}-1)", BLACK, PCT)
        put(sc, f"F{r}", kind)
    HL = H0 + 1 + len(hist)
    put(sc, f"A{HL + 1}", f"Total {fyears[0]}-{fyears[-1]}", bold=True)
    put(sc, f"B{HL + 1}", f"=SUM(B{HL - 2}:B{HL})", BLACK, EUR, bold=True)
    put(sc, f"A{HL + 2}", f"Total {years[0]}-{years[-1]}", bold=True)
    put(sc, f"B{HL + 2}", f"=SUM(B{H0 + 2}:B{HL - 3})", BLACK, EUR, bold=True)
    band(sc, HL + 1, 6, border=True)
    band(sc, HL + 2, 6)
    put(sc, f"A{HL + 4}", "Reading notes", bold=True)
    for i, t in enumerate([
        "These are scenarios, not predictions: the growth rates are assumptions. Compare them with the growth in the history table.",
        "The minimum scenario is the spend if nothing changes from today: use it as the floor for any spending commitment.",
        "Check when existing reservations and savings plans expire: after expiry those resources go back to full price unless renewed.",
        "Not included: licences, support plans, costs billed outside these subscriptions, taxes.",
    ]):
        put(sc, f"A{HL + 5 + i}", t, NOTE)
    sc.freeze_panes = "B5"
    fb = BarChart()
    fb.type = "col"
    fb.title = f"Yearly Azure cost, history and {model['scenarios'][0]['name'].lower()} ({cur})"
    fb.height, fb.width = 9, 18
    fb.add_data(Reference(sc, min_col=2, min_row=H0 + 1, max_row=HL), titles_from_data=True)
    fb.set_categories(Reference(sc, min_col=1, min_row=H0 + 2, max_row=HL))
    fb.series[0].graphicalProperties.solidFill = "1F3864"
    fb.legend = None
    finish(fb)
    sc.add_chart(fb, f"H{H0}")

    wb._sheets = [wb["Summary"], wb["Monthly comparison"], wb["By subscription"], wb[fname], wb["Forecast inputs"]]
    for ws in wb.worksheets:
        ws.sheet_view.showGridLines = False
    wb.active = 0
    wb.properties.creator = "azure-finops-review"
    wb.save(out)


def fetch(a):
    """Read monthly history and recent daily cost from Cost Management (read-only)."""
    today = dt.date.today()
    complete = dt.date.fromisoformat(a.complete_through) if a.complete_through else today - dt.timedelta(days=2)
    cy, cm = complete.year, complete.month
    fc.PAUSE, fc.ALLOW_PARTIAL = a.pause, a.allow_partial
    ids = {s.strip() for s in a.subscriptions.split(",")} if a.subscriptions else None
    subs = fc.subscriptions(a.tenant, ids)
    monthly, currency = collections.defaultdict(dict), collections.Counter()
    for y in range(cy - a.years + 1, cy + 1):
        end = dt.date(y, 12, 31) if y < cy else dt.date(cy, cm, 1) - dt.timedelta(days=1)
        if end.year != y:
            continue  # current month is January: no complete month in this year yet
        print(f"  reading {y} ...", file=sys.stderr)
        for name, r in fc.collect(subs, f"{y}-01-01", end.isoformat(), granularity="Monthly", cost_type=a.cost_type):
            d = fc.day_of(r)
            monthly[(d.year, d.month)][name] = monthly[(d.year, d.month)].get(name, 0.0) + r["Cost"]
            currency[r.get("Currency", "")] += 1
    q_start = min(dt.date(cy, cm, 1), complete - dt.timedelta(days=max(a.rate_days * 2, 21)))
    print("  reading recent daily cost ...", file=sys.stderr)
    daily = collections.defaultdict(dict)
    for name, r in fc.collect(subs, q_start.isoformat(), today.isoformat(), cost_type=a.cost_type):
        d = fc.day_of(r)
        daily[name][d] = daily[name].get(d, 0.0) + r["Cost"]
        currency[r.get("Currency", "")] += 1
    if not monthly and not daily:
        sys.exit("No cost data returned for this scope and period")
    cur = (currency.most_common(1)[0][0] if currency else "") or "EUR"
    return monthly, daily, [n for _, n, _ in subs], today, complete, cur


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tenant", help="tenantId to restrict subscriptions to (recommended)")
    p.add_argument("--subscriptions", help="comma-separated subscription IDs")
    p.add_argument("--years", type=int, default=3, help="years to compare, ending with the current one (default 3)")
    p.add_argument("--out", help="output .xlsx file (default: azure-cost-FIRST-LAST.xlsx)")
    p.add_argument("--cost-type", default="ActualCost", choices=["ActualCost", "AmortizedCost"])
    p.add_argument("--rate-days", type=int, default=10, help="complete days used for run rates")
    p.add_argument("--complete-through", help="last complete day (default: today-2)")
    p.add_argument("--one-off", action="append",
                   help="monthly charge AMOUNT@DAY[:SUBSCRIPTION NAME], e.g. 500@13:Production (repeatable)")
    p.add_argument("--scenario", action="append",
                   help='"NAME:BASE:G1,G2,G3" - monthly base cost (or "auto") and growth %% for the next three years (repeatable)')
    p.add_argument("--spike-factor", type=float, default=1.5)
    p.add_argument("--pause", type=float, default=2.0, help="seconds between subscriptions (throttling)")
    p.add_argument("--allow-partial", action="store_true", help="continue if some subscriptions fail")
    p.add_argument("--demo", action="store_true", help="write synthetic demo data (no Azure calls)")
    p.add_argument("--anonymize", action="store_true", help="replace subscription names with 'Subscription 1..n'")
    a = p.parse_args()
    if not 1 <= a.years <= 6:
        sys.exit("--years must be between 1 and 6")

    scenarios = parse_scenarios(a.scenario)
    if a.demo:
        today = dt.date(2026, 11, 18)
        complete = today - dt.timedelta(days=2)
        monthly, daily, subs, demo_sc = demo_inputs(today)
        cur, one_off = "EUR", a.one_off or ["300@13:Production"]
        scenarios = scenarios or demo_sc
    else:
        monthly, daily, subs, today, complete, cur = fetch(a)
        one_off = a.one_off
    model = build_model(monthly, daily, subs, today, complete, a.years, cur, a.cost_type, a.rate_days, one_off,
                        a.spike_factor, scenarios, demo=a.demo)
    if a.anonymize:
        model = anonymize(model)
    out = a.out or f"azure-cost-{model['years'][0]}-{model['years'][-1]}.xlsx"
    render(model, out)
    t = totals(model)
    print(f"Workbook written: {pathlib.Path(out).resolve()}")
    print(f"  Scope: {', '.join(model['subs'])}  [{model['cost_type']}, {cur}]")
    for y in model["years"]:
        tag = " (incl. forecast)" if y == model["cur_year"] else ""
        print(f"  {y}: {t['year'][y]:,.0f}{tag}; monthly average {t['year'][y] / 12:,.0f}")
    print("  Forecast months: " + "; ".join(f"{MONTHS[m - 1]} {v:,.0f}" for m, v in t["forecast_months"].items()))
    print(f"  Run rates: weekday {sum(model['weekday']):,.1f}/day, weekend {sum(model['weekend']):,.1f}/day; "
          f"monthly one-offs {sum(model['one_off']):,.0f}")
    for name, base, vals in t["scenarios"]:
        print(f"  Scenario {name}: base {base:,.0f}/month -> " + ", ".join(f"{v:,.0f}" for v in vals) + f"; total {sum(vals):,.0f}")
    if not a.scenario and not a.demo:
        print("  note: no --scenario given; the scenario sheet uses example growth of 5% a year. Ask the user for their assumptions.")
    print("  note: formulas are calculated when the file is opened in Excel or LibreOffice.")


if __name__ == "__main__":
    main()
