# Paper Library

这是一个模块化单体项目：论文数据、PDF 导入、离线翻译和本地阅读器在同一个项目目录中管理。GitHub 只同步代码、测试、文档与示例配置，`libraries/` 中的本地论文数据不上传。阅读页只读取已生成的中文译文、结构标注与导读；网页导入会由后台任务调用模型处理。

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
│           ├── images/
│           ├── document-structure.json  # 文本角色与目录复核结果
│           └── reading-guide.json       # 已生成的论文导读
├── src/paper_library/         # 应用代码
├── scripts/                   # 稳定的命令入口
└── tests/
```

论文库 ID 和论文 ID 决定 URL 与翻译缓存身份。仅修改目录中的论文标题、文件名或项目绝对路径不会使译文失效；修改正文会影响对应段落的缓存匹配。`catalog.tsv` 只保存论文属性，文件位置统一由论文 ID 推导。

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

`.env` 保存部署基础配置，且已被 Git 忽略。前端设置单独保存在运行目录的 `settings.json`，权限为 `0600`。相同配置项按“进程环境变量 → 前端设置 → 项目 `.env` → 默认值”的顺序生效，不读取其他项目的配置。模板默认 profile 是 `qwen3.8-27b`，并发为 32；DeepSeek 配置也保留在模板中，并发为 16。可配置：

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

论文库支持标题搜索、主题、年份与期刊筛选，以及主题内的发表时间、年份、导入时间和标题排序；筛选条件保存在网址中。全文搜索可限定论文库与原文、译文或书目信息。

阅读页提供正文快捷入口、分组可折叠目录和章节筛选。组合文档只在正式论文标题与自身 DOI 能唯一定位时显示“跳到论文正文”，目录仍保留所有原有章节。章节导读展开时读取已保存内容，读取失败可重试；图片延迟加载并按已有图片尺寸预留空间。更多操作中的“阅读设置”可调整字号和行距，浏览器会记住设置与中英对照状态。这些显示设置不改写论文、结构标注或译文缓存。

`npm run test:frontend` 对当前阅读器进行只读浏览器验收，覆盖筛选排序、正文跳转、导读失败重试、阅读设置与 320–1440px 布局；`bash scripts/verify --browser --live` 包含 Python、隔离导入流程及所有阅读器浏览器回归测试。

### 前端设置

顶部的齿轮入口打开 `http://127.0.0.1:18020/manage/settings`，仅提供日常使用的模型名称与并发、模型接口连接、cicore01 SSH 连接与测试，以及导入默认值。模型接口和 SSH 认证信息默认折叠，需要修改连接时再展开。当前各处理步骤共用默认模型，Qwen/DeepSeek 保持非思考模式。

远端 Python、解析目录、处理窗口、超时、输出长度、温度、监听端口、数据与缓存路径、备份、日志、进程与队列管理继续由后端配置负责，不在设置页展示或编辑；设置接口也拒绝这些字段。页面不提供整份 `.env` 的编辑入口。

连接测试使用当前输入，不保存配置。保存模型或解析连接时还会验证一次；认证、连接或环境检查失败会保留原配置。密钥与 SSH 密码不回显；留空保留原值，模型密钥可显式清除，SSH 可切换至密钥或代理认证。设置页仅供绑定回环地址的单用户服务使用，写操作沿用管理令牌并校验来源。

保存成功后无需重启，新开始执行的任务与新启动的命令行操作使用新配置，排队任务在领取时取得配置；运行中任务保持本次执行开始时的完整配置，重试或重启恢复则重新读取配置。设置页区分待保存与已生效状态，多个窗口的过期保存返回冲突。模型切换后仍能读取先前模型生成的译文，并保留实际生成模型信息；结构与导读文件不因保存设置而重新生成。

PDF 解析固定到 cicore01。`PAPER_MINERU_REMOTE_MACHINE_ID` 保存通过已有可信 SSH 连接核实、且与远端部署清单一致的 `/etc/machine-id`，前端不能修改；任何解析都在上传 PDF 前核对该标识并排除本机。解析连接设置不改变后台备份所用的 `.env` SSH 连接。`npm run test:settings` 使用临时论文库和模拟模型接口验收完整设置流程，不改生产配置、不解析 PDF。

### 从网页导入

在首页点击“导入论文”，或打开 `http://127.0.0.1:18020/manage/import`：

论文库和研究主题下拉框旁的加号按钮分别用于新增论文库和主题。新建论文库只需填写名称，系统自动分配内部编号与论文编号前缀，创建后立即选中，无需重启服务；同名论文库不能重复创建。

