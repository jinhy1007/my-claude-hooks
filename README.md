# my-claude-hooks

我的 Claude Code hook 集合。**只改这个 repo，本机和云端都会同步。**

## 目前有的 hook

| 名称 | 作用 |
|---|---|
| `block-secrets-on-commit` | Claude 要 `git commit` 前，检查这次提交的内容。发现 `.env`、私钥、凭证、API key 等敏感资料就挡下，并列出有问题的档案。其他指令不检查。 |
| `confirm-destructive-commands` | Claude 要执行可能删除档案、丢弃未保存修改、覆盖既有内容等难以复原的终端机指令时，先跳出确认，说明会影响哪些档案。查询类指令直接放行。 |

## 安装

### 本机（第一次）

```bash
git clone https://github.com/jinhy1007/my-claude-hooks ~/my-claude-hooks
```

```bash
bash ~/my-claude-hooks/install.sh
```

装完重启 Claude Code，或在 Claude Code 里打开一次 `/hooks`。

### 本机（之后更新）

```bash
cd ~/my-claude-hooks && git pull && bash install.sh
```

### 云端环境

在云端环境设定（session 标题列的环境选单 → Edit）的 **Setup script** 放这一行，之后不用再改：

```bash
git clone --depth 1 https://github.com/jinhy1007/my-claude-hooks /tmp/my-claude-hooks && bash /tmp/my-claude-hooks/install.sh
```

每个新的云端 session 都会自动抓最新版安装。

## 移除

```bash
bash ~/my-claude-hooks/uninstall.sh
```

只会移除这个 repo 装的 hook，你的其他设定不受影响。

## 新增一个 hook

在 `hooks/` 底下建一个资料夹，放两个档案：

```
hooks/
  你的-hook-名称/
    hook.json   ← 设定：什么时候触发
    hook.py     ← 实际要做的事（也可以是 .sh）
```

`hook.json` 范例：

```json
{
  "description": "这个 hook 做什么（安装时会显示）",
  "event": "PreToolUse",
  "matcher": "Bash",
  "script": "hook.py",
  "timeout": 30,
  "statusMessage": "执行中显示的文字"
}
```

- `event`：什么时机触发，例如 `PreToolUse`（工具执行前，可以挡下）、`PostToolUse`（工具执行后）、`Stop`（Claude 回答完）、`SessionStart`（session 开始）。
- `matcher`：针对哪个工具，例如 `Bash`、`Write|Edit`。不需要就省略。

push 之后，本机跑一次更新指令即可；云端的新 session 会自动套用。

> ⚠️ 这个 repo 是公开的，**不要放任何密码或 API key**。
