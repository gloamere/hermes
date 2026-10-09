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
