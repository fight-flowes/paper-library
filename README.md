# Paper Library

这是一个模块化单体项目：论文数据、PDF 导入、离线翻译和本地阅读器在同一个项目目录中管理。GitHub 只同步代码、测试、文档与示例配置，`libraries/` 中的本地论文数据不上传。网页只读取已生成的中文译文，不会调用模型。

## 目录结构

```text
paper-library/
├── libraries/                 # 论文数据
│   └── <library-id>/
│       ├── library.toml
│       ├── catalog.tsv
│       └── papers/<paper-id>/
│           ├── source.pdf
│           ├── article.md
│           ├── metadata.json
│           └── images/
├── src/paper_library/         # 应用代码
├── scripts/                   # 稳定的命令入口
└── tests/
```

论文库 ID 和论文 ID 决定 URL 与翻译缓存身份。论文标题、文件名和项目绝对路径变化不会使译文失效。`catalog.tsv` 只保存论文属性，文件位置统一由论文 ID 推导。

## 安装与配置

```bash
conda activate paper
python -m pip install -r requirements.lock
python -m pip install --no-build-isolation --no-deps -e '.[dev]'
cp .env.example .env
# 首次克隆时建立一个空论文库
mkdir -p libraries
cp -R examples/library libraries/example
```

`.env` 是唯一的项目配置文件，且已被 Git 忽略。模板默认 profile 是 `qwen3.8-27b`，并发为 32；DeepSeek 配置也保留在模板中，并发为 16。实际生效值来自项目 `.env`，不读取其他项目的配置。可配置：

- `PAPER_LIBRARY_HOST`、`PAPER_LIBRARY_PORT`、`PAPER_LIBRARY_CONDA_ENV`
- `PAPER_LIBRARY_LIBRARIES_DIR`、`PAPER_LIBRARY_STATE_DIR`
- `PAPER_LIBRARY_UPLOAD_MAX_MB`（默认 256，允许 1–4096）
- `PAPER_LIBRARY_ENABLE_REMOTE_MANAGEMENT`（默认 0）
- `PAPER_TRANSLATION_PROFILE`、`PAPER_TRANSLATION_CACHE`
- `LLM_TEMPERATURE`（默认 `0.1`，取值 0–2，翻译、断段修复、结构校对和导读统一使用；修改后重启阅读器，已有缓存保留）
- 每个 `LLM_<名称>_MODEL` 对应的 `BASE_URL`、`API_KEY`、`TIMEOUT_SECONDS`、`MAX_OUTPUT_TOKENS`、`MAX_CONCURRENCY`、`REQUEST_STYLE`

如需从其他位置读取 dotenv，设置 `PAPER_LIBRARY_SETTINGS_FILE`。配置文件路径属于运行环境配置，不应写进项目 `.env`。

## 阅读器

```bash
scripts/dev up
scripts/dev status
scripts/dev logs
scripts/dev urls
scripts/dev restart
scripts/dev stop
scripts/dev doctor
```

默认地址是 `http://127.0.0.1:18020/`。后台 PID 和日志保存在被忽略的 `.paper-library-dev/`。停止进程前会核对 PID、工作目录和命令行，避免误停其他服务；启动完成以 `/health/ready` 为准。

### 从网页导入

在首页点击“导入论文”，或打开 `http://127.0.0.1:18020/manage/import`：

1. 选择一个本地 PDF 和目标论文库。
2. 从下拉框选择研究主题，也可点击“新增主题”。
3. 选择原文语言，并决定是否检索书目信息。
4. 默认勾选“解析后自动翻译”。取消勾选时仍执行解析、断段修复、信息核对、结构校对和导读，完成后可在任务页点击“开始翻译”。

提交后页面按阶段显示排队、PDF 解析、段落修复、信息检索与修正、发布入库、结构校对、翻译和自动导读。解析等缺少可靠计数的阶段显示等待动画和当前步骤；段落修复显示已检查断点数，翻译显示已处理段落数及失败数。页面区分阶段耗时、最近处理进展与后台心跳，状态请求超时或断网时明确提示并自动重试。关闭页面或重启阅读器不会丢失任务；重新打开最近任务即可继续查看。解析失败或翻译服务暂时不可用时，任务页保留可重试状态。任务页逐项保存解析、段落检查、信息核对、翻译和导读结果。信息核对未成功、段落检查未确认、译文阅读提示或导读失败，均显示“已完成，有待处理项”；重试复用已发布论文，补核验信息、重试带提示译文并续生成导读，不重复解析 PDF。排队或运行中的任务可以取消。

上传同时校验 `.pdf` 后缀和 PDF 文件头，并以流式方式执行大小限制。管理操作默认只在回环地址可用。若确实需要绑定非本机地址，必须显式设置 `PAPER_LIBRARY_ENABLE_REMOTE_MANAGEMENT=1`，并自行确保网络访问受到保护。

运行状态位于 `.paper-library-dev/`：

```text
.paper-library-dev/
├── jobs.sqlite3             # 持久化任务、阶段和进度
├── management.token         # 自动生成，权限 0600，不要复制或提交
└── uploads/<job-id>/        # 尚未结束或可重试任务的暂存 PDF
```

