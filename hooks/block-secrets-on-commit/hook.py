#!/usr/bin/env python3
"""Claude Code PreToolUse hook (Bash).

Before Claude runs `git commit`, scan what is about to be committed for
.env files, private keys, certificates, API keys and other likely secrets.
If anything is found, deny the command and list the offending files.
Every other command passes through untouched.
"""
import json
import os
import re
import subprocess
import sys

# ---------- detect a git commit command ----------
GIT_COMMIT_RE = re.compile(
    r"\bgit((?:\s+(?:-[Cc]\s+\S+|--?[\w-]+(?:=\S+)?))*)\s+commit\b([^;&|\n]*)"
)

# ---------- sensitive file names ----------
SAFE_ENV_SUFFIXES = (".example", ".sample", ".template", ".dist", ".defaults")
SENSITIVE_NAME_RES = [
    (re.compile(r"(^|/)\.env(\.[^/]+)?$"), ".env 环境变量档"),
    (re.compile(r"(^|/)[^/]*\.env$"), ".env 环境变量档"),
    (re.compile(r"\.(pem|key|p8)$", re.I), "私钥 / 金钥档"),
    (re.compile(r"\.(p12|pfx|jks|keystore)$", re.I), "金钥库 / 凭证包"),
    (re.compile(r"\.(crt|cer|der|csr)$", re.I), "凭证 (Certificate) 档"),
    (re.compile(r"(^|/)id_(rsa|dsa|ecdsa|ed25519)$"), "SSH 私钥"),
    (re.compile(r"(^|/)(credentials|\.netrc|\.npmrc|\.pypirc)$"), "凭证设定档"),
    (re.compile(r"(^|/)[^/]*(service[-_]?account|credentials?)[^/]*\.json$", re.I), "云端凭证 JSON"),
]

# ---------- sensitive content (checked on added lines only) ----------
CONTENT_RES = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY( BLOCK)?-----"), "Private key"),
    (re.compile(r"-----BEGIN CERTIFICATE-----"), "Certificate"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS Access Key"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"), "Anthropic API key"),
    (re.compile(r"\bsk-(proj-)?[A-Za-z0-9_\-]{20,}"), "OpenAI 类 API key"),
    (re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b"), "GitHub token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}"), "GitHub token"),
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}"), "GitLab token"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), "Slack token"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "Google API key"),
    (re.compile(r"\b(sk|rk)_live_[0-9A-Za-z]{20,}"), "Stripe key"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "JWT token"),
]
GENERIC_ASSIGN_RE = re.compile(
    r"""(?ix)
    (api[_-]?key|apikey|secret[_-]?key|client[_-]?secret|access[_-]?token|
     auth[_-]?token|private[_-]?key|password|passwd|secret|token)
    ["']?\s*[:=]\s*["']?([^\s"',;]{12,})
    """
)
PLACEHOLDER_RE = re.compile(
    r"^(\$|\{\{|<|process\.env|os\.environ|os\.getenv|env\(|getenv|ENV\[|"
    r"your[_-]|xxx|changeme|placeholder|example|dummy|test|fake|\*+$)",
    re.I,
)


def git(repo, *args):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr)
    return r.stdout


def resolve_repo(cmd, match, base):
    repo = base
    # `cd somewhere && git commit ...`
    cds = re.findall(r"\bcd\s+(\"[^\"]+\"|'[^']+'|\S+)\s*(?:&&|;)", cmd[: match.start()])
    if cds:
        d = os.path.expanduser(cds[-1].strip("\"'"))
        repo = d if os.path.isabs(d) else os.path.join(repo, d)
    # `git -C path commit ...`
    c = re.search(r"-C\s+(\"[^\"]+\"|'[^']+'|\S+)", match.group(1) or "")
    if c:
        d = os.path.expanduser(c.group(1).strip("\"'"))
        repo = d if os.path.isabs(d) else os.path.join(repo, d)
    return repo


def commits_all(args):
    for tok in args.split():
        if tok == "--all" or re.fullmatch(r"-[A-Za-z]*a[A-Za-z]*", tok):
            return True
        if tok in ("-m", "-F", "--message", "--file"):
            break  # message text follows; stop looking for flags
    return False


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    if data.get("tool_name") != "Bash":
        return 0
    cmd = (data.get("tool_input") or {}).get("command", "")
    m = GIT_COMMIT_RE.search(cmd)
    if not m:
        return 0  # not a commit: don't check anything

    repo = resolve_repo(cmd, m, data.get("cwd") or os.getcwd())
    try:
        git(repo, "rev-parse", "--git-dir")
    except Exception:
        return 0

    diff_sets = [["--cached"]]
    if commits_all(m.group(2) or ""):
        diff_sets.append([])  # tracked-but-unstaged changes that -a will include

    findings = {}  # path -> set(reasons)

    def add(path, reason):
        findings.setdefault(path, set()).add(reason)

    for extra in diff_sets:
        try:
            names = git(repo, "diff", *extra, "--name-only", "--diff-filter=ACMR").splitlines()
            patch = git(repo, "diff", *extra, "-U0", "--no-color", "--diff-filter=ACMR")
        except Exception:
            continue

        for path in names:
            low = path.lower()
            if any(low.endswith(s) for s in SAFE_ENV_SUFFIXES):
                continue
            for rx, label in SENSITIVE_NAME_RES:
                if rx.search(path):
                    add(path, f"档名疑似{label}")
                    break

        current = None
        for line in patch.splitlines():
            if line.startswith("+++ "):
                current = line[6:] if line.startswith("+++ b/") else None
                continue
            if not current or not line.startswith("+"):
                continue
            text = line[1:]
            for rx, label in CONTENT_RES:
                if rx.search(text):
                    add(current, f"内容含 {label}")
            for gm in GENERIC_ASSIGN_RE.finditer(text):
                if not PLACEHOLDER_RE.search(gm.group(2)):
                    add(current, f"内容疑似写死的密钥（{gm.group(1)}=…）")

    if not findings:
        return 0

    lines = ["已挡下这次 git commit：发现疑似敏感资料，以下档案有问题："]
    for path in sorted(findings):
        lines.append(f"  - {path}：{'；'.join(sorted(findings[path]))}")
    lines.append(
        "请把这些档案移出暂存区（git restore --staged <档案>）、加进 .gitignore，"
        "或改用环境变量后再提交。若确认是误判，请由使用者自行手动提交。"
    )
    reason = "\n".join(lines)
    print(json.dumps({
        "systemMessage": reason,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
