# Paper Library 模块化单体重构设计

## 背景与目标

Paper Library 已经同时承担论文入库、元数据补全、离线翻译和网页阅读，但当前代码与数据都直接位于项目根目录，Shell 入口重复处理 Conda，解析业务集中在大型 `scripts/*.py` 文件中，论文库身份又隐含在物理目录名和绝对路径里。

本次重构把项目整理为一个可安装、可测试的模块化单体。代码和论文数据继续由同一项目管理，业务模块通过明确接口协作；终端入口只负责运行环境和编排。重构不兼容旧的 `reader.sh`、`parse_pdf.sh`、`translate.sh` 命令，但保留论文永久 ID、网页 URL 和全部有效 DeepSeek 译文。

## 项目边界

上级目录仍是多个研究项目的工作区。Paper Library 保持为其中独立的 `paper-library/` 项目，不把代码平铺到工作区根目录，也不吸收 MinerU、`cognitive_overload` 或 `pnas_nexus`。

目标目录：

```text
paper-library/
├── pyproject.toml
├── README.md
├── .env.example
├── scripts/
│   ├── _common
│   ├── dev
│   ├── dev_process.py
│   ├── library
│   └── translate
├── src/paper_library/
│   ├── config.py
│   ├── catalog/
│   ├── ingest/
│   ├── metadata/
│   ├── translation/
│   └── web/
├── libraries/
│   ├── mechanism-interpretability/
│   └── whitespace/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── scripts/
│   └── browser/
└── .paper-library-dev/
```

`.paper-library-dev/`、`.env`、Python/Node 缓存与翻译 SQLite 文件不进入 Git。

## Python 模块

`paper_library.config` 定义项目路径和运行配置。配置只在入口解析一次，优先级为命令行、进程环境、项目 `.env`、默认值。阅读器在没有模型配置时仍可运行。

`paper_library.catalog` 负责库发现、`library.toml`、catalog 模型、稳定路径、ID/别名唯一性和文件边界。其他模块不自行拼接论文路径。

`paper_library.ingest` 负责 MinerU 调用、临时工作区、永久 ID 分配和原子发布。发布器持有库级锁，完整验证后一次更新论文目录和 catalog。

`paper_library.metadata` 负责书目检索、匹配、覆盖值和元数据 Schema，不了解 CLI 或 Flask。

`paper_library.translation` 负责段落提取、模型配置、请求、缓存、批处理、质量警告和网页只读 DTO。终端翻译与网页读取复用相同服务。

`paper_library.web` 负责 Flask 工厂、路由、模板、静态资源和健康检查，不调用模型。

## 论文库和论文存储

每个论文库使用无空格的稳定目录名，并包含 `library.toml`：

```toml
schema_version = 1
id = "mechanism-interpretability"
name = "Mechanism Interpretability"
paper_id_prefix = "mi"
```

`id` 是 URL、CLI 和缓存使用的稳定身份；`name` 只用于显示；`paper_id_prefix` 只用于新论文编号。目录名必须等于 `id`，库改名只修改 `name`。

论文以永久 ID 保存：

```text
libraries/mechanism-interpretability/papers/mi-p0001/
├── source.pdf
├── article.md
├── metadata.json
└── images/
```

固定文件名使标题、年份、venue、subject 和原始文件名变化时无需移动文件。`metadata.json` 保存 `original_filename`、源文件哈希、解析版本、书目信息和补全来源。

新版 `catalog.tsv` 删除可推导的 `source_pdf` 与 `markdown` 字段，保留：

```text
id, year, venue, short_name, subject, title, sha256,
parsed_at, publication_date, aliases
```

读取器只接受新版表头。迁移工具负责把旧 catalog 和文件树一次转换为新版，日常运行不保留双格式兼容分支。

## 命令入口

### `scripts/dev`

提供 `up`、`stop`、`restart`、`status`、`logs`、`urls`、`run` 和 `doctor`。后台进程使用新会话启动，状态写入 `.paper-library-dev/`。PID 只作为提示；停止进程前校验当前用户、进程组、工作目录、模块参数和端口。外部进程占用 18020 时返回错误，不终止外部进程。

`up` 只有在 `GET /health/ready` 成功后才返回。就绪检查验证两个论文库均可发现且 catalog 可读取，不要求翻译模型在线。

### `scripts/library`

提供：

