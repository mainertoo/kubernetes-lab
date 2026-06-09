#!/usr/bin/env python3
"""Configure / drive Wiki.js native Git Storage to the PRIVATE mainertoo/wiki repo.

Wiki.js mirrors its entire content tree (Markdown + frontmatter) bidirectionally
to mainertoo/wiki via its built-in Git Storage module. This script configures
that target and triggers its sync actions via the Wiki.js GraphQL API — useful
for initial setup and for DISASTER RECOVERY (re-applying the config after a wiki
rebuild). See docs/wiki-git-storage.md for the full runbook.

No secrets are hardcoded:
  - WIKI_API_KEY  : read from env, else the live k8s secret wiki-js-secret.
  - WIKI_GIT_SSH_KEY (the repo's ed25519 deploy key): read from the SOPS file
    apps/base/wiki-js/wiki-js-secret.sops.yaml (decrypted on the fly).

Usage:
  configure-git-storage.py config <sync|push|pull>   # set mode + full config
  configure-git-storage.py action <handler>          # syncUntracked|sync|importAll|purge
  configure-git-storage.py status

Typical first-time seed:  config sync  ->  action syncUntracked  ->  action sync
"""
import base64, json, os, subprocess, sys, urllib.request

API = os.environ.get("WIKI_API_URL", "https://wiki.lab.mainertoo.com/graphql")
REPO_URL = "git@github.com:mainertoo/wiki.git"
BRANCH = "main"
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SOPS_FILE = os.path.join(REPO_ROOT, "apps/base/wiki-js/wiki-js-secret.sops.yaml")


def k8s_secret(key):
    out = subprocess.check_output(
        ["kubectl", "get", "secret", "wiki-js-secret", "-n", "wiki-js",
         "-o", f"jsonpath={{.data.{key}}}"])
    return base64.b64decode(out).decode()


def sops_secret(key):
    import yaml
    out = subprocess.check_output(["sops", "decrypt", SOPS_FILE])
    return yaml.safe_load(out)["stringData"][key]


TOKEN = os.environ.get("WIKI_API_KEY") or k8s_secret("WIKI_API_KEY").strip()


def gql(query, variables=None):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(API, data=body, headers={
        "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        d = json.load(r)
    if d.get("errors"):
        print("GraphQL errors:", json.dumps(d["errors"], indent=2)); sys.exit(1)
    return d["data"]


def _cfg(**kv):
    # Wiki.js expects each config value JSON-encoded as {"v": <actual>}.
    return [{"key": k, "value": json.dumps({"v": v})} for k, v in kv.items()]


def config(mode):
    target = {
        "key": "git", "isEnabled": True, "mode": mode, "syncInterval": "PT5M",
        "config": _cfg(
            authType="ssh", repoUrl=REPO_URL, branch=BRANCH,
            sshPrivateKeyMode="contents",
            sshPrivateKeyContent=sops_secret("WIKI_GIT_SSH_KEY"),
            sshPrivateKeyPath="", basicUsername="", basicPassword="",
            defaultEmail="mainertoo@gmail.com", defaultName="Wiki.js",
            localRepoPath="./data/repo", alwaysNamespace=False,
            gitBinaryPath="", verifySSL=True),
    }
    m = ("mutation($t:[StorageTargetInput]!){storage{updateTargets(targets:$t)"
         "{responseResult{succeeded message}}}}")
    r = gql(m, {"t": [target]})["storage"]["updateTargets"]["responseResult"]
    print(f"config(mode={mode}): succeeded={r['succeeded']} msg={r['message']}")


def action(handler):
    m = ('mutation($k:String!,$h:String!){storage{executeAction(targetKey:$k,'
         'handler:$h){responseResult{succeeded message}}}}')
    r = gql(m, {"k": "git", "h": handler})["storage"]["executeAction"]["responseResult"]
    print(f"action({handler}): succeeded={r['succeeded']} msg={r['message']}")


def status():
    for s in gql("{storage{status{key title status message lastAttempt}}}")["storage"]["status"]:
        if s["key"] == "git":
            print("git status:", json.dumps(s, indent=2))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "config": config(sys.argv[2])
    elif cmd == "action": action(sys.argv[2])
    else: status()
