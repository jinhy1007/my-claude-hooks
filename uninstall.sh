#!/usr/bin/env bash
# 移除由本 repo 安装的所有 hook（你自己的其他设定不受影响）。
set -euo pipefail

python3 - <<'PY'
import json, os, shutil, time
p = os.path.expanduser("~/.claude/settings.json")
if not os.path.exists(p):
    print("没有 ~/.claude/settings.json，不需要移除"); raise SystemExit
shutil.copy(p, p + ".bak-" + time.strftime("%Y%m%d%H%M%S"))
with open(p) as f:
    s = json.load(f)
h = s.get("hooks", {})
for ev in list(h):
    groups = []
    for g in h[ev]:
        g["hooks"] = [x for x in g.get("hooks", []) if "/.claude/my-claude-hooks/" not in x.get("command", "")]
        if g["hooks"]:
            groups.append(g)
    if groups: h[ev] = groups
    else: del h[ev]
if "hooks" in s and not s["hooks"]:
    del s["hooks"]
with open(p, "w") as f:
    json.dump(s, f, indent=2, ensure_ascii=False); f.write("\n")
print("已从 settings.json 移除所有 my-claude-hooks 的 hook")
PY
rm -rf "$HOME/.claude/my-claude-hooks"
echo "完成。请重启 Claude Code 或打开一次 /hooks。"