1. 选择一个本地 PDF 和目标论文库。
2. 从下拉框选择研究主题，也可点击“新增主题”。
3. 选择原文语言；默认勾选“Tavily 检索并修复书目信息”，可按需取消。
4. 默认勾选“解析后自动翻译”。取消勾选时仍执行解析、断段修复、信息核对、结构校对和导读，完成后可在任务页点击“开始翻译”。

网页导入按以下顺序执行：

```text
上传并排队 → MinerU 解析 → 段落修复 → Tavily 信息核验 → 发布入库
          → 结构校对（含目录复核与修复）→ 翻译 → 生成导读
```

未勾选的信息检索或翻译步骤会跳过。结构校对在同一阶段内检查文本角色、目录遗漏、误收录和章节层级；翻译与导读随后执行，阅读页目录与导读使用同一份有效结构标注。

解析等缺少可靠计数的阶段显示等待动画和当前步骤；段落修复显示已检查断点数，结构校对显示已处理检查项数，翻译显示已处理段落数及失败数。这些是当前阶段的进度，不是整条导入流程的百分比。页面分别显示阶段耗时、最近处理进展与后台心跳，状态请求超时或断网时提示并自动重试。

任务页保存各阶段结果。信息核对会区分检索暂不可用、未找到官方记录、候选证据不足和官方记录冲突；有可信候选时显示官方链接供核对，自动修复仍要求本地内容与官方记录一致。信息核对未成功、段落或结构判断不确定、译文带有阅读提示、导读失败时，会显示“已完成，有待处理项”。结构校对失败保留已有结构或原始排版，仍尝试翻译和导读；翻译失败也不阻止生成导读。

当前恢复与取消行为：

- 关闭页面不会停止后台任务；任务记录持久化，重启服务后可恢复调度。
- 已发布入库的论文重试时复用论文包，补做检查并复用有效的结构、译文及导读缓存，不重复解析 PDF。
- **尚未入库时，不保证从解析完成处续跑。** 解析结果仍在临时目录中，此时异常或服务重启可能需要重新执行 MinerU。
- 排队或运行中的任务可以取消。取消采用协作检查，进行中的 Tavily 或模型请求可能需要等待返回或超时，不能保证立即停止。

首次导入与已有论文的信息刷新都使用 Tavily 核验，但目前修正范围有差异：首次导入更新目录和元数据标题，不同步改写正文标题；`scripts/library refresh-metadata` 在符合匹配条件时还会修复残缺的正文标题。

上传同时校验 `.pdf` 后缀和 PDF 文件头，并以流式方式执行大小限制。管理操作默认只在回环地址可用。若确实需要绑定非本机地址，必须显式设置 `PAPER_LIBRARY_ENABLE_REMOTE_MANAGEMENT=1`，并自行确保网络访问受到保护。

运行状态位于 `.paper-library-dev/`：

