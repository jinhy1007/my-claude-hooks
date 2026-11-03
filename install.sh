#!/usr/bin/env bash
# 把 hooks/ 里的所有 hook 安装到 ~/.claude，对这台机器上所有 Claude Code session 生效。
# 可以重复执行：每次都会以 repo 的内容为准（新增、更新，repo 里删掉的 hook 也会一并移除）。
set -euo pipefail

command -v python3 >/dev/null || { echo "❌ 需要 python3，请先安装"; exit 1; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$HOME/.claude/my-claude-hooks"

# 1. 复制 hook 脚本到固定位置（之后就算删掉 repo 资料夹，hook 也照常运作）
mkdir -p "$HOME/.claude"
rm -rf "$TARGET"
mkdir -p "$TARGET"
cp -R "$REPO_DIR/hooks/." "$TARGET/"

# 2. 把每个 hook 合并进 ~/.claude/settings.json（保留你原有的其他设定，先备份）
python3 - "$TARGET" <<'PY'
import json, os, shutil, sys, time

target = sys.argv[1]
settings_path = os.path.expanduser("~/.claude/settings.json")
MARK = "/.claude/my-claude-hooks/"          # 由本 repo 管理的 hook，指令里都含这个路径
LEGACY = ["/.claude/hooks/block-secrets-on-commit.py"]  # 旧版单独安装的，顺便清掉

settings = {}
if os.path.exists(settings_path):
    shutil.copy(settings_path, settings_path + ".bak-" + time.strftime("%Y%m%d%H%M%S"))
    with open(settings_path) as f:
        text = f.read().strip()
    settings = json.loads(text) if text else {}

hooks_cfg = settings.setdefault("hooks", {})

# 先移除所有由本 repo 管理（以及旧版）的 hook，再依 repo 现况重新加入
def managed(h):
    cmd = h.get("command", "")
    return MARK in cmd or any(l in cmd for l in LEGACY)

for event in list(hooks_cfg):
    groups = []
    for g in hooks_cfg[event]:
        g["hooks"] = [h for h in g.get("hooks", []) if not managed(h)]
        if g["hooks"]:
            groups.append(g)
    if groups:
        hooks_cfg[event] = groups
    else:
        del hooks_cfg[event]

installed = []
for name in sorted(os.listdir(target)):
    meta_path = os.path.join(target, name, "hook.json")
    if not os.path.isfile(meta_path):
        continue
    with open(meta_path) as f:
        meta = json.load(f)
    # hook.json 可以只设一个触发时机，也可以用 "triggers" 列出多个
    for trig in meta.get("triggers", [meta]):
        script = trig.get("script", meta.get("script"))
        path = f'"$HOME/.claude/my-claude-hooks/{name}/{script}"'
        if script.endswith(".py"):
            cmd = f"python3 {path}"
        elif script.endswith(".sh"):
            cmd = f"bash {path}"
        else:
            os.chmod(os.path.join(target, name, script), 0o755)
            cmd = path
        if trig.get("args"):
            cmd += " " + " ".join(trig["args"])
        entry = {"type": "command", "command": cmd}
        for key in ("timeout", "statusMessage"):
            if key in trig:
                entry[key] = trig[key]
        group = {"hooks": [entry]}
        if trig.get("matcher"):
            group["matcher"] = trig["matcher"]
        hooks_cfg.setdefault(trig["event"], []).append(group)
    installed.append(f"  ✅ {name}：{meta.get('description', '')}")

if not hooks_cfg:
    del settings["hooks"]

os.makedirs(os.path.dirname(settings_path), exist_ok=True)
with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2, ensure_ascii=False)
    f.write("\n")

print(f"已安装 {len(installed)} 个 hook 到 {settings_path}：")
print("\n".join(installed) if installed else "  （没有 hook）")
PY

echo "完成。若 Claude Code 正在执行，请重启它，或在 Claude Code 里打开一次 /hooks 让设定生效。"