- `list`：列出库 ID、显示名、前缀和论文数量。
- `check`：只读检查配置、跨库 ID/别名、catalog 与论文文件完整性。
- `show --library ID --paper ID`：显示论文状态和路径。
- `ingest --library ID --subject SUBJECT PDF`：解析并发布论文。
- `refresh-metadata --library ID (--paper ID | --all)`：刷新书目信息。

`ingest` 接受任意位置 PDF。相同 SHA256 已存在时返回已有论文；失败或中断只清理临时目录，不发布部分结果。

### `scripts/translate`

提供：

- `check`：使用临时缓存发送一个短翻译请求。
- `status`：按库和论文统计完成、缺失与警告段落。
- `plan`：只计算任务，不调用模型。
- `run`：翻译缺失段落。
- `retry-warnings`：只重译有质量警告的段落。

选择范围统一为 `--all`、`--library ID` 或 `--library ID --paper ID`。并发仍限制为 1–32，默认读取当前模型配置。

`scripts/_common` 只提供项目根解析、Conda 环境选择、统一错误和 `run_in_env`；没有个人绝对路径。`scripts/library` 与 `scripts/translate` 最终执行 `python -m paper_library...`。

## 翻译缓存迁移

新缓存身份为稳定的 `library_id/paper_id`，不包含论文库绝对路径。缓存键继续包含段落规范文本、受保护内容、接口、模型和提示词指纹。

一次性迁移按以下步骤运行：

1. 从旧 catalog 加载每篇论文及段落。
2. 使用旧绝对路径身份计算现有缓存键。
3. 使用稳定库 ID 身份计算新缓存键。
4. 在同一 SQLite 事务中复制缓存文本和全部 provenance 字段。
5. 新旧键冲突时只接受内容与 provenance 完全一致的记录，否则中止。
6. 核对当前协议的 1283 段均能以新身份读取后，再完成数据目录切换。

缓存数据库迁入 `~/.cache/paper-library/translations.sqlite3`。原文件保留为带时间戳的备份，执行报告记录迁移、跳过和冲突数量。网页 URL 和论文 ID 不变化。

## 数据迁移与回滚

迁移在阅读器停止后执行，并使用项目内临时目录：

1. 验证旧 catalog、源 PDF、Markdown、图片和 metadata。
2. 在临时 `libraries.new/` 构建全部新版目录。
3. 核对每个 PDF SHA256、Markdown 字节内容、图片文件集合和论文数量。
4. 在临时根运行 catalog、渲染和翻译读取测试。
5. 原子切换到 `libraries/`。
6. 迁移翻译缓存并验证 1283 段。
7. 启动新阅读器并检查首页、两个库、六篇论文、PDF、图片和译文接口。

任一步失败都不删除旧论文树或旧缓存。迁移成功并完成验证后，旧树才从 Git 工作区移除；缓存备份继续保留。

## 测试

`pyproject.toml` 定义运行依赖和 `dev` 可选依赖，测试不读取真实 `.env`。测试分为：

- 单元测试：配置、库模型、路径边界、ID、渲染、翻译键和 metadata。
- 集成测试：入库事务、跨库唯一性、缓存迁移和 Flask 路由。
- 脚本测试：帮助、参数转发、Conda 执行、进程身份、外部端口保护和健康等待。
- 浏览器测试：双语切换、部分译文、警告样式、移动布局和零模型 POST。
- 迁移测试：在临时旧目录和 SQLite 副本上运行完整迁移两次，证明幂等。

最终验证包括全量 pytest、Playwright、Shell 语法、真实 DeepSeek `check`、全库翻译 `plan`、正式 HTTP 健康检查和 Git 范围检查。

## 验收条件

- 只有 `scripts/dev`、`scripts/library`、`scripts/translate` 三个公开入口。
- Python 业务代码全部位于 `src/paper_library`，`scripts` 中无业务实现。
- 两个论文库均位于 `libraries/`，每篇论文使用永久 ID 目录和固定文件名。
- 旧命令被删除，README 只描述新命令。
- 六篇论文、PDF、Markdown、图片、元数据、别名和网页 URL 均可用。
- 当前 1283 段 DeepSeek 译文全部迁移，3 段质量提示仍可识别。
- 阅读器启动成功前必须通过 HTTP 就绪检查，停止操作不会误杀外部进程。
- 配置、运行状态和秘密文件边界明确，测试不依赖开发机状态。
- 全部自动化验证通过，Git 暂存区不包含 `.env`、缓存、日志或其他项目。