```text
.paper-library-dev/
├── jobs.sqlite3             # 持久化任务、阶段和进度
├── management.token         # 自动生成，权限 0600，不要复制或提交
└── uploads/<upload-id>/        # 尚未结束或可重试任务的暂存 PDF
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

导入命令在本机 Conda `paper` 环境中调度 SSH/SCP，MinerU 仅在 `cicore01` 的独立 `paper-mineru` 环境运行。本机禁止解析 PDF；服务器连接、CUDA、模型或解析失败时直接报错，不回退到本机。新论文先写入临时目录，通过校验后再发布论文包和目录。相同 PDF 哈希默认视为已入库；使用 `--force` 可重新解析，`--dry-run` 只显示目标编号。`--no-enrich` 跳过本机 Tavily Gateway 的书目信息补全。

远程配置使用 `PAPER_MINERU_MODE=remote` 与 `PAPER_MINERU_REMOTE_HOST/PORT/USER/PYTHON/ROOT`。密码只保存于本项目 `.env` 的 `PAPER_MINERU_REMOTE_PASSWORD`，也可配置独立 SSH 密钥 `PAPER_MINERU_REMOTE_IDENTITY_FILE`。SSH 严格校验已有主机密钥；端口转发 `127.0.0.1:12222` 必须可用。旧的 `PAPER_MINERU_EXECUTABLE` 不再用于本机解析。

远端工作目录为 `/home/leisaihua/paper-library-parser`，包含 `worker.py`（仓库源文件 `src/paper_library/ingest/remote_worker.py`）、`deployment.json`（机器身份）、`mineru.json`、`models/` 与临时 `jobs/`。上传使用随机任务目录，GPU 任务在本项目内串行执行。客户端每 5 秒发送心跳；SSH 输入关闭或连续 45 秒收不到心跳时，远端停止本次任务。工作器按启动身份回收该任务的子进程（含独立会话中的 MinerU 临时 API 服务），确认退出后再释放 GPU 锁；取消及总超时同样生效。成功和可连接的失败任务会清理暂存目录，断网时可能保留暂存文件，远端任务仍受连接与总时限约束。

PDF 首页文字和内嵌标题也只在远端提取，保存为论文包中的 `pdf-frontmatter.json`；旧论文缺少该文件时，核验仅使用已有 Markdown 等线索，不在本机打开 PDF。`parser-provenance.json` 保存实际解析版本、GPU 和后端。执行 `bash scripts/ops check --network` 可检查远程 CUDA 和模型/检索接口。

MinerU 的资源与超时参数统一放在项目 `.env`。远程解析默认使用 8 页处理窗口和 1 倍混合批量，允许单篇论文解析最多 6 小时。`PAPER_MINERU_IDLE_TIMEOUT_SECONDS=0` 表示不再因为一段时间没有日志而误判解析卡死；任务取消与总时限仍然有效。解析输出保存在 `.paper-library-dev/logs/mineru/`，默认保留最近 10 份，便于定位具体页面或模型阶段的问题。

ACL 论文优先读取官方页面的 `citation_*` 结构化引文字段，取得标题、完整作者、会议、日期和 DOI，再与本地正文独立核对；官方读取暂不可用时才回退到 Tavily。Markdown 后备提取会定位对应论文的标题链接，忽略导航和纠错表单，支持多行作者；标题缺失或不唯一时标为提取不完整，不把导航文字或本地标题冒充官方证据。真实标题与标识符冲突仍阻止自动更新。

对于仅含 ACL/arXiv 编号的文件名，新导入在无法核验时可使用明确的正文标题展示，但核验状态保持未确认，不推测作者、年份或 DOI。服务启动时会根据已核验且与目录一致的书目信息清理过期任务警告，其他阶段结果保留。

书目信息核验会使用 `.env` 中当前配置的模型和 `LLM_TEMPERATURE`，根据标题、原始文件名和首页文字生成首选检索词及备选检索词，再按顺序调用 Tavily。模型仅规划检索词，论文身份仍需与本地标题和官方页面交叉核验。若首页或已有记录有 DOI，先直查 Crossref 官方记录；若文件名或记录有 arXiv、ACL 编号，先直查相应官方页面。直查仍需与解析标题、PDF 标题或作者信息核对；Crossref 暂不可用时继续用 Tavily 检索。匹配到预印本后会继续尝试剩余查询；没有确认正式发表版本时保留预印本信息。模型超时、返回无效内容或未配置时回退到原有标题检索。`PAPER_METADATA_QUERY_LLM_ENABLED=0` 可关闭此功能；`PAPER_METADATA_QUERY_ALTERNATIVES` 默认 3，`PAPER_METADATA_QUERY_TIMEOUT_SECONDS` 默认 45。检索词、模型信息和每轮结果写入核验记录的 `search_plan` 与 `attempts` 字段，手动核验命令保持不变。

检索线索优先级为：解析正文中的论文标题、作者和出版信息优先，PDF 内嵌标题及文件名作为兜底。正文开头有足够文本时，生成检索词的模型不会收到原始文件名、旧目录标题和旧年份/会议字段，以避免无关文件名干扰。`search_plan.input_source` 记录本轮使用的是解析文本还是兜底信息。

已有可信正文或 PDF 标题时，单独匹配文件名不能覆盖这些证据。代码、公式示例中的标题和 DOI 不作为论文身份；官方提取缺少真实标题时不会拿检索标题补造证据。DOI URL 与裸编号统一比较，保留括号等有效字符；arXiv 自身 DOI 与正式发表 DOI 分别核对。已发现的标识冲突也不能通过后续字段不完整的记录绕过。

书目信息核验会结合正文标题、原始文件名和可读取的 PDF 标题进行检索，并检查已有 DOI、arXiv 等标识是否冲突。官方页面标题与本地线索确认一致后，`refresh-metadata` 同步修正 `catalog.tsv` 和 `metadata.json`，必要时修复残缺的正文标题；新导入也使用相同核验逻辑，但暂不改写正文标题。无法确认时保留原信息。每次刷新会在论文包的 `metadata-repairs/` 下保存原文件和包含来源、匹配线索的核验报告，发布失败时回滚已修改的文件。

网页导入与 `scripts/library ingest` 使用同一套解析和原子发布服务；网页自动翻译与 `scripts/translate run` 使用同一翻译缓存和 `.env` 模型配置。`scripts/library ingest` 完成解析、段落修复、可选的信息核验及入库，不会自动执行后续结构校对、翻译和导读。终端处理新论文时，可在入库后依次执行：

```bash
bash scripts/structure run --library <library-id> --paper <paper-id>
bash scripts/translate run --library <library-id> --paper <paper-id>
bash scripts/guide run --library <library-id> --paper <paper-id>
```

将 `<library-id>` 和 `<paper-id>` 替换为实际编号。网页导入由后台串联这些步骤，适合单篇导入与查看持久化进度。

导入页的研究主题下拉框随论文库切换。点击“新增主题”可保存并选中新主题，无需先导入论文；主题保存在各论文库的 `subjects.json`，并与目录中已有的主题合并显示。

## 文本结构与目录校对

导入后的结构校对使用 `.env` 当前模型，在翻译与导读前运行。同一阶段内先判断文本角色，再结合候选目录与前后文复核标题遗漏、误收录和章节层级，无需额外命令。复核支持并发、断点续跑；不能确认的结果保留原判断并记录为待确认项。修正记录保存在结构文件的 `outline_review` 中。旧结构文件仍可读取，重新执行下方命令即可补做目录复核。结构标注保存在每篇论文的 `document-structure.json`，不重写 `article.md`，不改变原有译文标识或出处锚点。新导读使用同一份有效结构标注。仅章节层级变化时，可执行 `scripts/guide reconcile --all`，验证既有引文后同步层级；章节边界变化则需 `scripts/guide run` 重新生成。

```bash
bash scripts/structure run --library whitespace --paper ws-p0003
bash scripts/structure status --library whitespace --paper ws-p0003
bash scripts/structure run --all
```

终端运行时，结构校对使用单行动态进度条显示百分比和检查项计数，每篇结束后保留保存摘要；重定向到文件或管道时不输出动态刷新内容，只保留论文编号、完成摘要和错误信息。进度按已处理分组更新，等待模型响应期间不会自行增长。

重复执行会复用已完成的分组；`--force` 重新生成。`PAPER_STRUCTURE_CHUNK_CHARS` 控制每组原文大小，默认 12000；模型、温度、并发沿用 `.env` 模型配置。超长单块保留原样，不截断后改写正文。

校对区分题目、作者、出版信息、摘要、正文、标题、图注和诗歌等角色。模型只读取解析后的文本，当前不读取图片，也没有利用未保存的 MinerU 页坐标。代码、公式块、表格、列表、含图片或显式硬换行的段落不自动调整。中英文混排、Unicode 和原文内容保留；诗歌标注保留软换行。无法确定时沿用原排版。无标注、标注损坏或原文指纹不一致时页面自动回退；模型失败不阻止后续翻译和导读，任务显示待处理项。

导入任务重试会复用已入库论文，并补查缺失或不确定的段落修复及未核验书目信息；重复上传已有 PDF 也执行这些检查。结构标注中的 `unknown` 会在下一次运行时重新判断对应分组，正常分组继续复用。翻译服务不可用或全部段落失败时，仍尝试生成导读，最终显示“已完成，有待处理项”，可重试。未开启翻译的已入库任务，即使带有警告，也可点击“开始翻译”。

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

合并 Research briefing、正式论文及补充材料的文档，通过标题紧邻的独立 DOI 定位正式正文；书目核验使用正文标题，全文概览排除前置简报、参考文献和补充材料。各部分的章节导读仍保留。只有 `I`、`# Article` 等不足十个字符的片段不能提供有效引文，会明确提示原文信息不足；含其他有效正文的分块继续生成。汇总规则更新可复用已完成的章节缓存。

