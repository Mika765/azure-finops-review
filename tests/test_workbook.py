"""Tests for the multi-year workbook builder (no Azure access). Run: python tests/test_workbook.py"""
import datetime as dt
import pathlib
import re
import subprocess
import sys
import tempfile

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "plugins/azure-finops-review/skills/azure-finops-review/scripts"
sys.path.insert(0, str(SCRIPTS))
import finops_workbook as fw  # noqa: E402

TODAY = dt.date(2026, 11, 18)


def _model(one_off=("300@13:Production",), scenarios=None):
    monthly, daily, subs, demo_sc = fw.demo_inputs(TODAY)
    return fw.build_model(monthly, daily, subs, TODAY, TODAY - dt.timedelta(days=2), 3, one_off=list(one_off),
                          scenarios=scenarios or demo_sc, demo=True)


def test_model_numbers():
    m = _model()
    t = fw.totals(m)
    assert m["years"] == [2024, 2025, 2026]
    assert [mm for mm, _, _ in m["days"]] == [11, 12], "forecast months are the current month and the rest of the year"
    assert m["days"][0][1] + m["days"][0][2] == 14, "16 November complete -> 14 days left"
    assert m["table"][2026][9] is not None and m["table"][2026][10] is None, "October billed, November forecast"
    prod = m["subs"].index("Production")
    assert m["one_off"][prod] == 300 and m["one_off_pending"][prod] == 0, "charge on the 13th is already billed"
    assert abs(m["weekday"][prod] - 151) < 3, "the one-off day must not inflate the weekday rate"
    assert abs(sum(t["by_month"][2026]) - t["year"][2026]) < 0.01
    name, base, vals = t["scenarios"][0]
    assert abs(vals[2] - 9500 * 1.05 * 1.08 * 1.10 * 12) < 0.01, "growth compounds year on year"
    assert t["scenarios"][-1][0] == "Minimum" and t["scenarios"][-1][2][0] == t["scenarios"][-1][2][2]


def test_one_off_pending_and_not_split():
    m = _model(one_off=("300@25:Production", "50@1"))
    assert m["subs"][-1] == fw.NOT_SPLIT
    assert m["one_off_pending"][m["subs"].index("Production")] == 300, "day 25 is after the last complete day"
    assert m["one_off"][-1] == 50 and m["one_off_pending"][-1] == 0


def test_parse_scenarios():
    s = fw.parse_scenarios(["Main:19000:10,15,20", "Auto:auto:5,5,5"])
    assert s[0]["base"] == 19000 and s[0]["growth"] == [0.10, 0.15, 0.20] and s[1]["base"] is None


def test_workbook_file():
    try:
        from openpyxl import load_workbook
    except ImportError:
        print("SKIPPED test_workbook_file: openpyxl not installed")
        return
    m = fw.anonymize(_model())
    with tempfile.TemporaryDirectory() as t:
        out = pathlib.Path(t) / "w.xlsx"
        fw.render(m, out)
        wb = load_workbook(out)
        names = wb.sheetnames
        assert names == ["Summary", "Monthly comparison", "By subscription", "Forecast 2027-2029", "Forecast inputs"]
        text, formulas = [], 0
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for c in row:
                    if isinstance(c.value, str):
                        text.append(c.value)
                        if c.value.startswith("="):
                            formulas += 1
                            for ref in re.findall(r"'([^']+)'!", c.value):
                                assert ref in names, f"formula points to a missing sheet: {c.value}"
        blob = "\n".join(text)
        assert formulas > 300, "the workbook must be built from formulas, not pasted totals"
        assert "Subscription 1" in blob and "Production" not in blob, "anonymize must remove subscription names"
        assert "#REF" not in blob
        assert len(wb["Monthly comparison"]._charts) == 1 and len(wb["Summary"]._charts) == 1


def test_cli_demo():
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        print("SKIPPED test_cli_demo: openpyxl not installed")
        return
    with tempfile.TemporaryDirectory() as t:
        out = pathlib.Path(t) / "demo.xlsx"
        r = subprocess.run([sys.executable, str(SCRIPTS / "finops_workbook.py"), "--demo", "--out", str(out)],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert "Scenario Main scenario" in r.stdout and out.stat().st_size > 10000


if __name__ == "__main__":
    for t in (test_model_numbers, test_one_off_pending_and_not_split, test_parse_scenarios, test_workbook_file, test_cli_demo):
        t()
    print("OK: workbook model consistent, one-offs and scenarios parsed, file built from formulas, anonymize works, CLI demo works")
