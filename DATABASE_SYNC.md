# Zoom Chat Knowledge Hub 数据库交接脚本

这些脚本安装在电脑 A、B 各自的 `zoom-chat-kb-hub` 项目根目录，通过 Google Drive 的 `shared-state` 目录传递经过完整性检查的 SQLite 快照。实际运行的数据库仍保存在每台电脑的本地项目目录中，不会直接在 Google Drive 上运行 SQLite，也不会复制 `zoom-token.bin`、`.venv`、日志或 API 配置。

## 使用原则

任何时候只能有一台电脑取得控制权并运行服务。Google Drive 的同步存在延迟，租约文件只能用于防止日常误操作，不能代替真正的分布式锁。切换电脑前，务必等待 Google Drive 显示 **Up to date（同步完成）**。

## 日常切换

在当前使用数据库的电脑上释放控制权：

```powershell
.\release.cmd
```

等待 Google Drive 显示 **Up to date（同步完成）**，然后在另一台电脑上取得控制权：

```powershell
.\take.cmd
```

随时可以查看状态：

```powershell
.\sync-status.cmd
```

脚本默认把所在目录作为项目目录。Google Drive 交换目录默认为 `G:\My Drive\Claude\Project\zoom-chat-kb-hub\shared-state`。如果电脑 A 的 Google Drive 盘符不同，可通过 PowerShell 参数指定：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\take-control.ps1" -SharedDir "H:\My Drive\Claude\Project\zoom-chat-kb-hub\shared-state"
```

## 第一次使用

在数据库内容最新的电脑上直接运行 `.\release.cmd`，它会停止本机服务并发布第 1 代快照。等待 Google Drive 同步完成后，再在另一台电脑运行 `.\take.cmd`。

## 恢复与备份

- 如果持有控制权的电脑异常关机，先确认该电脑的服务已停止，等待 Google Drive 同步完成，再运行 `take-control.ps1 -Force`。
- 每次发布的历史版本都保留在 `shared-state\snapshots`。
- 共享快照替换本地数据库前，旧数据库会移到项目的 `data\sync-local-backups`。
- 如果出现哈希、文件大小或数据库完整性错误，应等待 Google Drive 完成同步后重试。`-Force` 不会绕过这些检查。
