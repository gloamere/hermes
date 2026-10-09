# Gloamere Hermes 运维 v0.1.0

本目录是独立的主机运维脚本，不向 Hermes 的模型工具列表添加工具。Telegram 凭据从现有 Hermes `.env` 读取，源代码与配置示例不含真实 Token、代理订阅、GitHub 凭据或 age 私钥。

## 当前部署

- 主程序 `/home/Gloamere/hermes`；用户状态 `/home/Gloamere/.hermes`；工作目录 `/home/Gloamere/workspace`。
- 本地中文 Whisper `base`、CPU/int8，空闲 10 分钟卸载；中文 Edge TTS `zh-CN-XiaoxiaoNeural`。权重与 Python/浏览器运行时可以重新下载，不进入每日备份。
- 子代理同步和后台并发共用 `delegation.max_concurrent_children: 3`。
- Curator 启用自动归档及技能合并，每周检查一次，空闲 2 小时后运行，合并前保留 2 份技能恢复点；内置技能不自动归档。
- Netdata 使用 Ubuntu 签名软件包，每 5 秒采样；CPU、内存、磁盘、网络、温度等历史数据存于本机。三层数据库配置 512/128/64 MB 的软容量目标，不连接 Cloud，不发送匿名遥测。
- 每 2 分钟检查服务、温度、SMART、磁盘和备份状态。CPU ≥95%、内存 ≥90% 持续 10 分钟通知；磁盘 ≥85%、CPU ≥85℃、固态 ≥70℃、服务异常立即通知。同一异常 6 小时最多一次，恢复时通知一次。
- 每天北京时间 09:00 向机器人主人发送简报，直接读取数据，不调用模型。任务失败通过 systemd `OnFailure` 通知；代理断网时会重试投递。

## 聊天状态指令

Hermes 会话和 Telegram 支持以下中文说明指令，读取本机数据，不调用模型，也不发起云端查询：

- `/server`：约 1 秒采样的 CPU、1/5/15 分钟负载、内存、交换空间、磁盘容量、固态读写、物理网卡上下行、CPU/固态最高温度、SMART 健康、服务状态和开机时长。
- `/backups`：每日完整备份的最近结果、时间、包大小、SQLite 校验和会话数量，工作目录备份、云端留存与清理记录、容量限制和下次计划；同时列出本地配置备份、升级前完整包、快速快照、代码检查点及 Curator 技能恢复点。云端信息来自最近任务的校验记录，不代表查询时再次检查 GitHub；启用但没有恢复点会明确显示“尚无记录”。

两条指令通过独立的 `gloamere-status` 插件注册，不修改 Hermes 核心。Telegram 私聊另用插件的原生平台处理器，即使代理正忙也能查询，不进入任务队列或打断任务；其他会话沿用通用插件分发。私聊处理器使用网关安装在适配器上的授权回调，未授权或未安装回调时拒绝读取。查询参数不会进入 Shell；插件用现有 sudo 权限调用只读主机组件。查询超时和读取失败返回中文说明，不向聊天输出凭据或原始错误日志。

```bash
sudo bash ops/gloamere/install-status.sh /home/Gloamere/.hermes
# 当前会话空闲时重启，使插件和 Telegram 菜单生效。
sudo systemctl restart hermes-gateway.service
# 命令行也可以直接查询：
sudo python3 /usr/local/lib/hermes-ops/status_query.py server
sudo python3 /usr/local/lib/hermes-ops/status_query.py backups
```

安装脚本保留已有配置，通过 Hermes 官方配置写入器启用插件，将两条命令放在菜单前面，并把菜单容量至少设为 62。验证使用 `scripts/run_tests.sh tests/ops/test_gloamere_status.py tests/ops/test_gloamere_operations.py`，覆盖真实插件发现、网关命令分发、超时回收和备份记录损坏等情况。

## 安装与验证

```bash
sudo apt-get install --no-install-recommends netdata netdata-plugins-python lm-sensors smartmontools nvme-cli age
sudo bash ops/gloamere/install.sh
# 按示例配置 /etc/hermes-ops/config.json、/etc/hermes-backup/config.json，权限 0600。
sudo python3 /usr/local/lib/hermes-ops/monitor.py check
sudo hermes-backup --force
sudo python3 /usr/local/lib/hermes-ops/monitor.py report
sudo systemctl enable --now hermes-monitor.timer hermes-report.timer hermes-backup.timer
```

备份仓库要先以 GitHub CLI 登录并克隆到配置中的 `repository`，必须是私密仓库；配置正确的 Git 提交身份。公钥文件指向用户离线保存的 age 私钥，服务器不保存解密私钥。

监控面板只监听服务器回环地址。电脑运行 `ssh -N -L 19999:127.0.0.1:19999 root@10.10.27.188`，然后打开 `http://127.0.0.1:19999`。

```bash
systemctl list-timers 'hermes-*'
journalctl -u hermes-backup -u hermes-monitor -u hermes-report --since today
HERMES_PYTHON=/path/to/pm-test-environment/bin/python scripts/run_tests.sh tests/ops/test_gloamere_operations.py
```

## 备份与容量

