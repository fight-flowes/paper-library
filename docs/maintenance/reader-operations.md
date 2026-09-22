# 本地论文阅读器：数据与运行维护

当前按本地单用户部署。代码和论文仍在同一个项目中，阅读记录另存于运行目录的 `personal.sqlite3`，重算翻译或导读不会覆盖笔记。

## 日常入口

- `/search`：题目、作者、原文、当前模型的已保存译文。支持中英文连续文本搜索，按论文分页（每页 20 篇、每篇最多 60 处匹配）；索引每 10 秒后台增量更新，不调用模型。移除论文后索引也会更新。
- `/reading`：最近阅读进度、标注和 JSON/Markdown 导出。
- 论文右侧“标注”：选中同一段原文后保存笔记或高亮；历史位置标记仍可查看，不再提供新建入口。高亮着色使用 CSS Highlight API，不支持该 API 的浏览器仍能查看记录并跳转。
- 阅读进度自动保存，重新打开后通过“继续上次阅读”返回。原文变化后，用引用及上下文重新定位；无法唯一定位的记录保留并提示失效，不自动跳到猜测的位置。当前选文标注针对原文，未扩展到译文或 PDF 画布。
- `/manage/status`：数据库、磁盘、依赖配置、最近备份时间和任务阶段记录。

## 数据版本

自动导入保留 MinerU 原始 Markdown。受支持的终端处理命令和后台导入在修改前后生成内容指纹及 `.history/<指纹>/` 快照，新版本包含当时的正文、书目信息、结构标注、导读、PDF 和图片。旧版快照仍可恢复其已记录的文本文件，不假装包含当时未保存的图片。重复版本不再保存；重新解析同一论文也保留历史。手工编辑文件不等于实时版本控制，应在编辑前先备份。

```bash
bash scripts/ops history --library whitespace --paper ws-p0003
bash scripts/ops restore-version --library whitespace --paper ws-p0003 --version <版本哈希> --target /新的/单篇恢复目录
```

结构标注和导读继续校验原文指纹；译文继续按内容与模型配置隔离。历史快照用于追溯，并不自动重新运行所有过期模型产物。PDF、图片、全部历史和个人数据库由整库备份保护。

## 备份与恢复

```bash
bash scripts/ops backup
bash scripts/ops verify .backups/某份备份
bash scripts/ops restore .backups/某份备份 --target /新的/恢复目录
bash scripts/ops restore .backups/某份备份 --target /新的/恢复目录 --apply
```

恢复默认只预览，`--apply` 只允许写入尚不存在的新目录。每个文件都有 SHA-256 与大小校验，SQLite 使用在线备份接口，不直接复制 WAL 主文件。恢复后的未完成任务暂停为可手动重试状态，避免打开恢复库时自行调用模型。

备份包括论文库、历史快照、任务及阶段记录、待处理上传文件、译文缓存和个人阅读数据库。凭据、管理令牌、PID、日志及可重建的模型分组缓存/搜索索引不进入备份。恢复目录的 `restore.env` 指定恢复后的数据路径；可用当前代码验证：

```bash
PAPER_LIBRARY_SETTINGS_FILE=/新的/恢复目录/restore.env \
  conda run --no-capture-output -n paper python -m paper_library.cli.ops check
```

确认恢复库正常后，再把项目 `.env` 中的 `PAPER_LIBRARY_LIBRARIES_DIR`、`PAPER_LIBRARY_STATE_DIR`、`PAPER_TRANSLATION_CACHE` 指向它并重启；不要在运行时覆盖当前数据库。模型凭据仍使用原项目配置，未包含在备份中。

`PAPER_BACKUP_DIR` 可配置独立磁盘目录，默认 `.backups/`。同盘备份可用于撤销误改，无法防止整块磁盘损坏。计划任务默认每小时检查一次，距离最近备份满 `PAPER_BACKUP_INTERVAL_HOURS=24` 小时才创建，成功校验后保留最近 `PAPER_BACKUP_KEEP=7` 份有效备份；手工执行 `backup` 不清理旧备份。手工备份遇到写入任务会退出；计划备份最多等待 `PAPER_BACKUP_WAIT_SECONDS=300` 秒，并阻止后续写入任务不断抢占维护窗口。等待超时和校验失败会记录到状态页，下个计划周期重试。超过两倍计划间隔时显示备份过期。设置 `PAPER_BACKUP_MIRROR_DIR` 可在第二磁盘或挂载目录保存并校验副本；该目录由用户提供，本项目不会自行创建远程备份服务，也不自动清理镜像目录。