成功、取消和不再需要的暂存文件会被清理；失败任务保留上传文件以支持重试。网页导入只新增或更新目标论文包，不会批量改写已有论文。

## 论文库

```bash
scripts/library list
scripts/library check
scripts/library show --library whitespace --paper ws-p0001
scripts/library ingest --library whitespace /path/to/paper.pdf --subject related-work
scripts/library refresh-metadata --library whitespace --paper ws-p0001
```

导入命令在 Conda `paper` 环境中运行 MinerU。新论文先写入临时目录，通过校验后再发布论文包和目录。相同 PDF 哈希默认视为已入库；使用 `--force` 可重新解析，`--dry-run` 只显示目标编号。`--no-enrich` 跳过本机 Tavily Gateway 的书目信息补全。

书目信息核验会结合正文标题、原始文件名和可读取的 PDF 标题进行检索，并检查已有 DOI、arXiv 等标识是否冲突。官方页面标题与本地线索确认一致后，`refresh-metadata` 同步修正 `catalog.tsv` 和 `metadata.json`，必要时修复残缺的正文标题；新导入也使用相同核验逻辑。无法确认时保留原信息。每次刷新会在论文包的 `metadata-repairs/` 下保存原文件和包含来源、匹配线索的核验报告，发布失败时回滚已修改的文件。

网页导入与 `scripts/library ingest` 使用同一套解析和原子发布服务；网页自动翻译与 `scripts/translate run` 使用同一翻译缓存和 `.env` 模型配置。终端命令适合批处理和自动化，网页适合单篇导入与查看持久化进度。

导入页的研究主题下拉框随论文库切换。点击“新增主题”可保存并选中新主题，无需先导入论文；主题保存在各论文库的 `subjects.json`，并与目录中已有的主题合并显示。

## 论文导读

导读使用 `.env` 当前配置的模型和并发，直接阅读原文，不依赖中文译文。命令预生成结果，阅读页的“导读”和各节“本节导读”只读取已保存内容。

```bash
bash scripts/guide run --library whitespace --paper ws-p0002
bash scripts/guide status --all
bash scripts/guide run --all
# 原文与模型相同时复用结果；主动重生成可加 --force
```

系统保留实际章节标题和顺序，按数字编号辅助识别子层级；未编号章节沿用 Markdown 层级，不强行改名为 Introduction/Method 等固定类别。模型结合实际内容说明章节作用，区分原文结论、帮助理解和阅读问题。较长章节分块读取，全文概览从带出处的章节要点汇总。原文已经丢失的标题或纯图片中的文字无法在本版自动恢复。

章节导读采用“本节概览 → 展开详细解读”：概览说明主线，复杂章节按理解难点补充步骤关系、术语含义、实验对比和适用条件。深度由内容复杂度决定，简单章节不强行扩写。长章节的分块结果会再整合、去重，形成一份连贯的章节导读。详细解读默认折叠，同样附有原文依据；提示词升级后重新执行 `guide run` 会生成新版结果，不需手动清空缓存。

每条要点必须带匹配原文的引文与定位链接。程序验证引文和位置存在，但不能据此保证模型的推理正确。导读基于解析后的文字及表格文本，不解读图片本身。无正文信息的作者单位、参考文献等允许跳过，不机械生成总结。

论文包的 `reading-guide.json` 保存完整导读、模型及原文版本；`reading-guide-cache/` 保存可续用的章节结果。原文变动后，网页隐藏旧导读并提示重新生成。失败不覆盖已有导读；批量运行会继续处理其他论文，并最终列出失败论文及具体章节原因。失败后去掉 `--force` 重新执行即可续用已完成章节（`--force` 会清空本轮章节缓存并全部重生成）。`PAPER_GUIDE_CHUNK_CHARS` 控制单次章节文本大小（默认 10000 字符），模型接口、超时、输出长度和并发沿用现有模型配置。

## 离线翻译

MinerU 导入后会先检查疑似断段，再进入翻译。规则筛选相邻正文及被完整图表块隔开的正文，当前 `.env` 翻译模型负责判断是否合并，并发沿用模型配置。确认断段后拼接正文；若中间有图表及图注，将其完整移动到合并段落之后，保留原有顺序。不确定、模型未配置或请求失败时保留原文。`PAPER_PARAGRAPH_REPAIR_ENABLED=0` 可关闭该步骤。

已有论文可单独修复：

```bash
scripts/library repair-paragraphs --library whitespace --paper ws-p0001
scripts/translate run --library whitespace --paper ws-p0001
```

每个输入版本的原文及判断记录保存在该论文包的 `paragraph-repairs/<原文哈希>/original.md` 和 `report.json`。需要撤销时，将对应 `original.md` 恢复为 `article.md`。合并后的段落重新翻译，未变段落继续命中缓存。筛选支持大写开头、短续段及 Table A1 等附录图表；保留作者单位、完整句末括号、诗行、代码、引用和列表结构。对未完成语法却被模型判为保留的断点额外复核，并记录两次判断。规则检查与模型判断仍可能遗漏，不能把运行完成等同于逐段人工核对无误；PDF 解析丢失文字时需对照原 PDF 恢复。

