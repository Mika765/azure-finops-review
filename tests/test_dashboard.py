"""Tests for the dashboard builder (no Azure access). Run: python tests/test_dashboard.py"""
import datetime as dt
import json
import pathlib
import subprocess
import sys
import tempfile

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "plugins/azure-finops-review/skills/azure-finops-review/scripts"
sys.path.insert(0, str(SCRIPTS))
import finops_dashboard as fd  # noqa: E402


def _data():
    today = dt.date(2026, 3, 18)
    return fd.build(fd.demo_records("2026-03", today), "2026-03", today - dt.timedelta(days=2), today,
                    ["A", "B"], "EUR", "ActualCost", one_off=["310@5"], budget=12000, findings=fd.DEMO_FINDINGS)


def test_numbers_are_consistent():
    d = _data()
    f = d["forecast"]
    actual = sum(x["v"] for x in d["daily"] if x["kind"] == "actual")
    assert abs(actual - f["mtd"]) < 0.5, "month-to-date must equal the sum of actual days"
    rest = f["remaining_weekdays"] * f["weekday_rate"] + f["remaining_weekend_days"] * f["weekend_rate"]
    assert abs(f["mtd"] + rest + f["one_offs"] - f["forecast"]) < 0.5, "forecast must equal its stated formula"
    assert f["low"] <= f["forecast"] <= f["high"]
    assert abs(sum(s["mtd"] for s in d["subs"]) - f["mtd"]) < 0.5, "subscriptions must add up to the total"
    assert abs(sum(s["mtd"] for s in d["services"]) - f["mtd"]) < 0.5, "services (incl. Other) must add up to the total"
    assert len(d["daily"]) == 31 and len(d["prev_daily"]) == 28
    assert [x["kind"] for x in d["daily"]].count("partial") == 2
    assert any(x["oneoff"] for x in d["daily"]), "the one-off day must be flagged"
    assert len(d["services"]) <= fd.TOP_SERVICES + 1


def test_render_is_self_contained_and_inert():
    d = _data()
    d["subs"][0]["name"] = "</script><img src=x onerror=alert(1)>"
    with tempfile.TemporaryDirectory() as t:
        out = pathlib.Path(t) / "d.html"
        fd.render(d, out)
        html = out.read_text(encoding="utf-8")
    assert html.count("</script>") == 1, "data must not be able to close the script tag"
    assert "<img src=x" not in html
    assert "http://" not in html.replace("http://www.w3.org/2000/svg", "") and "https://" not in html, "no external resources"
    blob = html.split("const D = ", 1)[1].split(";\n", 1)[0]
    assert json.loads(blob)["subs"][0]["name"].startswith("</script>")


def test_anonymize():
    d = _data()
    d["subs"][0]["name"] = d["meta"]["scope"][0] = "Contoso-Prod"
    d["findings"][0]["title"] = "Stop cluster in Contoso-Prod"
    d = fd.anonymize(d)
    blob = json.dumps(d)
    assert "Contoso-Prod" not in blob and d["meta"]["anonymized"]
    assert d["subs"][0]["name"] == "Subscription 1" and "Subscription 1" in d["findings"][0]["title"]


def test_cli_demo():
    with tempfile.TemporaryDirectory() as t:
        out = pathlib.Path(t) / "demo.html"
        r = subprocess.run([sys.executable, str(SCRIPTS / "finops_dashboard.py"), "--demo", "--out", str(out)],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert out.stat().st_size > 20000


if __name__ == "__main__":
    for t in (test_numbers_are_consistent, test_render_is_self_contained_and_inert, test_anonymize, test_cli_demo):
        t()
    print("OK: dashboard numbers consistent, output self-contained and inert, anonymize works, CLI demo works")
