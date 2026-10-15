# Magpie SSH Monitor

在 Mac 的 [Magpie](https://github.com/yetone/magpie) 中查看 SSH 服务器上的 Codex 额度快照和会话用量。

支持自己添加 SSH 别名。每台服务器有独立额度卡片、`SSH-别名` 用量筛选、采集进度和后台任务，服务器上不安装常驻服务。

## 安装

需要 macOS、Python 3.9+、已运行的 Magpie，以及能免交互连接的 SSH 别名。服务器也需要 Python 3.9+ 和包含明确 `token_usage_record` 的 Codex JSONL 日志。已在 Magpie 0.1.1246 验证。

```sh
git clone https://github.com/meii11/magpie-ssh-monitor.git
cd magpie-ssh-monitor
python3 manage.py add devbox --name "开发服务器"
```

把 `devbox` 换成你在 `~/.ssh/config` 中配置的别名。地址、用户、端口、跳板和密钥继续由系统 SSH 管理；本插件不复制密钥、不修改服务器的 Codex 配置。

```sh
python3 manage.py add buildbox --name "构建服务器" --interval 120
python3 manage.py list
python3 manage.py status devbox
python3 manage.py sync devbox
```

安装程序会读取真实远端数据、导入本机 Magpie、注册插件，并安装每台服务器的 LaunchAgent。程序安装到 Application Support，删除或移动源码目录不会影响已安装的采集器。

**请通过命令行安装。** 直接在 Magpie 中添加未配置的 GitHub 源码仓库，不会自动配置 SSH 采集器。

## 查看

- 用量 → 概览：`显示名称 · Codex SSH` 额度卡片。
- 用量 → 请求：Agent 选择 `SSH-别名`。
- 用量 → 会话：远端会话 ID 以 `别名/` 开头。

首次回填最近 7×24 小时，之后默认每 60 秒增量同步。Magpie 的“7 天”按自然日筛选，可能与首次导入总数不同；子任务按独立执行线程统计。

## 可选参数

```sh
python3 manage.py add devbox \
  --name "开发服务器" \
  --remote-dir "~/.codex/sessions" \
  --gateway http://127.0.0.1:3425 \
  --interval 60 \
  --days 7
```

- `--remote-dir`：远端日志目录，支持绝对路径、`~/` 和空格。
- `--gateway`：本机 Magpie 地址，仅接受 `http://127.0.0.1:端口`。
- `--interval`：5–86400 秒，默认 60；额度过期检查随轮询间隔调整。
- `--days`：首次回填 1–3650 天，默认 7。
- `--magpie`：指定 Magpie CLI 路径。
- `--manual`：只注册插件，不安装定时任务，手动运行 `sync`。
- `--root`：指定独立本地状态目录；这是全局参数，写在子命令前面，目录权限需为 700。

## 更新、移除

```sh
git pull
python3 manage.py update devbox
python3 manage.py remove devbox
```

更新保留身份、统计和历史起点；移除只停止指定服务器的后台任务、移除对应插件。已导入 Magpie 的用量与本地历史保留，再次添加相同别名可继续使用原进度。

## 数据边界

额度是带时间的日志快照，远端没有新请求就不会有新额度记录；不会把过期窗口显示成“已恢复满额”。只导入有明确请求 ID 的真实用量，重复记录和重试不会多算；模型归属不确定时会报告问题，不推算。

缓存输入单独统计，推理 Token 已包含在输出中。费用是 Magpie 按模型价格给出的 API 等价估算，不是订阅实际扣款；未知价格保留为未定价。导入行显示的 200 表示有用量记录，并非抓取的真实 HTTP 状态；本项目不监控完整的失败请求、耗时或任务完成通知。

首版不支持扫描范围内的 `.jsonl.zst` 压缩日志；遇到会明确报错。旧格式日志没有明确请求记录时，不重建其用量。

SSH 只返回 ID、模型、Token、额度窗口和时间，不返回对话正文、工具输出、凭据或未知字段。不发送遥测，不调用模型 API。

数据保存在 `~/Library/Application Support/MagpieSSHMonitor`。注册或移除插件前会备份本机 Magpie 配置；配置、数据库、快照、运行日志和本地工作记录均不会进入 Git。

## 开发

```sh
python3 -m unittest discover -s tests -v
node --test tests/*.test.mjs
```

欢迎提 Issue 和 PR。请提供版本、错误和最小脱敏记录，不要上传密钥、完整对话或本地数据库。

本项目是独立社区扩展，使用 Magpie 的[插件接口](https://usemagpie.ai/docs/plugins)与[公开用量导入接口](https://github.com/yetone/magpie/blob/main/docs/reference.md)，采用 [MIT 许可证](LICENSE)。
