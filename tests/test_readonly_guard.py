"""Tests for the read-only guard. Run: python -m pytest tests  (or: python tests/test_readonly_guard.py)"""
import json
import pathlib
import subprocess
import sys

GUARD = pathlib.Path(__file__).resolve().parents[1] / "plugins/azure-finops-review/skills/azure-finops-review/hooks/readonly_guard.py"
sys.path.insert(0, str(GUARD.parent))
from readonly_guard import check  # noqa: E402

ALLOWED = [
    "az account list --all -o json",
    "az account show",
    "az account get-access-token --resource https://management.azure.com",
    "MSYS_NO_PATHCONV=1 az vm list -d --subscription 00000000-0000-0000-0000-000000000000",
    "az aks show -g rg -n aks --query powerState",
    "az graph query -q \"resources | take 5\" --first 1000",
    "az monitor metrics list --resource /x --metric Transactions --top 200",
    "az monitor activity-log list --max-events 5000 --start-time 2026-11-01",
    "az monitor log-analytics query -w abc --analytics-query \"Usage | take 5\"",
    "az monitor diagnostic-settings list --resource /x",
    "az consumption budget list --subscription s",
    "az account management-group list",
    "az reservations reservation-order list",
    "az storage account show-usage -l westeurope",
    "az rest --method get --url https://management.azure.com/providers/Microsoft.Management/managementGroups/x/providers/Microsoft.Consumption/budgets?api-version=2023-05-01",
    "az rest --url /subscriptions/s/resourceGroups?api-version=2021-04-01",
    "az rest --method post --url /subscriptions/s/providers/Microsoft.CostManagement/query?api-version=2023-11-01 --body @b.json",
    "az aks get-credentials -g rg -n aks",
    "python scripts/finops_cost.py --forecast 2026-11 | tail -5",
    "terraform plan -out tf.plan && terraform show tf.plan",
    "terraform state list",
    "kubectl get pods -A && kubectl describe node x",
    "kubectl config get-contexts",
    "helm list -A",
    "Get-AzVM -Status | Select-Object Name",
    "Invoke-AzRestMethod -Path '/subscriptions/s?api-version=2022-12-01' -Method GET",
    "git log --oneline | head",
    "grep -r 'az vm delete' docs/",
    "echo done",
]

DENIED = [
    "az vm deallocate -g rg -n vm",
    "az vm delete --ids /x --yes",
    "az group delete -n rg",
    "az aks stop -g rg -n aks",
    "az aks nodepool scale -g rg --cluster-name aks -n np -c 1",
    "az account set --subscription s",
    "az config set core.output=table",
    "az monitor diagnostic-settings create --resource /x -n d --workspace w --logs '[]'",
    "az monitor diagnostic-settings update --resource /x -n d",
    "az role assignment create --assignee me --role Reader",
    "az storage account update -n sa --sku Standard_LRS",
    "az resource tag --ids /x --tags a=b",
    "az lock delete -n l -g rg",
    "az deployment group create -g rg -f main.bicep",
    "az rest --method put --url /subscriptions/s/resourceGroups/rg?api-version=2021-04-01 --body {}",
    "az rest --method delete --url /x",
    "az rest --method patch --url /x --body {}",
    "az rest --method post --url /subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm/deallocate?api-version=2023-03-01",
    "az rest --url /subscriptions/s/x/restart --body {}",
    "echo ok && az vm stop -g rg -n vm",
    "az vm list; az vm start -g rg -n vm",
    "MSYS_NO_PATHCONV=1 az.cmd webapp stop -g rg -n app",
    "C:/Program Files/Microsoft SDKs/Azure/CLI2/wbin/az.cmd vm delete -n x",
    "x=$(az vm delete -n x)",
    "terraform apply -auto-approve",
    "terraform destroy",
    "terraform import a.b /x",
    "terraform state rm a.b",
    "kubectl delete pod x",
    "kubectl scale deploy x --replicas=0",
    "kubectl apply -f m.yaml",
    "helm uninstall x",
    "azd down",
    "Remove-AzResourceGroup -Name rg -Force",
    "Stop-AzVM -Name vm -ResourceGroupName rg",
    "Set-AzContext -Subscription s",
    "Invoke-AzRestMethod -Path '/x' -Method DELETE",
    "curl -X DELETE https://management.azure.com/subscriptions/s/resourceGroups/rg",
    "Invoke-RestMethod -Method Put -Uri https://management.azure.com/x",
]


def test_allowed():
    bad = [c for c in ALLOWED if check(c)]
    assert not bad, f"wrongly blocked: {bad}"


def test_denied():
    bad = [c for c in DENIED if not check(c)]
    assert not bad, f"not blocked: {bad}"


def test_hook_protocol():
    ev = json.dumps({"tool_name": "Bash", "tool_input": {"command": "az group delete -n rg"}})
    out = subprocess.run([sys.executable, str(GUARD)], input=ev, capture_output=True, text=True)
    assert out.returncode == 0
    d = json.loads(out.stdout)["hookSpecificOutput"]
    assert d["permissionDecision"] == "deny" and "read-only" in d["permissionDecisionReason"]
    ev = json.dumps({"tool_name": "Bash", "tool_input": {"command": "az vm list"}})
    out = subprocess.run([sys.executable, str(GUARD)], input=ev, capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip() == ""


if __name__ == "__main__":
    for t in (test_allowed, test_denied, test_hook_protocol):
        t()
    print(f"OK: {len(ALLOWED)} allowed, {len(DENIED)} denied, hook protocol")