正文范围按原文位置判断，参考文献即使不在目录中也不会混入概览；Nature 文献放在参考文献之后的 Methods 仍保留。组合文档有多个不同的正文 DOI 候选时，提示无法唯一确定，不根据文件顺序猜测。已有目录与引文均有效时，可运行 `scripts/guide run --library <论文库> --paper <论文编号> --overview-only` 只更新全文概览，保留章节内容，并保存旧导读备份。

论文包的 `reading-guide.json` 保存完整导读、模型及原文版本；`reading-guide-cache/` 保存可续用的章节结果。原文或有效结构标注变动后，网页隐藏过期导读并提示重新生成。失败不覆盖已有导读；批量运行会继续处理其他论文，并最终列出失败论文及具体章节原因。失败后去掉 `--force` 重新执行即可续用已完成章节（`--force` 会清空本轮章节缓存并全部重生成）。`PAPER_GUIDE_CHUNK_CHARS` 控制单次章节文本大小（默认 10000 字符），模型接口、超时、输出长度和并发沿用现有模型配置。

读取导读时同时验证章节覆盖、字段和引文内容，原文版本相同也不直接信任损坏的文件。空章节缓存必须有有效的跳过说明；原文信息不足的提示在长章节整合后仍保留。

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

新译文若遗漏或重复受保护标记、破坏格式或未完整生成，会带校验反馈最多纠正两次，再保存其中完整性较好的版本。修复失败不会覆盖更完整的已有译文。尚未解决的内容缺失在任务中显示“完整性需校对”，不会仅因所有请求完成就清除警告；可用 `retry-warnings` 单独重试。单纯新增数字的阅读提示仍允许任务完成。