每天 03:30，先调用官方 `hermes backup`，校验 ZIP 与 SQLite；再打包工作目录、源码提交及未提交补丁、代理/systemd/sudo 服务配置、GitHub CLI 登录状态、运维脚本等，整体使用 age 加密。工作目录不追踪外部符号链接，排除 `.git`、`node_modules`、`.venv`、`venv`、`__pycache__`、`.cache`、`tmp`；普通文件超限则整次备份失败并通知，不会默默漏掉。

私密仓库 `YYYY-MM/YYYY-MM-DD/manifest.json` 记录校验与 Release 标签，密文存放在同仓库 Release 附件，64 MB 分片。GitHub 返回的附件 SHA-256 必须匹配；没有服务器摘要时下载验证。确认新备份与远端 main 一致后才清理旧附件和专用备份标签，避免每日密文进入 Git 历史而无法回收。

保留最近 30 天的每日快照，以及近 12 个自然月各自最新的一份快照；同一天更新后保留最新快照。总密文上限 5 GiB，超额时优先删除最旧快照，始终保留最新可恢复快照；单次密文最多 512 MiB，工作目录输入最多 2 GiB、单文件最多 256 MiB。超限会失败通知，管理员可以调整私有配置。新目录规则生效前的旧 Git 密文约 2 MB 会留在历史中，此后只有小清单进入历史。

临时明文仅在受限 staging 中存在，运行结束自动删除；检查脚本不会删除工作目录中的项目或文档。日志每周轮转 4 份、10 MB 触发轮转。配置备份并不包含 Netdata 的性能数据库；该数据库自身按容量淘汰。

## 迁移恢复

1. 取得私密备份仓库、所选日期清单和离线 age 私钥。
2. `gh release download <release_tag> -R gloamere/hermes-backups -D /secure/restore`。
3. 按清单顺序校验并拼接 `hermes.tar.age.part-*`，使用 `age -d -i /offline/age-identity.txt -o payload.tar hermes.tar.age` 解密。
4. 在受限目录解包。按备份中的 `deployment.json` 克隆指定源码 SHA；用官方 `hermes import hermes.zip` 恢复用户状态。
5. 把 `workspace.tar.gz` 恢复到工作目录，检查符号链接再使用；保留原有工作目录，确认完成后再切换。
6. 根据 `host/` 恢复代理、systemd、GitHub CLI、运维配置，重新安装运行时与模型权重；不要盲目覆盖新机器的网卡、IP 或 Netplan。
7. 验证 OAuth、Telegram、sudo、浏览器/桌面、语音、备份与监控，然后启用定时器。

官方参考：[Hermes backup](https://hermes-agent.nousresearch.com/docs/reference/cli-commands#hermes-backup)、[迁移恢复](https://hermes-agent.nousresearch.com/docs/reference/faq#exporting-hermes-to-another-machine)、[Telegram 语音](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram)、[Netdata](https://learn.netdata.cloud/docs/netdata-agent/configuration)。

## 网页管理与通用网页工具

`hermes-dashboard.service` 使用同一个用户状态目录，开机启动并监听 `127.0.0.1:9119`。通过 SSH 转发访问，默认聊天工作目录为 `/home/Gloamere/workspace`，使用常驻代理，服务失败接入既有告警。

```bash
sudo install -m 0644 ops/gloamere/systemd/hermes-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-dashboard.service
# 在自己的电脑执行，并保持此连接：
ssh -N -L 9119:127.0.0.1:9119 root@10.10.27.188
```

打开 `http://127.0.0.1:9119`。服务使用预先构建的 `hermes_cli/web_dist`；升级前端后应按官方构建流程刷新，再重启此服务。服务文件纳入每日加密备份，前端与 Node 依赖可重新构建。

搜索和网页提取分别指定 `web.search_backend: exa`、`web.extract_backend: exa`，使用现有内置匿名免费服务及故障回退，不额外保存 API Key。免费服务有额度和可用性限制；失败时沿用工具错误提示，不声称联网成功。官方 Memory Wiki 目前仅调研，未安装；其当前主题提取不适合纯中文内容。

## 每日开发工具简报

Hermes 原生 cron 每天北京时间 09:30 运行 `collect-news.sh`，采集 Hermes、Codex 与 GitHub CLI 官方发布 Atom，再由默认模型整理中文摘要，投递到现有 Telegram 主人私聊；失败也向同一私聊告警。`continuity` 使用上一回输出按链接去重，过滤 alpha/beta/canary/preview/nightly。数据窗口 48 小时、最多 6 条；没有新发布也仅发送一句简报。

采集脚本只读取三个源，不下载文章附件，使用 ETag/Last-Modified 缓存；每源最大 512 KiB、请求超时 15 秒、整个脚本最多 65 秒。缓存最多 3 个固定文件和 14 天小清单，均位于 `$HERMES_HOME/data/news-digest`，任务脚本和 job 定义跟随官方用户状态备份。

```bash
install -m 0600 ops/gloamere/news_digest.py /home/Gloamere/.hermes/scripts/
install -m 0700 ops/gloamere/collect-news.sh /home/Gloamere/.hermes/scripts/
hermes cron list --all
hermes cron pause <job_id>
```
