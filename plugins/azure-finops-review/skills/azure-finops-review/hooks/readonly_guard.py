#!/usr/bin/env python3
"""Claude Code PreToolUse hook: keep Azure / IaC / Kubernetes commands read-only.

Reads the hook event JSON on stdin. If the shell command would change anything in
Azure (or in Terraform state / a Kubernetes cluster), it denies the tool call and
tells Claude why. Everything else passes through untouched.

This is a safety net, not a sandbox: it inspects command text, so a write hidden
inside another script or built from variables can slip past it. The real
guarantee is to run the review with an identity that only has read roles
(Reader, Cost Management Reader, Log Analytics Reader).
"""
import json
import re
import shlex
import sys

# --- Azure CLI -------------------------------------------------------------
# The last word of the command group decides (e.g. `az vm list` -> "list").
AZ_READ_VERBS = {
    "list", "show", "query", "get", "version", "help", "export", "wait",
    "login", "logout", "find", "check-name-availability", "list-skus",
}
AZ_READ_PREFIXES = ("list-", "show-", "get-")
AZ_READ_EXACT = {  # whole command groups that only read
    ("account", "show"), ("account", "list"), ("account", "get-access-token"),
    ("account", "tenant", "list"), ("extension", "list"), ("bicep", "version"),
    ("graph", "query"), ("config", "get"),
}
AZ_DENY_EXACT = {("account", "set"), ("account", "clear"), ("config", "set"), ("config", "unset")}
# POST endpoints that only read data
REST_READ_POST = re.compile(
    r"/providers/Microsoft\.(CostManagement/(query|forecast)|ResourceGraph/resources"
    r"|Consumption/[^?]*|Advisor/[^?]*/(list|query)\w*)|/(query|listCost\w*)\?|/api/query",
    re.I)

# --- Terraform / Bicep / kubectl / helm ------------------------------------
TF_READ = {"plan", "show", "validate", "fmt", "output", "version", "providers", "graph",
           "init", "console", "-version", "--version", "-help", "--help", "workspace"}
TF_STATE_READ = {"list", "show", "pull"}
TF_WS_READ = {"list", "show", "select"}
KUBECTL_READ = {"get", "describe", "logs", "top", "version", "explain", "api-resources",
                "api-versions", "cluster-info", "auth", "config", "diff", "events", "wait"}
KUBECTL_CONFIG_READ = {"view", "get-contexts", "current-context", "get-clusters", "use-context"}
HELM_READ = {"list", "ls", "get", "status", "history", "show", "search", "template", "version",
             "lint", "env", "repo", "dependency"}

# --- PowerShell Az module ---------------------------------------------------
PS_WRITE = re.compile(
    r"\b(New|Set|Remove|Update|Start|Stop|Restart|Add|Register|Unregister|Enable|Disable|Move|"
    r"Rename|Reset|Suspend|Resume|Import|Publish|Grant|Revoke|Clear|Install|Sync|Approve|Deny|"
    r"Undo|Restore|Backup|Submit|Send)-Az[A-Za-z]+", re.I)
PS_REST = re.compile(r"\bInvoke-AzRestMethod\b(?P<rest>.*)", re.I | re.S)
HTTP_WRITE = re.compile(r"(-X\s*|--request\s+|-Method\s+|--method\s+|-m\s+)['\"]?(PUT|PATCH|DELETE|POST)\b", re.I)

AZ_PATH = re.compile(
    r"""^\s*(?:[A-Za-z_]\w*=\S*\s+)*&?\s*["']?(?:[A-Za-z]:)?[^"';|&]*[\\/]az(?:\.cmd|\.exe|\.bat)?["']?\s+(?P<rest>.*)$""",
    re.I | re.S)
SEGMENT_SPLIT =re.compile(r"\|\||&&|[;|\n]|(?<![<>])&(?!&)")


def tokens(seg):
    try:
        return shlex.split(seg, posix=True)
    except ValueError:
        return seg.split()


def opt(toks, *names):
    for i, t in enumerate(toks):
        for n in names:
            if t == n and i + 1 < len(toks):
                return toks[i + 1]
            if t.startswith(n + "="):
                return t.split("=", 1)[1]
    return None