网页重试已入库任务时，会补译缺失段落及重试完整性有问题的缓存，保留仅有数字阅读提示的译文。读取缓存会重新计算当前警告，不直接采用历史标志；缺乏完整性检查记录的旧任务不会自动清除警告。完整 URL 也要按原样保留；章节编号由程序补回，不会在纠错时要求模型重复生成编号。

解析文本本身缺少变量时，校正译文会明确标出缺失，并提示对照 PDF；这类源文本提示不要求模型凭空补造变量。

## 测试

```bash
bash scripts/verify
bash scripts/verify --browser
bash scripts/verify --browser --live
scripts/library check
scripts/dev doctor
```

翻译浏览器测试默认连接本机 `18020` 端口，可用 `PAPER_READER_URL` 和 `PAPER_TEST_BROWSER` 覆盖。管理流程测试会启动一次性本地服务，并只写入临时论文库。这些测试使用替代的解析器或模型响应，不等同于真实 PDF 经 MinerU、Tavily、本地模型完成全链路验收；结构复核与导读目前也需结合各自的测试验证。

## 阅读管理与稳定运行

已增加 [全文搜索](http://127.0.0.1:18020/search)、[最近阅读](http://127.0.0.1:18020/reading) 和 [运行状态](http://127.0.0.1:18020/manage/status)。选中原文后，可点击顶部工具栏的荧光笔图标直接保存高亮，无需填写备注；每条标注可展开“添加备注”或“编辑备注”，清空备注并保存仍保留高亮，点击“删除标注”才删除整条记录。“最近阅读”保存最近一次阅读位置，支持继续阅读。个人记录保存在独立数据库中；原文变化后无法可靠定位的记录保留并提示。

`bash scripts/ops` 提供备份、校验、恢复预览、历史版本和运行检查。支持 systemd 用户服务与计划备份；配置、恢复演练和升级操作详见 [运行维护说明](docs/maintenance/reader-operations.md)。


## 代码检查与发布边界

`requirements.lock` 固定验证过的 Python 3.11 应用、测试和构建依赖；MinerU 在解析环境单独安装，不随阅读服务升级其 GPU 依赖。`package-lock.json` 固定浏览器测试依赖。`scripts/verify --browser` 使用临时论文库，不要求模型、真实 PDF 或现有运行数据；`--live` 才检查当前 18020 阅读器和已有论文。

本项目使用独立 Git 仓库，远程地址为 `git@github.com:fight-flowes/paper-library.git`。`.github/workflows/check.yml` 定义 push / pull request 时的 CI 检查；部署前也可执行本地统一检查。`libraries/` 论文数据、`.env`、运行数据库、缓存和备份均不提交；首次克隆请按上面的步骤创建空论文库，再从网页导入 PDF。

布局职责：`reader.css` 保存基础排版和正文样式，`workspace.css` 保存列表、搜索、任务等公共工作区组件，`reading-layout.css` 保存论文页工具栏、右栏和抽屉布局，`reading-guide.css` 保存导读组件。新增样式应修改所属规则，避免持续在末尾叠加覆盖。

阅读页的「更多阅读操作（···）」支持复制普通引用和 BibTeX。引用使用现有书目信息，缺失字段省略；如需更新信息，可先执行论文信息核验。浏览器不允许自动复制时，会显示可手动复制的文本。


### 单用户服务与远端备份

当前阅读器由 systemd 用户服务托管，定时备份已启用。无效目录行会阻止写入，书目更新有跨文件崩溃恢复；坏库和可重建搜索索引的故障与正常论文阅读隔离。`/health/ready` 同时核对正文、目录、数据库及未恢复事务。

`PAPER_BACKUP_REMOTE_DIR` 指定 cicore01 专用备份目录，使用项目现有 SSH 连接；本地备份完成后验证并保存远端副本，默认保留最近七份。离线恢复方法与服务维护见 [运行维护说明](docs/maintenance/reader-operations.md)。
