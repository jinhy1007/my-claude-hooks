#!/usr/bin/env python3
"""Claude Code PreToolUse hook (Bash).

Before Claude runs a terminal command that could delete files, discard
unsaved changes, overwrite existing content, or otherwise be hard to undo,
pause and ask the user to confirm, explaining what will be affected.
Read-only commands pass through silently.
"""
import json
import os
import re
import shlex
import subprocess
import sys

MAX_LIST = 8  # 最多列出几个受影响的项目


# ---------------------------------------------------------------- helpers
def short_list(items):
    items = list(items)
    shown = "、".join(items[:MAX_LIST])
    if len(items) > MAX_LIST:
        shown += f" …等共 {len(items)} 项"
    return shown


def resolve(cwd, p):
    p = os.path.expanduser(p)
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(cwd, p))


def describe_path(cwd, p):
    full = resolve(cwd, p)
    if any(ch in p for ch in "*?["):
        return f"{p}（万用字元，可能符合多个档案）"
    if os.path.isdir(full) and not os.path.islink(full):
        n = 0
        for _root, _dirs, files in os.walk(full):
            n += len(files)
            if n > 9999:
                break
        return f"{p}/（资料夹，内含 {n if n <= 9999 else '上万'} 个档案）"
    if os.path.exists(full):
        return p
    return None  # 不存在，不算影响


def git(cwd, *args):
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=10)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def dirty_files(cwd, paths=None):
    out = git(cwd, "status", "--porcelain", "--untracked-files=no", "--", *(paths or []))
    return [line[3:] for line in out.splitlines() if line.strip()]


def strip_heredoc_bodies(cmd):
    # 移除 heredoc 的内文（那是写进档案的内容，不是要执行的指令）
    return re.sub(
        r"<<-?\s*(['\"]?)(\w+)\1([^\n]*)\n.*?\n\s*\2\s*(?=\n|$)",
        r"<<HEREDOC\3",
        cmd,
        flags=re.S,
    )


def tokenize(cmd):
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


SEPARATORS = {";", "&&", "||", "|", "&", "\n", "(", ")", ";;"}
PREFIXES = {"sudo", "env", "nohup", "time", "command", "builtin", "exec", "nice", "stdbuf", "doas"}


def split_segments(tokens):
    seg = []
    for t in tokens:
        if t in SEPARATORS or (set(t) <= set(";&|()") and t):
            if seg:
                yield seg
            seg = []
        else:
            seg.append(t)
    if seg:
        yield seg