```bash
scripts/translate check
scripts/translate status --all
scripts/translate plan --library mechanism-interpretability --paper mi-p0001
scripts/translate run --library mechanism-interpretability --paper mi-p0001
scripts/translate run --all --concurrency 16
scripts/translate retry-warnings --library whitespace
```

每个任务的并发默认取当前 profile 的 `MAX_CONCURRENCY`，命令行可在 1 到 32 之间调整；同一模型端点的所有进程还共享配置中的并发上限（不超过 `PAPER_LLM_MAX_CONCURRENCY=32`）。每个成功段落立即保存，再次运行会跳过缓存内容。缓存默认位于 `~/.cache/paper-library/translations.sqlite3`，报告位于相邻的 `reports/` 目录。

译文按 `library-id/paper-id`、段落内容、模型和提示词版本隔离。数字、公式、代码、链接和格式标签会先替换成占位符；可阅读但存在轻微格式偏差的译文会保存并记录警告。网页翻译接口只接受 GET。

## 测试

```bash
bash scripts/verify
bash scripts/verify --browser
bash scripts/verify --browser --live
scripts/library check
scripts/dev doctor
```

翻译浏览器测试默认连接本机 `18020` 端口，可用 `PAPER_READER_URL` 和 `PAPER_TEST_BROWSER` 覆盖。管理流程测试会启动一次性本地服务，并只写入临时论文库。

### 文本结构校对

导入后的结构校对使用 `.env` 当前模型，在翻译与导读前运行。结构标注保存在每篇论文的 `document-structure.json`，不重写 `article.md`，不改变原有译文标识或出处锚点。新导读使用同一份有效结构标注。仅章节层级变化时，可执行 `scripts/guide reconcile --all`，验证既有引文后同步层级；章节边界变化则需 `scripts/guide run` 重新生成。

```bash
bash scripts/structure run --library whitespace --paper ws-p0003
bash scripts/structure status --library whitespace --paper ws-p0003
bash scripts/structure run --all
```

重复执行会复用已完成的分组；`--force` 重新生成。`PAPER_STRUCTURE_CHUNK_CHARS` 控制每组原文大小，默认 12000；模型、温度、并发沿用 `.env` 模型配置。超长单块保留原样，不截断后改写正文。

校对区分题目、作者、出版信息、摘要、正文、标题、图注和诗歌等角色。模型只读取解析后的文本，当前不读取图片，也没有利用未保存的 MinerU 页坐标。代码、公式块、表格、列表、含图片或显式硬换行的段落不自动调整。中英文混排、Unicode 和原文内容保留；诗歌标注保留软换行。无法确定时沿用原排版。无标注、标注损坏或原文指纹不一致时页面自动回退；模型失败不阻止后续翻译和导读，任务显示待处理项。

导入任务重试会复用已入库论文，并补查缺失或不确定的段落修复及未核验书目信息；重复上传已有 PDF 也执行这些检查。结构标注中的 `unknown` 会在下一次运行时重新判断对应分组，正常分组继续复用。翻译服务不可用或全部段落失败时，仍尝试生成导读，最终显示“已完成，有待处理项”，可重试。未开启翻译的已入库任务，即使带有警告，也可点击“开始翻译”。

### 阅读管理与稳定运行

已增加 [全文搜索](/search)、[最近阅读](/reading) 和 [运行状态](/manage/status)。论文右侧“标注”可保存原文高亮和笔记；“最近阅读”保存最近一次阅读位置，支持继续阅读。个人记录保存在独立数据库中；原文变化后无法可靠定位的记录保留并提示。

`bash scripts/ops` 提供备份、校验、恢复预览、历史版本和运行检查。支持 systemd 用户服务与计划备份；配置、恢复演练和升级操作详见 [运行维护说明](docs/maintenance/reader-operations.md)。


### 代码检查与发布边界

`requirements.lock` 固定验证过的 Python 3.11 应用、测试和构建依赖；MinerU 在解析环境单独安装，不随阅读服务升级其 GPU 依赖。`package-lock.json` 固定浏览器测试依赖。`scripts/verify --browser` 使用临时论文库，不要求模型、真实 PDF 或现有运行数据；`--live` 才检查当前 18020 阅读器和已有论文。

本项目使用独立 Git 仓库，远程地址为 `git@github.com:fight-flowes/paper-library.git`。`.github/workflows/check.yml` 定义 push / pull request 时的 CI 检查；部署前也可执行本地统一检查。`libraries/` 论文数据、`.env`、运行数据库、缓存和备份均不提交；首次克隆请按上面的步骤创建空论文库，再从网页导入 PDF。

布局职责：`reader.css` 保存基础排版和正文样式，`workspace.css` 保存列表、搜索、任务等公共工作区组件，`reading-layout.css` 保存论文页工具栏、右栏和抽屉布局，`reading-guide.css` 保存导读组件。新增样式应修改所属规则，避免持续在末尾叠加覆盖。