def check_az(toks):
    words = []
    for t in toks:
        if t.startswith("-"):
            break
        words.append(t.lower())
    if not words:
        return None
    if any(w.startswith("$") or w.startswith("`") for w in words):
        return "az command built from a variable cannot be verified as read-only"
    if words[0] == "rest":
        method = (opt(toks, "--method", "-m") or ("post" if opt(toks, "--body", "-b") else "get")).lower()
        url = opt(toks, "--url", "--uri", "-u") or ""
        if method == "get" or method == "head":
            return None
        if method == "post" and REST_READ_POST.search(url):
            return None
        return f"az rest --method {method.upper()} changes resources (only GET and read-only query POSTs are allowed)"
    t = tuple(words)
    if t in AZ_DENY_EXACT or t[:2] in AZ_DENY_EXACT:
        return f"'az {' '.join(t)}' changes local Azure CLI context; pass --subscription instead"
    if t in AZ_READ_EXACT or t[:2] in AZ_READ_EXACT:
        return None
    verb = words[-1]
    if verb in AZ_READ_VERBS or verb.startswith(AZ_READ_PREFIXES):
        return None
    if len(words) == 1 and words[0] in ("--version", "version", "find", "upgrade"):
        return None
    return f"'az {' '.join(words)}' is not a read-only command"


def check_terraform(toks):
    args = [t for t in toks[1:] if not t.startswith("-chdir")]
    if not args:
        return None
    sub = args[0]
    if sub == "state":
        return None if len(args) > 1 and args[1] in TF_STATE_READ else "terraform state changes are not allowed"
    if sub == "workspace":
        return None if len(args) > 1 and args[1] in TF_WS_READ else "terraform workspace changes are not allowed"
    if sub in TF_READ:
        return None
    return f"'terraform {sub}' changes infrastructure or state"


def check_kubectl(toks):
    args = [t for t in toks[1:] if not t.startswith("-")]
    if not args:
        return None
    if args[0] == "config":
        return None if len(args) > 1 and args[1] in KUBECTL_CONFIG_READ else "kubectl config changes are not allowed"
    if args[0] == "auth":
        return None if len(args) > 1 and args[1] in ("can-i", "whoami") else "kubectl auth changes are not allowed"
    return None if args[0] in KUBECTL_READ else f"'kubectl {args[0]}' changes the cluster"


def check_helm(toks):
    args = [t for t in toks[1:] if not t.startswith("-")]
    return None if not args or args[0] in HELM_READ else f"'helm {args[0]}' changes the cluster"


def check_segment(seg):
    seg = seg.strip()
    if not seg:
        return None
    m = PS_WRITE.search(seg)
    if m:
        return f"PowerShell '{m.group(0)}' changes Azure resources"
    m = PS_REST.search(seg)
    if m and HTTP_WRITE.search(m.group("rest")):
        return "Invoke-AzRestMethod with a write method is not allowed"
    if re.search(r"management\.azure\.com|graph\.microsoft\.com", seg, re.I) and \
            re.search(r"\b(curl|wget|Invoke-RestMethod|Invoke-WebRequest|irm|iwr)\b", seg, re.I) and \
            HTTP_WRITE.search(seg) and not REST_READ_POST.search(seg):
        return "HTTP write call to the Azure / Graph API is not allowed"
    # az called by a full path, possibly unquoted with spaces (C:/Program Files/.../az.cmd vm ...)
    m = AZ_PATH.match(seg)
    if m:
        return check_az(tokens(m.group("rest")))
    toks = tokens(seg)
    # skip env assignments and wrappers: MSYS_NO_PATHCONV=1 az ..., sudo, &, time
    while toks and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]) or toks[0] in ("sudo", "time", "&", "env", "command", "exec")):
        toks = toks[1:]
    if not toks:
        return None
    exe = re.split(r"[\\/]", toks[0])[-1].lower()
    exe = re.sub(r"\.(cmd|exe|bat|ps1)$", "", exe).strip("'\"")
    if exe == "az":
        return check_az(toks[1:])
    if exe in ("terraform", "tofu", "terragrunt"):
        return check_terraform(toks)
    if exe in ("kubectl", "oc"):
        return check_kubectl(toks)
    if exe == "helm":
        return check_helm(toks)
    if exe == "azd" and len(toks) > 1 and toks[1] in ("up", "provision", "deploy", "down"):
        return f"'azd {toks[1]}' changes Azure resources"
    return None


def check(command):
    # look inside $( ... ) and backticks too
    inner = re.findall(r"\$\(([^()]*)\)", command)
    for part in [command] + inner:
        for seg in SEGMENT_SPLIT.split(part):
            reason = check_segment(seg)
            if reason:
                return reason
    return None


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0
    cmd = (event.get("tool_input") or {}).get("command") or ""
    try:
        reason = check(cmd)
    except Exception as e:  # fail closed for anything that looks like infra tooling
        if re.search(r"\b(az|terraform|tofu|kubectl|helm|azd)\b|-Az[A-Z]", cmd):
            print(f"azure-finops-review read-only guard could not verify this command ({e}); blocked.",
                  file=sys.stderr)
            return 2
        return 0
    if reason:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason":
                f"azure-finops-review is read-only: {reason}. Do not retry or work around this. "
                "Put the change in the report as a recommendation; the user runs approved changes themselves.",
        }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
