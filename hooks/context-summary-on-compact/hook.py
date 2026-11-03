#!/usr/bin/env python3
"""Claude Code hook: keep a fixed context summary across compactions.

  hook.py save     (PreCompact)  读取对话纪录与上一版摘要，请 Claude 只整理「已明确确认」
                                 的目标、决策、进度、待办、禁止事项，覆盖写回同一份摘要。
  hook.py restore  (SessionStart, matcher=compact)  压缩完成后把摘要加回工作背景。

摘要位置：~/.claude/context-summaries/<session_id>.md（同一个 session 只有一份）。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

SUMMARY_DIR = os.path.expanduser("~/.claude/context-summaries")
CHILD_ENV = "MY_CLAUDE_HOOKS_SUMMARIZER"
MAX_CHARS = 150_000        # 送去整理的对话最多保留多少字（取最近的部分）
MAX_MSG_CHARS = 4_000      # 单则讯息最多保留多少字
KEEP_DAYS = 30             # 旧摘要保留天数
MODEL = os.environ.get("MY_CLAUDE_HOOKS_SUMMARY_MODEL", "sonnet")

SYSTEM_PROMPT = """你是一个严谨的会议纪录员。你会收到一段 Claude Code 工作对话（使用者与 Claude 之间），以及上一版的上下文摘要（可能没有）。
请输出更新后的上下文摘要，供对话压缩后继续工作使用。

严格规则：
1. 只记录「已经明确确认」的内容：使用者明确说出的要求、使用者明确同意或做出的决定、已经实际完成并确认的结果。
2. Claude 自己的提议、推测、计划，如果使用者没有明确同意，一律不要记录。
3. 不要推测、不要补充、不要美化，不要加入对话中没有的资讯。宁可少写，不可写错。
4. 以上一版摘要为基础更新：仍然有效的保留；被后来的对话推翻或修改的，改成最新的；已完成的待办移到进度。
5. 每一项简短具体，必要时写出档案路径、指令或名称。某一栏没有内容就写「（无）」。
6. 只输出下面的 Markdown，不要有任何前言、说明或结尾。

## 目标
## 重要决策
## 目前进度
## 尚未完成的事项
## 明确禁止的做法"""


def find_claude():
    found = shutil.which("claude")
    if found:
        return found
    for p in ("~/.local/bin/claude", "~/.claude/local/claude", "/opt/homebrew/bin/claude",
              "/usr/local/bin/claude", "~/.npm-global/bin/claude"):
        p = os.path.expanduser(p)
        if os.access(p, os.X_OK):
            return p
    return "claude"


def summary_path(session_id):
    safe = re.sub(r"[^\w.-]", "_", session_id or "unknown")
    return os.path.join(SUMMARY_DIR, f"{safe}.md")


def strip_noise(text):
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.S)
    return text.strip()


def clip(text, n=MAX_MSG_CHARS):
    return text if len(text) <= n else text[:n] + " …（以下省略）"


def read_transcript(path):
    """把 JSONL 对话纪录转成「使用者：… / Claude：…」的纯文字，略过工具输出。"""
    parts = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            kind = e.get("type")
            if kind not in ("user", "assistant") or e.get("isMeta"):
                continue
            content = (e.get("message") or {}).get("content")
            if isinstance(content, str):
                texts = [content]
            elif isinstance(content, list):
                texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            else:
                texts = []
            text = strip_noise("\n".join(texts))
            if not text:
                continue
            if e.get("isCompactSummary"):
                label = "【先前压缩产生的摘要】"
            else:
                label = "使用者：" if kind == "user" else "Claude："
            parts.append(label + clip(text))
    convo = "\n\n".join(parts)
    if len(convo) > MAX_CHARS:
        convo = "…（较早的对话省略）\n\n" + convo[-MAX_CHARS:]
    return convo


def prune_old():
    cutoff = time.time() - KEEP_DAYS * 86400
    try:
        for name in os.listdir(SUMMARY_DIR):
            p = os.path.join(SUMMARY_DIR, name)
            if name.endswith(".md") and os.path.getmtime(p) < cutoff:
                os.remove(p)
    except OSError:
        pass


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False))


def save(data):
    session_id = data.get("session_id", "")
    transcript = data.get("transcript_path", "")
    path = summary_path(session_id)
    if not transcript or not os.path.isfile(transcript):
        emit({"systemMessage": "⚠️ 找不到对话纪录，这次没有更新上下文摘要。"})
        return 0

    convo = read_transcript(transcript)
    previous = ""
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            previous = f.read().strip()

    user_input = (
        "# 上一版上下文摘要\n\n" + (previous or "（没有，这是第一次整理）")
        + "\n\n# 本次工作对话\n\n" + (convo or "（没有可用的对话内容）")
        + "\n\n# 指示\n\n请依规则输出更新后的上下文摘要。"
    )

    env = dict(os.environ, **{CHILD_ENV: "1"})
    try:
        r = subprocess.run(
            [find_claude(), "-p", "--model", MODEL, "--tools", "", "--no-session-persistence",
             "--system-prompt", SYSTEM_PROMPT],
            input=user_input, capture_output=True, text=True, timeout=220,
            env=env, cwd=tempfile.gettempdir(),
        )
        result = r.stdout.strip()
        ok = r.returncode == 0 and "## 目标" in result
    except Exception as exc:
        ok, result, r = False, "", None
        err = str(exc)
    else:
        err = (r.stderr or r.stdout or "").strip()[:300]

    if not ok:
        msg = "⚠️ 这次没能更新上下文摘要（" + (err or "整理失败") + "）。"
        msg += "已保留上一版摘要。" if previous else ""
        emit({"systemMessage": msg})
        return 0

    result = result[result.index("## 目标"):]
    os.makedirs(SUMMARY_DIR, exist_ok=True)
    header = (
        f"<!-- 上下文摘要 · session {session_id} · 更新于 {time.strftime('%Y-%m-%d %H:%M')} -->\n"
        "# 上下文摘要（只含已明确确认的内容）\n\n"
    )
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(header + result + "\n")
    os.replace(tmp, path)  # 原子性覆盖：永远只有这一份
    prune_old()
    emit({"systemMessage": f"📝 已更新上下文摘要：{path}"})
    return 0


def restore(data):
    path = summary_path(data.get("session_id", ""))
    if not os.path.isfile(path):
        return 0
    with open(path, encoding="utf-8") as f:
        summary = f.read().strip()
    if not summary:
        return 0
    context = (
        "以下是这次工作在对话压缩前整理的上下文摘要，只包含使用者已明确确认的内容。"
        "请以此为准继续工作；「明确禁止的做法」必须遵守。如果与压缩后的摘要有冲突，以这份为准，"
        f"不确定时先问使用者。（档案：{path}）\n\n" + summary
    )
    emit({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}})
    return 0


def main():
    if os.environ.get(CHILD_ENV):
        return 0  # 由本 hook 启动的整理程序，不要再触发自己
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    if mode == "save":
        return save(data)
    if mode == "restore":
        return restore(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