# ---------------------------------------------------------------- analysis
class Analyzer:
    def __init__(self, cwd):
        self.cwd = cwd
        self.issues = []  # (类型, 说明)

    def add(self, kind, detail):
        self.issues.append((kind, detail))

    # -------- entry
    def analyze(self, cmd, depth=0):
        if depth > 3:
            return
        cmd = strip_heredoc_bodies(cmd)
        try:
            tokens = tokenize(cmd)
        except ValueError:
            self.coarse(cmd)
            return
        for seg in split_segments(tokens):
            self.segment(seg, depth)

    def coarse(self, cmd):
        # 无法解析时的保守判断
        if re.search(r"\b(rm|rmdir|shred|unlink|truncate|mkfs|dd)\b|git\s+(reset\s+--hard|clean|push\s+.*(-f|--force))", cmd):
            self.add("可能难以复原", "指令格式复杂，无法精确分析，但含有删除或覆盖类的操作")

    # -------- one simple command
    def segment(self, seg, depth):
        seg = self.redirections(seg)
        # 去掉 sudo / env VAR=1 等前缀
        while seg and (seg[0] in PREFIXES or re.fullmatch(r"\w+=.*", seg[0]) or (seg[0].startswith("-") and len(seg) > 1 and seg[0] != "-")):
            seg = seg[1:]
        if not seg:
            return
        name = os.path.basename(seg[0])
        args = seg[1:]

        if name == "cd" and args:
            self.cwd = resolve(self.cwd, args[0])
            return
        if name in ("bash", "sh", "zsh", "dash") and "-c" in args:
            i = args.index("-c")
            if i + 1 < len(args):
                self.analyze(args[i + 1], depth + 1)
            return
        if name == "eval":
            self.analyze(" ".join(args), depth + 1)
            return
        if name == "xargs":
            rest = [a for a in args if not a.startswith("-")]
            if rest:
                self.segment(rest + ["<由 xargs 传入的档案>"], depth)
            return

        handler = getattr(self, "cmd_" + name.replace("-", "_"), None)
        if handler:
            handler(args)

    # -------- > file 覆盖
    def redirections(self, seg):
        out, i = [], 0
        while i < len(seg):
            t = seg[i]
            if t in (">", ">|", "&>") and i + 1 < len(seg):
                target = seg[i + 1]
                if not target.startswith("/dev/") and target != "HEREDOC":
                    full = resolve(self.cwd, target)
                    if os.path.isfile(full) and os.path.getsize(full) > 0:
                        self.add("覆盖既有内容", f"{target} 会被整个清空、改写（目前 {os.path.getsize(full)} bytes）")
                i += 2
                continue
            if t in (">>", ">&", "<", "<<", "<<<", "<&") and i + 1 < len(seg):
                i += 2
                continue
            out.append(t)
            i += 1
        return out

    # -------- 删除
    def _delete(self, args, label="删除档案"):
        targets = [a for a in args if not a.startswith("-") or a == "-"]
        described = [d for d in (describe_path(self.cwd, t) for t in targets) if d]
        if any(t.startswith("<") for t in targets):
            described.append("由前一个指令产生的档案清单")
        if described:
            self.add(label, short_list(described) + "（删除后无法从垃圾桶救回）")

    def cmd_rm(self, args):
        self._delete(args)

    cmd_rmdir = cmd_unlink = cmd_srm = cmd_rm

    def cmd_shred(self, args):
        self._delete(args, "彻底抹除档案")

    def cmd_find(self, args):
        if "-delete" in args:
            self.add("删除档案", f"find {' '.join(a for a in args if a != '-delete')} 找到的所有档案")
        for flag in ("-exec", "-execdir", "-ok"):
            if flag in args:
                sub = args[args.index(flag) + 1:]
                if sub and os.path.basename(sub[0]) in ("rm", "shred", "unlink", "mv", "truncate"):
                    self.add("删除或修改档案", f"find 找到的每个档案都会执行 {sub[0]}")

    def cmd_truncate(self, args):
        files = [a for a in args if not a.startswith("-") and not re.fullmatch(r"[+-]?\d+[KMGkmg]?", a)]
        if files:
            self.add("覆盖既有内容", f"截断档案 {short_list(files)}")

    def cmd_dd(self, args):
        for a in args:
            if a.startswith("of="):
                self.add("覆盖既有内容", f"dd 写入 {a[3:]}（会直接覆盖目标）")

    def cmd_mkfs(self, args):
        self.add("格式化磁碟", f"mkfs {' '.join(args)}（磁碟上全部资料会消失）")

    def cmd_crontab(self, args):
        if "-r" in args:
            self.add("删除设定", "删除你全部的 crontab 排程")

    # -------- 覆盖：mv / cp / tee / sed -i
    def _dest_overwrite(self, args, verb):
        if any(a in ("-n", "--no-clobber", "-i", "--interactive") for a in args):
            return
        files = [a for a in args if not a.startswith("-")]
        if len(files) < 2:
            return
        dest = resolve(self.cwd, files[-1])
        if os.path.isdir(dest):
            hits = [s for s in files[:-1] if os.path.exists(os.path.join(dest, os.path.basename(s.rstrip("/"))))]
            if hits:
                self.add("覆盖既有内容", f"{verb}会覆盖 {files[-1]} 里已存在的 {short_list(os.path.basename(h) for h in hits)}")
        elif os.path.exists(dest):
            self.add("覆盖既有内容", f"{verb}会覆盖已存在的 {files[-1]}")

    def cmd_mv(self, args):
        self._dest_overwrite(args, "移动")

    def cmd_cp(self, args):
        self._dest_overwrite(args, "复制")


    def cmd_tee(self, args):
        if "-a" in args or "--append" in args:
            return
        hits = [a for a in args if not a.startswith("-") and os.path.isfile(resolve(self.cwd, a))]
        if hits:
            self.add("覆盖既有内容", f"tee 会改写 {short_list(hits)}")

    def cmd_sed(self, args):
        if any(a == "-i" or a.startswith("-i") or a.startswith("--in-place") for a in args):
            files = [a for a in args[1:] if not a.startswith("-") and os.path.isfile(resolve(self.cwd, a))]
            self.add("直接修改档案", f"sed -i 会直接改写 {short_list(files) if files else '指定的档案'}（没有备份）")

    def cmd_perl(self, args):
        if any(re.fullmatch(r"-\w*i\S*", a) for a in args):
            self.add("直接修改档案", "perl -i 会直接改写指定的档案")

    def cmd_chmod(self, args):
        if "-R" in args or "--recursive" in args:
            self.add("大量修改权限", f"chmod {' '.join(args)}（整个资料夹的权限都会改变）")

    def cmd_chown(self, args):
        if "-R" in args or "--recursive" in args:
            self.add("大量修改拥有者", f"chown {' '.join(args)}")

    def cmd_python(self, args):
        code = " ".join(args)
        if "-c" in args and re.search(r"os\.(remove|unlink|rmdir)|shutil\.rmtree|\.unlink\(|\.rmdir\(", code):
            self.add("删除档案", "这段 Python 程式会删除档案或资料夹")

    cmd_python3 = cmd_python

    def cmd_node(self, args):
        code = " ".join(args)
        if re.search(r"fs\.(rm|rmSync|unlink|unlinkSync|rmdir|rmdirSync)\b", code):
            self.add("删除档案", "这段 Node 程式会删除档案或资料夹")

    # -------- 程序 / 服务 / 外部资源
    def cmd_kill(self, args):
        self.add("结束程序", f"结束程序 {' '.join(args)}（未保存的工作会遗失）")

    cmd_pkill = cmd_killall = cmd_kill

    def cmd_docker(self, args):
        a = " ".join(args)
        if re.search(r"\b(rm|rmi|prune|down)\b|volume\s+rm|system\s+prune", a):
            self.add("删除 Docker 资源", f"docker {a}")

    def cmd_kubectl(self, args):
        if args and args[0] in ("delete", "drain", "replace"):
            self.add("修改 Kubernetes 资源", f"kubectl {' '.join(args)}")

    def cmd_terraform(self, args):
        if args and args[0] in ("destroy", "apply"):
            self.add("修改云端基础设施", f"terraform {args[0]}")

    def cmd_gh(self, args):
        a = " ".join(args)
        if re.search(r"\b(delete|archive)\b|pr\s+close|issue\s+close|pr\s+merge", a):
            self.add("修改 GitHub 上的内容", f"gh {a}")

    def cmd_aws(self, args):
        a = " ".join(args)
        if re.search(r"\b(rm|rb|delete-\S+|terminate-instances)\b", a):
            self.add("删除云端资源", f"aws {a}")

    def cmd_gsutil(self, args):
        if args and args[0] in ("rm", "rb"):
            self.add("删除云端资源", f"gsutil {' '.join(args)}")

    def cmd_rsync(self, args):
        if any(a.startswith("--delete") for a in args):
            files = [a for a in args if not a.startswith("-")]
            self.add("删除档案", f"rsync --delete 会删除 {files[-1] if files else '目标'} 里来源没有的档案")

    def _sql(self, args, tool):
        a = " ".join(args)
        if re.search(r"\b(DROP|TRUNCATE)\b|\bDELETE\s+FROM\b|\bUPDATE\s+\w+\s+SET\b|\bALTER\s+TABLE\b", a, re.I):
            self.add("修改资料库", f"{tool} 会执行会删除或修改资料的 SQL")

    def cmd_psql(self, args):
        self._sql(args, "psql")

    def cmd_mysql(self, args):
        self._sql(args, "mysql")

    def cmd_sqlite3(self, args):
        self._sql(args, "sqlite3")

    def cmd_redis_cli(self, args):
        if any(a.upper() in ("FLUSHALL", "FLUSHDB", "DEL") for a in args):
            self.add("删除资料", f"redis-cli {' '.join(args)}")

    # -------- git
    def cmd_git(self, args):
        # 处理 git -C path
        while args and args[0].startswith("-"):
            if args[0] in ("-C", "-c") and len(args) > 1:
                if args[0] == "-C":
                    self.cwd = resolve(self.cwd, args[1])
                args = args[2:]
            else:
                args = args[1:]
        if not args:
            return
        sub, rest = args[0], args[1:]
        flags = set(rest)

        if sub == "reset" and "--hard" in flags:
            files = dirty_files(self.cwd)
            detail = "丢弃所有尚未 commit 的修改"
            if files:
                detail += f"，受影响：{short_list(files)}"
            target = [r for r in rest if not r.startswith("-")]
            if target:
                detail += f"；并把分支退回到 {target[0]}（之后的 commit 会从分支上消失）"
            self.add("丢弃未保存的修改", detail)

        elif sub == "checkout" and ("--" in rest or "." in rest or "-f" in flags or "--force" in flags):
            paths = rest[rest.index("--") + 1:] if "--" in rest else [r for r in rest if r == "."]
            files = dirty_files(self.cwd, paths) if paths else dirty_files(self.cwd)
            if files:
                self.add("丢弃未保存的修改", f"还原这些档案、丢弃修改：{short_list(files)}")

        elif sub == "restore" and not ({"--staged", "-S"} & flags and not {"--worktree", "-W"} & flags):
            paths = [r for r in rest if not r.startswith("-")]
            files = dirty_files(self.cwd, paths)
            if files:
                self.add("丢弃未保存的修改", f"还原这些档案、丢弃修改：{short_list(files)}")

        elif sub == "switch" and ({"--discard-changes", "-f", "--force"} & flags):
            files = dirty_files(self.cwd)
            if files:
                self.add("丢弃未保存的修改", f"切换分支并丢弃修改：{short_list(files)}")

        elif sub == "clean" and any(re.fullmatch(r"-\w*f\w*", r) or r == "--force" for r in rest):
            dry = [r for r in rest if r not in ("-f", "--force")]
            dry = [re.sub(r"f", "", r) if re.fullmatch(r"-\w*f\w*", r) else r for r in dry]
            dry = [r for r in dry if r not in ("-", "")]
            try:
                r = subprocess.run(["git", "-C", self.cwd, "clean", "-n", *dry], capture_output=True, text=True, timeout=10)
                ok, out = r.returncode == 0, r.stdout
            except Exception:
                ok, out = False, ""
            files = [l.replace("Would remove ", "") for l in out.splitlines() if l.startswith("Would remove")]
            if files:
                self.add("删除档案", f"删除未被 git 追踪的档案：{short_list(files)}")
            elif not ok:
                self.add("删除档案", f"git clean {' '.join(rest)}（无法预先列出会删除哪些档案）")

        elif sub == "stash" and rest and rest[0] in ("drop", "clear"):
            self.add("删除暂存的修改", "git stash " + " ".join(rest) + "（stash 里的修改会消失）")

        elif sub == "branch" and ({"-D", "--delete", "-d"} & flags or any(re.fullmatch(r"-\w*D\w*", r) for r in rest)):
            names = [r for r in rest if not r.startswith("-")]
            self.add("删除分支", f"删除分支 {short_list(names)}")

        elif sub == "push":
            if {"-f", "--force", "--force-with-lease", "--force-if-includes"} & flags or any(r.startswith("--force") or (r.startswith("+") and len(r) > 1) for r in rest):
                self.add("覆盖远端历史", f"强制推送：会用本地版本覆盖远端 {' '.join(r for r in rest if not r.startswith('-')) or '目前分支'} 的历史，别人的 commit 可能遗失")
            if "--delete" in flags or "-d" in flags or any(r.startswith(":") and len(r) > 1 for r in rest):
                self.add("删除远端分支", f"git push {' '.join(rest)}")

        elif sub == "tag" and ({"-d", "--delete"} & flags):
            self.add("删除标签", f"git tag {' '.join(rest)}")

        elif sub in ("filter-branch", "filter-repo"):
            self.add("改写整个历史", f"git {sub} 会改写 repo 的全部历史")

        elif sub == "rebase" and not ({"--abort", "--continue", "--skip", "--quit"} & flags):
            self.add("改写历史", f"git rebase {' '.join(rest)}（会改写 commit 历史）")

        elif sub == "reflog" and rest and rest[0] in ("expire", "delete"):
            self.add("删除复原纪录", "清除 reflog，之后就无法找回遗失的 commit")

        elif sub == "gc" and any(r.startswith("--prune") for r in rest):
            self.add("永久清除", "git gc --prune 会永久删除无法存取的 commit")

        elif sub == "worktree" and rest and rest[0] == "remove":
            self.add("删除工作目录", f"git worktree {' '.join(rest)}")

        elif sub == "update-ref" and "-d" in flags:
            self.add("删除参照", f"git update-ref {' '.join(rest)}")


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    if data.get("tool_name") != "Bash":
        return 0
    cmd = (data.get("tool_input") or {}).get("command", "")
    if not cmd.strip():
        return 0

    an = Analyzer(data.get("cwd") or os.getcwd())
    try:
        an.analyze(cmd)
    except Exception:
        an.coarse(cmd)

    if not an.issues:
        return 0  # 查询或一般指令：不打扰

    seen, lines = set(), []
    for kind, detail in an.issues:
        if (kind, detail) not in seen:
            seen.add((kind, detail))
            lines.append(f"• 【{kind}】{detail}")
    reason = "⚠️ 这个指令可能难以复原，请确认后再继续：\n" + "\n".join(lines)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