## 任务恢复和资源限制

后台仍一次执行一篇论文；模型并发沿用 `.env`。终端与网页的翻译、断段修复、结构校对和导读共享端点级文件锁额度；进程异常退出自动释放。默认槽位在运行目录 `llm-slots/`，同一项目的所有入口应使用相同 `.env` 与运行目录。多个独立部署需要共享 `PAPER_LLM_STATE_DIR` 才能统一限制。论文级写入锁隔离终端和后台操作，维护锁防止备份与受支持的写入命令交错。`PAPER_JOB_MAX_PENDING=32` 限制排队及运行中的任务总数。任务租约到期可恢复，连续异常恢复达到 `PAPER_JOB_MAX_RECOVERIES=3` 后转为失败，需查看阶段记录后手动重试。用户重试会重置计数。

MinerU 优先使用 `PAPER_MINERU_EXECUTABLE`，其次使用当前 Python 环境的可执行文件；总超时默认 3600 秒、无输出超时默认 900 秒，可通过 `.env` 调整。超时终止所属进程组并允许重试。后台线程错误有监督重启，`/health/worker` 单独报告工作线程是否存活和心跳是否及时。

正常停机尽量把未完成任务重新排队；单个请求没有在停机超时内返回时，由下次启动通过租约恢复。用户点击“取消”仍保持取消语义。重复导入、翻译失败继续导读、不确定结构分组重试等规则继续有效。

## 服务、监控与日志

```bash
bash scripts/ops check
bash scripts/ops check --network
bash scripts/ops install-user-units
systemctl --user daemon-reload
systemctl --user enable --now paper-library.service paper-library-backup.timer
```

如当前通过 `scripts/dev up` 启动，安装单元后先用 `scripts/dev restart` 切换，避免同端口运行两个进程。此后 `scripts/dev up/stop/restart/status` 会识别本项目的 systemd 服务。不要同时启用第二份实例。

用户服务启用后在用户服务管理器启动时运行，异常退出自动重启，并有限制重启频率。是否允许退出登录后继续运行由系统的用户 lingering 设置决定，本项目不修改该全局设置。

```bash
systemctl --user status paper-library.service paper-library-backup.timer
journalctl --user -u paper-library -n 80
systemctl --user start paper-library-backup.service
```

结构化诊断日志为运行目录 `events.log`，默认每份 `PAPER_LOG_MAX_MB=5` MB、保留 `PAPER_LOG_BACKUPS=3` 份，隐藏凭据，不记录模型提示词或原始响应。完整任务阶段历史保存在任务数据库，可从状态页查看。模型 `/models` 可连接只表示接口连通，不等同于推理质量或完整任务成功。

升级流程：先创建并校验备份，停止服务，更新代码与依赖，运行测试和 `ops check`，再启动并检查就绪端点。失败时保留故障数据，从备份恢复到新目录后切换数据路径；不要把旧版程序直接连接未经确认的新数据库。


## 本轮可靠性约定

- 正式入口使用 Waitress，线程数由 `PAPER_HTTP_THREADS=8` 控制；`/health/ready` 反映缓存阅读可用性，`/health/worker` 反映后台处理可用性，应同时监控。
- 论文包与目录发布先写持久事务日志；回滚再次失败时保留 `.transactions/` 下的旧包及目录副本，启动服务时恢复。存在未恢复事务时拒绝创建看似完整的备份。
- 终端脚本不再 `source .env`，配置由 Python dotenv 解析。仅选择 Conda 环境需要轻量读取一个受限的环境名称，不执行配置中的 Shell 内容。只读命令不再锁住全部论文或生成历史快照。
- 新模型结果记录模型、温度、输出上限和请求方式。已有译文/导读保留以避免参数微调触发整库重算；要按新参数完全生成请显式使用 `--force`。结构校对原有指纹包含温度，温度变化后重新运行会更新结构结果。旧产物没有生成参数时保留未知，不补写当前参数冒充历史。
- 阅读位置采用乐观版本校验，旧页面不能覆盖别处的新位置。临时网络错误最多重试三次，冲突时允许恢复其他页面的最新位置；进度按原文位置计算，切换中英文不会改变分母。
- 搜索是可重建索引，失败不影响打开论文。首次启动后台建立索引时会明确提示，新增内容通常在 10 秒刷新周期之后可检索。
