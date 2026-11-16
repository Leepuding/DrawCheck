# DrawCheck 本地数据 API 初版（DC-04）

本单提供 Python store 接口与 SQLite 迁移，未新增或挂载 HTTP 路由。既有 `/api/health` 与鉴权壳保持原样。下述 REST 为后续前端、导入与审查任务的对接约定，不应视作已实现接口。用户可见文案继续以 prototype-v3、SPEC-Q-RESOLVED-v0.2 为准，错误码由后续接口层映射。

## 数据目录与连接

默认根目录：Windows 为 `%LOCALAPPDATA%\DrawCheck\`，macOS 为 `~/Library/Application Support/DrawCheck/`，Linux 为 `${XDG_DATA_HOME:-~/.local/share}/DrawCheck/`。开发设置 `DRAWCHECK_DATA_DIR` 可完全隔离数据。数据库为根目录下 `drawcheck.sqlite3`，与代码、安装目录分离。

```text
<数据根目录>/
  drawcheck.sqlite3
  projects/<项目 UUID>/
    originals/<内容 SHA-256>    # 只读副本，无扩展名；原名存在数据库
    cache/                      # 后续解析、渲染与转换的衍生文件
    exports/                    # 后续导出产物的本地缓存
```

原图路径以 POSIX 相对路径入库，不能包含绝对路径、`..`、反斜杠或盘符；受控子路径拒绝符号链接，包括根目录内的重定向，防止 cache/exports 指向 originals。项目名称不拼入文件系统路径。默认目录的初始化只发生在显式创建 `Repository` 时，导入模块不写生产目录。

Q-07 的最终用户导出目录仍是 `文档\图纸审查\<项目名>\`，由 DC-17 实现。此处 `exports/` 是内部缓存，`record_export` 仅登记缓存，未实现对外保存文件、PDF 或 Excel 生成。

```python
from backend.store import Repository, Evidence, Source, BBox

with Repository() as store:
    project = store.create_project("中文 项目（示例）")
    version = store.create_version(project["id"], "第一版")
    imported = store.import_original(version["id"], "/开发目录/自制图.pdf")
    original = imported["record"]
    unit = store.create_unit(version["id"], original["id"],
                             kind="pdf_page", ordinal=1, name="T0105(二）")
    evidence = Evidence(unit["id"], Source.PDF_TEXT, "2400", BBox(.1, .2, .3, .4))
    fact = store.add_fact(version["id"], evidence, numeric_value="2400", unit="毫米")
```

连接使用外键、短写事务、`synchronous=FULL`，默认等待锁 1 秒，可用 `Repository(root, timeout=...)` 调整。连接由创建它的线程使用；工作线程各建连接，不跨线程共享。用上下文管理器或 `close()` 释放连接。

迁移命令：`python -m backend.store.migrate --data-dir <临时目录>`，或设置环境变量后省略参数。当前 `PRAGMA user_version=1`；重复执行无变更，失败回滚表和版本号，拒绝降级较新的数据库。

## 数据模型与不变量

| 表 | 核心字段与关系 |
|---|---|
| projects | `id, name, created_at` |
| drawing_versions | `id, project_id, sequence, name, created_at`；一个项目多版本，序号项目内唯一 |
| original_files | `id, version_id, project_id, name, relative_path, sha256, size_bytes`；不可改写或删除 |
| drawing_units | `id, version_id, original_id, kind, ordinal, name, drawing_number, metadata`；PDF 页或 DWG 图框，序号从 1 开始 |
| facts | 证据字段、`numeric_value, unit`；原文与数值分别保存，数值使用十进制字符串以避免浮点舍入 |
| associations | `version_id, left_unit_id, right_unit_id, kind, details` |
| issues | `version_id, title`、左右证据、`rule_result, primary_opinion, review_opinion, review_status, review_note, comparison_tag, previous_issue_id` |
| pending_confirmations | 证据字段、`reason, status, confirmed_text`；原始来源保留，人工确认另存，不自动修改事实或重算 |
| review_tasks | `version_id, status, checkpoints, estimated_cost_micros, incomplete, tolerance_snapshot, created_at` |
| model_calls | `version_id, task_id, unit_id, role, provider, model, status, input_tokens, output_tokens, estimated_cost_micros, image_sha256, prompt_version, created_at` |
| exports | `version_id, task_id, format, relative_path, sha256, created_at`；格式仅 pdf/xlsx |
| settings | 单行非密钥 JSON 配置 |

所有 `id` 为不透明 UUID 十六进制字符串；时间使用 UTC ISO 格式。store 返回字典，SQL 中的 `*_json` 列解码为不带 `_json` 后缀的 JSON 对象或值。缺少模型意见存 `null`，不虚构复核结果。任务状态和检查点只记录调用方提交的事实，本单不执行审查、不自动判定结果。

每个事实、待确认项都必须有 `unit_id, source, text, x0, y0, x1, y1`。问题的两侧分别以 `left_` 和 `right_` 为前缀存上述字段，所以涉及图纸由两侧 `unit_id` 明确确定。来源只能是 `看图识别 | PDF文字 | CAD文字 | 用户确认`。坐标原点为**图纸单元左上角**，x 向右、y 向下；均在 `[0,1]`，`x0<=x1, y0<=y1`。输入 PDF/CAD 坐标由后续解析器先转换；DWG 框按裁切后的图纸单元归一化。拒绝 NaN/Infinity、反向框和越界框。SQLite 非空、CHECK 与复合外键再次验证来源、框与所属版本。

问题复核状态：`疑似 | 已确认 | 排除 | 需补充资料`；对比标签独立为 `null | 仍存在 | 已解决 | 新出现`，不能用标签替换复核结论。`previous_issue_id` 须指向同项目更早版本。自动匹配、复核继承与已解决问题生成交给 DC-18，本单仅存取。

项目最新版本以最大 `sequence` 确定。创建新版本即冻结该项目较早版本的数据；SQLite 触发器阻止旧版记录增、改、删，包括绕过 Repository 的直接 SQL。版本与原图记录本身不可修改。没有删除 API。Q-11 的历史项目仍可打开，其最新版本可继续工作；只读限制针对项目内部旧版本，不是所有历史项目。

原图采用内容寻址，复制只使用独占 `xb` 创建，完成后 `fsync`、设只读并核验 SHA-256。源文件只读打开，不更改其权限。导入期间源内容改变会报错，不入库。副本已有时仅核验，不覆盖、修复或重新设权限。同版本同内容返回原记录与 `reused=true`（调用方负责重复提示）；跨版本同项目复用物理副本，返回新记录与 `reused=false`，表示已新增该版本文件关联；跨项目不复用。

`original_path` 返回前核验哈希和只读位。后续引擎必须用 `review_session(task_id)` 包围处理：前后核验全部原图，异常退出也进行后置核验。它不创建实际审查流程，不自动标完成，也不会在哈希错误后自动回滚调用方已写检查点；调用方应捕获错误并记录任务失败，禁止据此显示成功。

## Python 存取入口

| 操作 | 方法 |
|---|---|
| 项目 | `create_project(name)`、`rename_project(id, name)` |
| 版本 | `create_version(project_id, name)` |
| 原图 | `import_original(version_id, source)`、`original_path(original_id)`、`verify_originals(version_id)` |
| 图纸单元 | `create_unit(...)`、`update_unit_directory(unit_id, name=..., drawing_number=...)` |
| 事实 / 关联 | `add_fact(version_id, evidence, numeric_value=..., unit=...)`、`add_association(...)` |
| 问题 | `create_issue(version_id, title, left, right, rule_result=..., ...)`、`update_issue(id, review_status=..., review_note=...)` |
| 待确认项 | `create_pending(version_id, evidence, reason)`、`confirm_pending(id, status=..., confirmed_text=...)` |
| 审查任务 | `create_task(version_id)`、`update_task(id, status=..., checkpoints=..., incomplete=..., estimated_cost_micros=...)`、`review_session(id)` |
| 调用 / 导出 | `record_model_call(...)`、`record_export(task_id, format=..., relative_path=...)` |
| 设置 | `get_settings()`、`save_settings(changes)`；增量更新，校验失败整体不落库 |
| 查询 | `get(table, id)`、`list_records(table, version_id=..., project_id=...)`；表名仅允许固定枚举 |

通用查询返回扁平证据字段，与上表一致。`list_records` 的项目筛选仅适用于 versions/original_files，版本筛选适用于带 version_id 的业务表。版本按序号返回，其余按写入顺序返回；产品列表排序、分页与问题编号留给接口层。本单不提供通用 SQL/任意字段更新接口。

设置字段：

```json
{
  "dimension_tolerance_mm": 1,
  "elevation_tolerance_m": 0.005,
  "monthly_limit_yuan": null,
  "billing_period": "calendar_month",
  "currency": "CNY",
  "cost_warning_ratio": 0.8,
  "primary_model": null,
  "review_model": null
}
```

模型槽位可存 `{provider, model, base_url, credential_ref}`，其中 `credential_ref` 只能是系统凭据库的引用标识，不能放密钥。未知字段（含 `api_key`）拒绝入库。此层不测试模型、不联网、不判定单双模型可用性；Q-03 业务规则交 DC-08。调用方也不得将密钥放入证据、备注等自由文本。

费用统一以整数微元记录（`1 元 = 1,000,000 微元`），避免浮点累加误差；每条模型调用与任务累计费用由后续引擎在自己的事务中协调，本单不会自动汇总。任务创建时拷贝 Q-01 当前两项容差作为快照，后续改设置不会改旧快照。Q-02 上限初始为 `null`，不设默认金额；填值须大于零。费用上限拦截、80% 提醒和月度统计不在本单实现。

## 后续 REST 形状（尚未挂载）

所有 `/api/*` 沿用既有本机令牌鉴权；列表返回 `{"items": [...]}`，记录直接返回对象；创建返回 201，查询和修改返回 200。返回中不包含数据根目录绝对路径、密钥或数据库错误原文。

| HTTP | 形状 | 对应存储操作 |
|---|---|---|
| GET / POST | `/api/projects` | 查询 / 创建项目 |
| PATCH | `/api/projects/{id}` | 项目改名 |
| GET / POST | `/api/projects/{id}/versions` | 查询 / 创建版本 |
| POST | `/api/versions/{id}/imports` | 桌面选取文件后由可信导入层调用 import_original；不允许未验证 HTTP 请求任意指定本机路径 |
| GET | `/api/versions/{id}/units`、`/facts`、`/issues`、`/pending`、`/tasks` | 对应表按版本查询 |
| PATCH | `/api/units/{id}`、`/api/issues/{id}`、`/api/pending/{id}` | 对应明确字段修改 |
| POST | `/api/versions/{id}/tasks` | 后续引擎创建并启动任务，非本单直接启动 |
| GET | `/api/tasks/{id}` | 状态、未完成与检查点；检查点只供内部使用，不展示产品步骤明细 |
| GET / PATCH | `/api/settings` | 设置读写；密钥另走后续凭据服务 |
| POST | `/api/tasks/{id}/exports` | 后续导出层生成并登记两个产物 |

未来 HTTP 错误形状为 `{"error":{"code":"VERSION_READONLY","message":"..."}}`，不返回异常链。store 抛 `StoreError`，其 `.code` 是字符串枚举。建议映射：

| 错误码 | 建议 HTTP | 含义 |
|---|---|---|
| INVALID_DATA / INVALID_PATH | 422 | 字段、坐标、关联或受限路径不合法 |
| NOT_FOUND | 404 | 记录不存在 |
| VERSION_READONLY / ORIGINAL_IMMUTABLE | 409 | 旧版只读或原图/版本记录不可改写 |
| ORIGINAL_CHANGED | 409 | 副本缺失、哈希变化或只读位被修改，须停止处理 |
| DATA_DIR_NOT_WRITABLE | 503 | 数据目录或数据库无法写入 |
| DATABASE_BUSY | 503 | 锁等待超时，允许用户稍后重试 |
| DISK_FULL | 507 | 写入遇到磁盘空间或配额不足 |
| SCHEMA_TOO_NEW | 409 | 数据库来自较新程序，拒绝降级 |
| STORAGE_IO / DATABASE_ERROR | 500 | 其他文件或数据库错误，保留内部异常链以便开发定位 |

## 解析器交接

`backend/parsers/base.py` 仅定义 `Parser` Protocol 和输入/输出数据类。输入包含 original_id、已核验只读路径、预期 SHA-256、cache_dir 与格式；输出包含图纸单元、带来源与归一化框的原文、未完成原因。PDF 页和 DWG 图框序号都从 1 起。协议不提供写原图能力，也不执行解析、联网或格式转换。未来 DC-06/DC-07 必须保证只在 cache_dir 生成衍生物，并自行核对缓存相对路径边界。

## DC-06 已实现的导入 Python 接口

导入目前通过 Python 调用，未新增 HTTP 路由、未改健康检查或鉴权。后续 REST 可映射 `submit → job_id`、`get_job → 任务快照`；只允许本机可信选文件流程提供路径。

```python
from backend.importer import ImportService, ImportLimits
from backend.parsers.pdf import PDFParser

# 省略数据目录时读取 DRAWCHECK_DATA_DIR，默认目录沿用 store。
with ImportService(limits=ImportLimits(max_files=300, max_bytes=2_000_000_000),
                   pdf_parser=PDFParser(page_timeout=60, max_dimension=8192)) as importer:
    job_id = importer.submit(["/本机/自制图.pdf", "/本机/文件夹", "/本机/图纸.zip"],
                             project_name="导入项目", version_name="第一版")
    snapshot = importer.get_job(job_id)  # 可反复轮询；返回深拷贝，不含 Future 对象
    finished = importer.wait(job_id, timeout=600)  # 测试或本机后台调用；勿阻塞 HTTP 主线程
```

默认自动建项目与版本；传 `version_id=最新版本ID` 则追加到该版本，便于同版去重。修订匹配不在本单实现。单个服务内串行导入，每个任务在工作线程中独立开关 Repository。任务表只在内存中，关闭服务会等待在途导入结束；进程退出后查询记录不保留。程序应复用一个服务实例并在退出时 `close()`。未知 job_id 查询抛 `KeyError`，`wait` 超时抛 `concurrent.futures.TimeoutError`，不取消后台任务。

任务快照形状：

```json
{
  "job_id": "不透明标识",
  "status": "completed",
  "phase": "导入已结束",
  "processed_count": 2,
  "total_count": 2,
  "current_file": null,
  "error": null,
  "result": {
    "project_id": "项目标识",
    "version_id": "版本标识",
    "file_count": 2,
    "unit_count": 1,
    "skipped_count": 0,
    "summary": "已导入 2 个文件 · 拆出 1 张图",
    "duplicate_message": "",
    "files": [
      {"name": "自制图.pdf", "original_id": "原图1", "format": "pdf", "sha256": "内容哈希", "status": "imported", "status_label": "已导入", "unit_count": 1},
      {"name": "自制图.dwg", "original_id": "原图2", "format": "dwg", "sha256": "内容哈希", "status": "unreadable", "status_label": "读不了", "unit_count": 0}
    ],
    "drawings": [
      {"unit_id": "单元标识", "original_id": "原图1", "source_original": "自制图.pdf", "ordinal": 1, "drawing_number": "1", "name": "自制图.pdf · 第 1 页", "preview_relative_path": "projects/.../cache/.../preview.png", "review_relative_path": "projects/.../cache/.../review.png", "page_relative_path": "projects/.../cache/.../page.pdf", "width_points": 200, "height_points": 100}
    ],
    "unreadable": [{"name": "自制图.dwg", "code": "DWG_INCOMPLETE", "reason": "未提取出可用图纸", "original_id": "原图2", "page": null, "notice": {"summary": "暂时无法读取这份 DWG。", "notes": [], "action": "请在 CAD 中核对原文件，或请制图方提供同版 PDF。"}}]
  }
}
```

`status` 为 `queued / running / completed / rejected / failed`。`completed` 表示导入任务已遍历结束。各文件的读取情况由 `files.status` 和 `unreadable` 表示。`file_count` 统计本次新增的可用 PDF（至少一页成功）和已归档的 DWG。`unit_count` 统计落库的 PDF 页面与 DWG 图框。`skipped_count` 统计同版本跳过的重复文件。

`unreadable` 行含 `name / code / reason / original_id / page`，后两项可为 null。单页失败填写从 1 开始的页码与对应原图。DWG 导入直接调用本机解析器，保留成功生成的预览及原生尺寸证据。适用的 DWG 行新增 `notice.summary / notes / action` 供界面显示，`reason` 保留技术诊断原文。`GET /api/issues` 的 `comparison_details` 提供缺单位或人工结论未定的底图尺寸对照，并保留两侧文件来源。

限额在新建项目之前检查：普通文件及单层压缩包展开成员合计 ≤300 个文件、≤2,000,000,000 字节，重复和读不了成员也计入输入配额；目录项不计数；压缩包本身单个也不得超过字节上限。全部输入先快照/展开至系统临时目录，避免文件扫描后增长绕过限额，临时内容在完成或异常后删除。超限 `rejected` 且整批不新增项目、原图或单元；快照期间输入大小改变也整批拒绝，以 `INPUT_CHANGED` 提示重新选择。普通 IO/存储错误返回 `failed`，沿用 store 错误码；已入库内容保留在 `result`，不声称整批回滚。`total_count/processed_count` 是候选文件进度，扫描、解包时通过 `phase` 表示阶段，无人为完成百分比。

ZIP 使用标准库；RAR/7z 使用 PATH 中 `7z` 或 `7za`，每个命令默认 60 秒，无工具则该压缩包列入读不了。拒绝危险路径、链接和嵌套包，不使用整包直接落盘命令。文件夹递归跳过点号隐藏项/目录，不跟随符号链接目录；文件链接列入读不了。显式选中的隐藏文件仍按普通文件处理。

PDFParser 实现原有 Parser 协议；不修改 base.py。通过 PDFParseResult/PDFUnit 的附加字段提供逐页失败、审查图和拆页 PDF 路径。解析器路径相对于 `ParseInput.cache_dir`，入库/导入结果转换为相对于数据根目录；衍生物只写 cache_dir。预览 72 dpi、审查 150 dpi，超大页通过 `-scale-to` 把两种图的单边均限制为默认 8192 像素，不放大小页；PDF 原页不缩放。每页两次渲染共用默认 60 秒预算，`pdfinfo` 单独默认 60 秒。Poppler 超时杀死并回收直接子进程，仅该页记失败，后页继续；pypdf 读取与拆页在工作线程内执行，未提供进程级硬超时或内存限额。当前不提取 PDF 文字层，`texts=[]`，后续识别流程需另接。

## DC-07 DWG Python 解析约定

`backend.parsers.dwg.DwgParser.parse(ParseInput)` 保持 `Parser` 协议，返回 `ParseResult`；文件级状态使用 `parse_dwg_file` 返回的 `DwgFileResult`：`status` 为 `成功` / `部分成功` / `读不了`，`reason` 为中文原因，`result.incomplete` 保留缺字体、外参、代理对象、DXF 残缺、JSON 兜底等降级原因。组件缺失统一为「读不了：DWG 解析组件不可用」。`parse_dwg_files` 按输入顺序串行返回状态表。

异步处理器须 `await parse_dwg_file_async(request)`，SHA 校验与进程等待也在线程内执行。未新增 HTTP 路由；供 DC-06/后续 API 接入。转换、JSON/DXF 解析与 matplotlib 渲染均在工作子进程内执行。

单元 `kind="dwg_frame"`；`preview_relative_path` 为相对 `ParseInput.cache_dir` 的 POSIX 路径：`dwg/<原图SHA前16位>/run-<随机标识>/unit-<ordinal>.png`。每次独立缓存，避免失败重试误用旧预览。成功必须有单元及 PNG；JSON 兜底仅提供部分文字/统计时预览为 `None`，状态为部分成功，不作为完整看图预览。文字来源为 `Source.CAD_TEXT`（「CAD文字」），bbox 以单元左上角为原点、范围 0～1；DXF 文字框为近似外包框。JSON 仅对明确模型空间归属的文字保留插入点，按已提取文字范围归一化，原因中注明其定位边界。未核实的文件名/块名不作为图号。

组件目录由 `DRAWCHECK_LIBREDWG_BIN` 指定（同时包含 `dwg2dxf`、`dwgread`），未指定时搜索 PATH；目标版本为 Spike 验证的 0.13.3，未确认版本会记部分成功原因。`DRAWCHECK_DWG_TIMEOUT_SEC` 默认每条转换命令 120 秒；`DRAWCHECK_DWG_WORKER_TIMEOUT_SEC` 默认 `2×转换超时+120` 秒；`DRAWCHECK_DWG_MEMORY_MB` 默认 1536 MB。Linux 尽力使用 `RLIMIT_AS`，Mac/Windows 不宣称具备硬内存上限。只读原图的 SHA 在解析前后复核，缓存路径拒绝符号链接与 originals。

R2 补充：JSON 兜底按 LibreDWG 0.13.3 的 `entmode=2`（模型空间）抽取 TEXT/MTEXT（ownerhandle 指向 `*Model_Space` 作补充），块内与图纸空间文字只计数（`statistics.cad_text_skipped`）并写入原因，不定位。DXF 路径在已有 `TK-*` 图框的布局中，把框外符合 GB/T 14689 标准/加长图幅尺寸的非 `TK-*` 图块（如目录页、A4 说明页）也作为单元输出，非 TK 单元名带「（图框待确认）」，文件级原因用「块名不是常见图框（当前为：…），请核对是不是一张图」；不在任何单元内的文字会计数写入原因。预览缺字体时逐个列出缺失字体名，并用本机通过字形自检的中文字体替代；替代失败写「中文字形无法渲染，预览中文不可读」。内存：`DRAWCHECK_DWG_MEMORY_MB` 默认 1536 MB（本批最大 JSON 约 205 MB 的 D0202 实测需约 1000 MB，960 MB 失败、1024 MB 通过）；超限时原因为「…解析内存超过上限 N MB…」或「被信号 N 终止，崩溃或内存不足；Linux 已施加 N MB 地址空间上限…」，不会为空串。面向用户原因不含 cache 绝对路径与 LibreDWG stderr 长尾；ERROR 码与内存上限数字保留。

## DC-08 设置与模型接口

本单提供 Python 设置/模型层；可选挂载了最小 REST（见下）。密钥不进 store、不进接口返回、不进日志。费用对外一律标「估算」，内部为整数微元（`1 元 = 1_000_000`）。

### Python 入口

```python
from backend.settings import SettingsService, CredentialService
from backend.models import list_presets, OpenAICompatClient, MockModelClient
from backend.store import Repository

with Repository(data_dir) as repo:
    svc = SettingsService(repo, credentials=CredentialService())
    view = svc.get_settings_view()          # 含 model_status / presets
    svc.save_settings({...}, primary_api_key="...", review_api_key="...")
    status = svc.model_status()             # none | single | dual
    pre = svc.precheck_start_review(version_id=...)  # 阻断时不建任务
    test = svc.test_connection(role="primary")       # 或 review
```

`model_status`：`none`（0 个，阻断）/ `single`（1 个，允许开始；供 DC-10 跳过复核、DC-14 告知、DC-17 标「单模型」）/ `dual`（2 个不同模型）。

测试连接用量记入数据目录 `dc08_test_ledger.json`（本单自管台账，**不改** `model_calls` 表）。若需入库，见 NOTES 方案。

### 计划 REST 形状（本单已在 app 挂载最小实现）

| HTTP | 路径 | 说明 |
|---|---|---|
| GET | `/api/settings` | 非密钥设置 + `model_status` + 预设列表 |
| PATCH | `/api/settings` | 增量更新；密钥字段 `primary_api_key` / `review_api_key` 只经凭据服务 |
| POST | `/api/settings/test-connection` | body: `{role, use_image?}` |
| POST | `/api/settings/precheck` | body: `{version_id?}`；开始审查前置检查 |

错误形状：`{"error":{"code":"...","message":"..."}}`。

### 错误码表

| 码 | 场景 |
|---|---|
| `CONNECTION_OK` | 测试连接成功（文案：连接正常） |
| `CREDENTIAL_REQUIRED` | 密钥为空（文案：需先填写） |
| `AUTH_INVALID` | 密钥无效或无权限 |
| `BALANCE_INSUFFICIENT` | 余额不足 |
| `RATE_LIMITED` | 限流 |
| `TIMEOUT` | 超时 / 网络不通 |
| `CERTIFICATE_ERROR` | 证书错误 |
| `VISION_UNSUPPORTED` | 模型不支持看图 |
| `LOCAL_UNREACHABLE` | 本地地址连不上 |
| `BAD_RESPONSE` | 返回格式不对 / 非 JSON |
| `PROXY_ERROR` | 系统代理错误（含 SOCKS 缺依赖） |
| `OTHER` | 其他（界面：「连接失败（错误码：××）」） |
| `LOCAL_URL_REQUIRED` | 本地模型未填地址 |
| `PRIMARY_REQUIRED` | 只配复核、主模型空 |
| `SAME_MODEL` | 主=复核（含跨服务商同一底座） |
| `MONTHLY_LIMIT_REQUIRED` | 费用上限未填 |
| `NO_MODEL` | 开始审查：0 个模型 |
| `LIMIT_REQUIRED` | 开始审查：未设上限 |
| `COST_WARNING` | 已用 ≥80%（每自然月一次；调高上限后重算） |
| `COST_EXCEEDED` | 已到上限，暂停联网审查 |
| `OK` | 前置检查通过 |

单模型告知句（DC-14）：「只配了一个模型，本次结果未经第二个模型复核」。Excel「单模型」标识交 DC-17。

### DC-08 R1/R2 补充（错误码与金额）

- `OTHER_HTTP_<status>`：非专项映射的 HTTP 4xx/5xx，保留状态码便于排查（不含响应体/密钥）。
- `OTHER_NET_<ExceptionClass>`：httpx/网络层异常类型名（去敏）。
- 金额展示（SPEC「DC-08 金额显示格式」已裁定）：后缀「元」、不用「¥」；估算值大于 0 前加「约」（「0.00 元」「不足 0.01 元」不加）；上限不加「约」；2 位小数四舍五入；≥1000 千分位；负数按「0.00 元」并记日志；上限输入框保持用户原样。示例：「本月模型费用估算已用 80%（约 40.00 元 / 上限 50.00 元）」。
- 产品模式默认不读 `env:SILICONFLOW_API_KEY`；仅 `DRAWCHECK_DEV=1` 或 `DRAWCHECK_LIVE_MODEL=1`。
- 预设服务商改 `base_url` 须改选「自定义」并重新填密钥（`PRESET_URL_LOCKED`）。

## DC-09 看图识别与目录

本单提供 Python 识别/目录层；可选挂载了最小 REST（见下）。默认 **Mock/replay**（DC-05）；真调用仅当 `DRAWCHECK_LIVE_MODEL=1`。角色固定 `primary`，禁止同模型双跑冒充复核。样图不进仓；密钥不进返回/日志。

### Python 入口

```python
from backend.extract import ExtractService, PROMPT_VERSION
from backend.catalog import CatalogService, MANUAL_CORRECTION_LABEL
from backend.store import Repository

with Repository(data_dir) as repo:
    extract = ExtractService(repo)  # 默认 Mock
    result = extract.extract_unit(unit_id)          # 或 extract_version(version_id)
    catalog = CatalogService(repo)
    catalog.apply_extract_result(result)            # 写入目录行
    rows = catalog.list_catalog(version_id)         # specialty / drawing_number / name
    catalog.rename(unit_id=unit_id, name="新图名") # 持久化「人工修正」
```

- `prompt_version` 默认 `dc09-extract-v1`（见 `backend.extract.PROMPT_VERSION`）。
- 缓存键：`image_sha256 + prompt_version + role`（role=`primary`）；命中不调用模型。
- 格式错误默认重试 1 次（`format_retries`）；仍失败 → 单元 `extract_status=incomplete`，不得当成功空结果。
- 隐藏把握度阈值默认 `0.55`（不进设置页）；`unclear=true` 或低于阈值 → `pending_confirmations`。
- 人工修正字段：`manual_correction`（bool）+ `manual_correction_label`（展示「人工修正」）；图号图名经 `update_unit_directory` 持久化，标记写入单元 `metadata` 与 `dc09_catalog_state.json`。
- 多图框：一页可产出多条目录行；无图签/模型空列表：保留单元并占位，不静默丢图。
- Q-05②（两模型同处读数不一致）本单不实现复核，仅预留接口字段供 DC-13/16；本单实现① + 隐藏阈值。

### 计划 REST 形状（本单已最小挂载）

| HTTP | 路径 | 说明 |
|---|---|---|
| POST | `/api/extract/unit` | body: `{unit_id, persist?}` → 识别结果 |
| GET | `/api/catalog/{version_id}` | 目录行列表 |
| POST | `/api/catalog/rename` | body: `{unit_id 或 row_id, drawing_number?, name?, specialty?}` |

### 错误码（DC-09）

| 码 | 场景 |
|---|---|
| `EXTRACT_BAD_FORMAT` | 模型返回非约定 JSON |
| `EXTRACT_INCOMPLETE` | 重试后仍失败 / 无图 / 回放未命中 |
| `EXTRACT_NO_IMAGE` | 无审查图 |
| `INVALID_RENAME` | 改名参数不足 |
| `ROW_NOT_FOUND` | 目录行不存在 |

## DC-14 · 导入与目录 REST（增量）

沿用 Cookie/Bearer 鉴权。DC-08 设置路由不变。前端设置只读写真实接口；测试连接只测已保存配置，因此界面会明确提示先保存当前设置，再 `PATCH /api/settings → POST /api/settings/test-connection`，费用上限仍须必填。前端映射 `CREDENTIAL_REQUIRED` 为「请先填写模型密钥」、`LOCAL_URL_REQUIRED` 为「请先填写本地模型地址」。不返回或回显明文密钥。

| 方法 | 路径 | 请求 / 返回 |
|---|---|---|
| POST | `/api/import` | multipart 重复字段 `files` 上传内容，支持相对目录文件名；或 JSON `{selection_id}`（桌面原生选择器登记的一次性凭证）。202 `{job_id}` |
| GET | `/api/import/{job_id}` | `{job_id,status,result,error}`；`result` 沿用 DC-06，含 summary/drawings/unreadable/files；不回传内部阶段或异常路径。任务记录仅在本次进程有效 |
| GET | `/api/units?version_id=…` | `{items:[图纸单元]}`；独立读取 SQLite，重启仍有效 |
| PATCH | `/api/units/{unit_id}` | `{name, drawing_number?}`，name 必填、去首尾空白、最多 500 字；省略 drawing_number 则保留原值；返回更新后的单元 |

HTTP 不接受任意 `paths`；一次性选择凭证只由 `app/launcher.py` 原生对话框登记，使用后失效，新选择会使旧凭证失效。浏览器文件和目录使用隐藏 file input，拖放目录通过目录项读取文件内容上传；临时上传在导入结束后清理，业务导入仍调用已有 ImportService。默认候选文件/字节限额沿用 DC-06；拒绝绝对文件名、目录穿越、重复目标。空选择、非法字段 422；任务或单元不存在 404；旧版本改名 409；存储繁忙 503、磁盘满 507。失败不回传本机绝对路径或数据库异常原文。

`POST /api/settings/precheck` 在告知前与点「开始」后调用，不传 version_id，以免取消告知或模拟进度创建真实审查任务。尊重 NO_MODEL、LIMIT_REQUIRED、COST_EXCEEDED 和 COST_WARNING。进度仅为前端约 3.6 秒计时模拟，完成进入标明占位的结果壳，不持久化真实审查结果；无新增引擎或进度 REST。目录名称、改名通过 Repository 持久化；导入任务 summary/读不了清单不跨进程恢复，历史项目打开留后续任务。

与 DC-09 合并时：本段追加保留；`backend/app.py` 仅新增 DC-14 路由块，DC-09 的识别/目录路由按并集处理并核对路径冲突；不替换 DC-08 路由、设置与模型业务。

## DC-10 审查引擎

本单提供后台审查任务编排（`backend/jobs/`）与最小 REST。规则 A/B 与双模型复核本体留给 DC-11/12/13（步骤插件空壳/跳过）。默认 Mock；真调用仅 `DRAWCHECK_LIVE_MODEL=1`。不改 frontend；结果页文案归 DC-15（W6-Q3=A：标题「审查完成」，本单只出数据层计数字段）。

### Python 入口

```python
from backend.jobs import JobService, get_default_registry, STEPS
from backend.settings import SettingsService
from backend.store import Repository

with Repository(data_dir) as repo:
    jobs = JobService(repo, settings_service=SettingsService(repo))
    probe = jobs.probe_incomplete(version_id)   # Q-10；不自动 resume
    started = jobs.start_review(version_id)     # 防重 / 续审 / 新建
    progress = jobs.get_progress(task_id)       # 一句状态 + 百分比 + 计数
```

步骤枚举：`extract` → `link` → `rules_a` → `rules_b` → `dual_review` → `finalize`。无插件时 link/rules 写 `checkpoint=skipped`。`model_status=single` 时跳过 `dual_review` 且复核角色调用=0；`dual` 进空钩子，禁止同模双跑。插件挂载：`get_default_registry().register(plugin)`（实现 `StepPlugin`：`name` / `uses_model` / `run(ctx)->StepResult`）。

### REST（已挂载）

| HTTP | 路径 | 说明 |
|---|---|---|
| POST | `/api/versions/{version_id}/tasks` | 开始审查 / 续审；同一 version 进程内已在跑 → **HTTP 200** + `already_running=true` + 已有 `task_id` |
| GET | `/api/tasks/{task_id}` | 进度与结果计数（产品形状） |
| GET | `/api/versions/{version_id}/review/resume-probe` | 未完成探测（Q-10）；**不**自动开审 |

### 进度 / 结果响应形状（供 DC-15）

仅下列字段（不得向产品 UI 约定返回步骤名列表或日志墙）：

```json
{
  "task_id": "…",
  "version_id": "…",
  "status": "进行中",
  "message": "正在审查图纸，请稍候",
  "percent": 50,
  "issue_count": 0,
  "incomplete_count": 0,
  "unreadable_count": 0,
  "out_of_scope_count": 0,
  "cost_paused": false,
  "outcome": "running",
  "already_running": false,
  "tolerance_snapshot": {"dimension_tolerance_mm": 1, "elevation_tolerance_m": 0.005}
}
```

稳定计数字段（W6-Q3=A 数据层）：

| 字段 | 含义 |
|---|---|
| `issue_count` | 问题数（issues 表） |
| `incomplete_count` | 未完成数（category=`incomplete`） |
| `unreadable_count` | 读不了数（category=`unreadable`） |
| `out_of_scope_count` | 范围外数（category=`out_of_scope`，原因「不在审查范围」） |
| `cost_paused` | 是否费用暂停；为 true 时 `message` 为 Q-02 定稿句，`status=暂停`，`outcome=cost_paused` |

`outcome`：`pending` / `running` / `completed` / `partial` / `cost_paused` / `failed`。DB `status` 仍为 store CHECK：`待开始|进行中|暂停|已完成|失败`。有未完成/读不了/范围外时 `outcome=partial`（或失败），**禁止**把失败写成「0 问题且成功」。

定稿文案：

- 运行中：`正在审查图纸，请稍候`（Q-06）
- 续审探测：`上次审查未完成，点「开始审查」接着审`（Q-10）
- 费用暂停：`本月费用估算已到上限，审查已暂停。可在设置里调高上限后点「开始审查」继续。`（Q-02）

### 错误码

| 码 | 场景 |
|---|---|
| `NO_MODEL` | 未配模型，不建任务（与 DC-08 一致） |
| `LIMIT_REQUIRED` | 未设费用上限 |
| `COST_EXCEEDED` | 已到上限；新建阻断或续审仍暂停 |
| `NOT_FOUND` | 任务不存在 |
| `SAME_MODEL` | 双模型路径同模（插件拒绝） |
| `STEP_FAILED` / `OTHER` | 步骤或内部错误 |

创建任务时写入容差快照（Q-01）；中途改设置不影响已写入快照。费用超限后**新的模型调用 = 0**，已有结果保留。

## DC-15 · 问题列表与复核（增量）

沿用 Cookie/Bearer 鉴权。本单提供结果页所需的问题查询、复核持久化与单元审查图；**不**实现审查引擎（DC-10）与规则引擎（DC-11）。无真问题时可用 `POST /api/issues/seed-mock` 注入静态 Mock（开发/联调；DC-11 合入后改由引擎落库）。

坐标约定：store 内 bbox 为图纸左上角原点、**0～1 归一化**；前端显示换算为百分比定位（`left=x0*100%` 等）。入库复核状态枚举与 DC-04 一致：`疑似 | 已确认 | 排除 | 需补充资料`。界面按钮文案「确认」对应入库「已确认」（W6-Q1=B）。

| 方法 | 路径 | 请求 / 返回 |
|---|---|---|
| GET | `/api/issues?version_id=` | 可选查询桩：`incomplete_count`、`unreadable_count`、`out_of_scope_count`、`cost_paused`、`revised`。返回 `{version_id,items,pending_count,issue_count,incomplete_count,unreadable_count,out_of_scope_count,cost_paused,revised}`。`items[]` 含 `id,title,review_status,review_note,diff,location,description,sheets,main_unit_id,related_unit_id,main_label,related_label,main_bbox,related_bbox,main_bbox_out_of_range,related_bbox_out_of_range,main_image_url,related_image_url`。排序：非排除按图号/标签升序，**排除置后**。 |
| PATCH | `/api/issues/{id}` | body `{review_status, review_note?, toggle?}`。`review_status` 接受入库字或按钮「确认」。`toggle=true` 且与当前状态相同时改回「疑似」。`review_note` 可空（排除原因本波不弹窗）。返回序列化后的单条问题。 |
| POST | `/api/issues/seed-mock` | body `{version_id, force?}`。若该版本已有问题且未 force，直接返回列表；否则基于现有单元写入若干 Mock 问题。 |
| GET | `/api/units/{unit_id}/image` | 返回单元 `metadata` 中 `review_relative_path` / `preview_relative_path` 指向的 PNG；缺失 404。 |

### 错误码（DC-15）

| 码 | HTTP | 场景 |
|---|---|---|
| `NOT_FOUND` | 404 | 版本/问题/审查图不存在 |
| `INVALID_DATA` | 422 | 缺 version_id、非法复核状态、备注过长、无单元可挂 Mock |
| `VERSION_READONLY` | 409 | 旧版本写复核（经 store） |
| `DATABASE_BUSY` | 503 | 存储繁忙 |

与 DC-10 并行：本单不改 `backend/jobs/`；引擎合入后 GET issues 改读真数据，本段契约字段保持并集。`backend/app.py` 仅追加本单路由块。

## DC-11 · 关联与规则 A（增量）

本单提供自动关联与规则 A（目录与图签、尺寸、标高）。经 DC-10 `StepRegistry` 挂载步骤 `link`、`rules_a`（`uses_model=False`，数值比较不交模型）。默认不新增产品 REST；问题/关联/待确认写入 store，供结果页（DC-15）与待确认（DC-16）消费。

### Python 入口

| 能力 | 入口 |
|---|---|
| 关联 | `backend.linking.LinkingService(store).link_version(version_id)` |
| 规则 A | `backend.rules.RulesAService(store).run(version_id, task_id=…)` |
| 插件 | `get_default_registry()` 已注册 `LinkPlugin` / `RulesAPlugin`；亦可 `reg.register(...)` 覆盖 |

### 关联字段（`associations.details`）

| 字段 | 说明 |
|---|---|
| `basis` / `basis_kind` | 依据说明与类型：`titleblock` / `catalog_ref` / `detail_index` / `shared_number` |
| `confidence` | 把握度 0～1 |

**禁止**仅凭相同数字或相同名称建立关联。同名无法确认同一对象时（W7-Q2=**B**）：**不报** `issues`，写入 **1** 条 `pending_confirmations`，`reason` 定稿为「两处同名，无法确认是否为同一处，请核对」。唯一同名正向例（如夹具 `ISSUE-SAME-NAME-OK`）为 0 疑似 + 0 待确认。

### 问题字段（A7 数据层）

`issues` 初始 `review_status=疑似`。`rule_result` 含：

| 字段 | 说明 |
|---|---|
| `category` | 与夹具对齐：`目录与图签不符` / `目录张数` / `图签编号前缀` / `尺寸` / `标高` |
| `left_value` / `right_value` | 两侧值 |
| `basis` | 判定依据 |
| `fixture_id` | 可选，对照开发答案 id |
| `tolerance_mm` / `tolerance_m` | 可选，实际使用的容差（来自任务快照） |

两侧证据坐标为图纸左上原点、**0～1** 归一化。目录类单侧问题时相关侧可使用占位框。

### 容差与缺单位（Q-01 / Q-05）

- 读取任务 `tolerance_snapshot`（创建时固化）；差值 **≤** 容差不报，**>** 才报。
- 尺寸缺单位 / 看不清：**不报**确定错误，`create_pending`（可复用 DC-09 路径）。

### 与 jobs 挂载

步骤顺序仍为 `extract → link → rules_a → rules_b → dual_review → finalize`。本单替换原 `link` / `rules_a` 的 NoOp；`rules_b` 仍为 NoOp（DC-12）。

与 DC-16 并行：双方若追加本文件 / `app.py` / `requirements.txt` / `LICENSES.md` / `jobs` 装配，合入取文末并集；本单未改 `frontend/`、未建 `backend/pending/`。
## DC-16 · 待你确认（增量）

沿用 Cookie/Bearer 鉴权。本单提供「待你确认」列表、填写保存 / 无法确定、写回事实来源「用户确认」，以及填值后**受影响规则重算钩子**。规则本体由 DC-11 提供；**无 DC-11 时钩子 skip**（`recalc.skipped=true`，`reason=rules_unavailable`），界面与持久化仍可用；合入后联调真规则路径。填值 / 再改**不得**新增看图或复核模型调用（响应含 `model_calls_delta`，须为 0）。

store 状态字：`待确认 | 已确认 | 无法确定`。页签计数仅统计 `待确认`。`已确认` 参与比较；`待确认` / `无法确定` 不参与并计入未完成（`incomplete_pending_count`）。输入 `confirmed_text` maxlength **80**。

W7-Q1=A：已处理行可再点开重新填写（改值或改点「无法确定」）；展开时页签计数**不加回**；保存后按新状态再次走确认写回与重算钩子。不提供单独「修改」按钮。

| 方法 | 路径 | 请求 / 返回 |
|---|---|---|
| GET | `/api/pending?version_id=` | 返回 `{version_id,items,pending_count,incomplete_pending_count}`。`items[]` 含 `id,title,location,reason,text,status,confirmed_text,source,bbox,bbox_out_of_range,main_label,main_image_url,participates_in_comparison,counts_incomplete,processed`。排序：未处理在前，已处理在后。 |
| PATCH | `/api/pending/{id}` | body `{status:"已确认"|"无法确定", confirmed_text?}`。`已确认` 须非空 `confirmed_text`（≤80）。写回 pending；`已确认` 时 `add_fact` 来源=`用户确认`。返回 `{item,items,pending_count,incomplete_pending_count,fact_id,fact_source,recalc,model_calls_before,model_calls_after,model_calls_delta}`，其中 `items` 为重算后的当前待确认列表。 |
| POST | `/api/pending/seed-mock` | body `{version_id, force?}`。无 pending 时注入 DC-05 风格模糊待确认（看不清「2?00」「CAM-0?」）；已有且未 force 则直接列表。 |

### 重算语义 / 与 DC-11 联调

- 钩子尝试 `backend.rules.registry.recalculate_for_pending(repo, version_id=…, pending=…)`（或同包 `recalculate_affected`）。
- 模块或符号缺失 → `recalc={invoked:false, skipped:true, reason:"rules_unavailable", …}`，**不失败请求**。
- DC-11 合入后：真跑受影响规则 A；本单不实现规则文件，不改 linking / jobs 状态机核心。
- 断言：`model_calls_delta == 0`（禁止重新看图/复核）。

### 错误码（DC-16）

| 码 | HTTP | 场景 |
|---|---|---|
| `NOT_FOUND` | 404 | 版本 / pending 不存在 |
| `INVALID_DATA` | 422 | 缺字段、非法状态、空确认内容、超过 80 字、无单元可挂 Mock |
| `VERSION_READONLY` | 409 | 旧版本写入 |
| `DATABASE_BUSY` | 503 | 存储繁忙 |

与 DC-11 并行：共改 `docs/API.md` 文末追加取并集；本单不改 `backend/rules/` 业务文件与对方 worktree。`backend/app.py` 仅追加本单路由块。


### 差异结果与原图区域（2026-11-06）

`GET /api/issues` 的 `items` 包含双侧原文可定位的差异，并保留已排除或已解决的历史记录。`comparison_details` 包含未核验配对和缺少双侧定位证据的历史候选。`comparison_summary` 每类输出 `difference`、`unverified`、`verified` 布尔范围标志。`verification` 提供 `has_effective_comparison`、`zero_difference_message`、`scope_note`。`finding.left/right` 包含 `unit_id`、`drawing_number`、`drawing_name`、`text`、`value`、`bbox`、`image_url`，并通过 `basis` 和 `reason` 说明配对依据和差异原因。

- `GET /api/units/{unit_id}/image-region/info`：原图能力 `kind`（pdf_vector、dwg_vector、raster、unavailable）、宽高、能力说明和尺寸/倍率上限。
- `GET /api/units/{unit_id}/image-region?x0=…&y0=…&x1=…&y1=…&width=…&height=…`：左上原点、归一化全图区域，返回局部 PNG。宽高各至多 2048；非法、非有限、退化或过大的区域返回 422。原图不可用返回 409，繁忙/失败返回 503。使用现有本机认证，不开放任意文件路径参数。
- PDF 从原文件页重绘，扫描仍受原始像素限制；DWG 仅使用与导入预览和图框一致的可验证转制缓存。所有渲染均为衍生缓存，不修改原件。

## 本机 Agent 只读接口 v1

桌面应用中完成导入和审查后，可以授权同机客户端读取一个指定图纸版本。同一个结果接口提供 stdio MCP 与本机 HTTP 两种调用方式。启动时指定的 `--version-id` 是唯一可访问的版本，同一项目的新版本需要重新授权启动。进程关闭即停止提供访问。

在源码目录使用 DrawCheck 的 Python 环境。可在本机终端查看数据库已有的版本 ID，再按项目和版本选择要授权的一项：

```python
from backend.store import Repository

with Repository(read_only=True) as repo:
    for project in repo.list_records("projects"):
        for version in repo.list_records("drawing_versions", project_id=project["id"]):
            print(project["name"], version["sequence"], version["name"], version["id"])
```

两种入口都默认读取桌面数据目录。用 `--data-dir` 可指定其他现有 DrawCheck 数据目录。不存在的数据库无需由 Agent 创建，旧库请先用桌面应用打开。接口用 SQLite 只读连接，每次请求读取同一数据库快照。已启动和等待中的审查可查看状态，尚未汇总的问题行暂不可查询。

### stdio MCP

安装可选依赖 `python -m pip install -r requirements-agent.txt`。在支持 stdio MCP 的本机客户端中配置以下命令与参数，`command`、`--data-dir` 和 `PYTHONPATH` 填实际绝对路径。使用客户端自己的权限设置控制哪些 Agent 可以调用这两个工具。

```json
{
  "command": "/path/to/drawcheck/.venv/bin/python",
  "args": ["-m", "backend.agent_api", "stdio", "--version-id", "<版本ID>", "--data-dir", "/path/to/data"],
  "env": {"PYTHONPATH": "/path/to/drawcheck"}
}
```

Windows 按本仓开发约定使用 `.venv-win\Scripts\python.exe` 的绝对路径。支持 `cwd` 的客户端也可将工作目录设为 DrawCheck 源码目录，由此找到模块。

| 工具 | 输入 | 返回 |
|---|---|---|
| `get_review_summary` | 无 | 审查状态与结果摘要 |
| `list_review_issues` | `limit`、`offset` | 已汇总问题分页 |

### HTTP

HTTP 使用项目既有 Python 依赖。先将独立生成的随机令牌置于 `DRAWCHECK_AGENT_TOKEN` 环境变量，例如用 `secrets.token_urlsafe(32)` 生成。然后在源码目录启动：

```text
python -m backend.agent_api http --version-id <版本ID>
```

固定监听 `127.0.0.1`，默认端口 `8765`，可用 `--port` 修改。使用 `Authorization: Bearer <令牌>`，应从本机客户端的凭据配置读取令牌。接口检查 Host，并拒绝携带浏览器 Origin 的请求。HTTP 令牌与桌面启动令牌独立，仅用于下面两个入口。

| 方法 | 路径 | 返回 |
|---|---|---|
| GET | `/agent/v1/versions/{version_id}/summary` | 审查状态与结果摘要 |
| GET | `/agent/v1/versions/{version_id}/issues?limit=50&offset=0` | 已汇总问题分页 |

错误正文为 `{"error":{"code":"…","message":"…"}}`。`401` 表示需提供正确令牌，`404` 表示版本不可访问或数据不存在，`409 RESULTS_NOT_READY` 表示尚未拿到已汇总的问题，`422` 表示分页参数有误，`503 DATA_UNAVAILABLE` 表示当前数据无法读取。MCP 通过工具错误返回相同的业务错误码。

### 结果口径

- 摘要包含 `version_id`、`task_id`、`outcome`、`percent`、`results_available`、`issue_count`、`pending_count`、`incomplete_count`、`unreadable_count`、`out_of_scope_count`、`cost_paused`、`drawing_count`、`recognized_count`、`has_effective_comparison` 和 `comparison_summary`。`outcome` 为 `not_started`、`pending`、`running`、`completed`、`partial`、`cost_paused` 或 `failed`。尚无汇总时 `issue_count` 为 `null`。
- `comparison_summary` 各类分别返回 `difference` 差异数、`unverified` 未核验数、`verified` 有无有效核验。`incomplete_count` 记录审查未完成，`unverified` 记录未达到核验条件的对照。解读零差异时同时查看 `results_available`、`has_effective_comparison` 及各项覆盖计数。
- 问题条目包含 `id`、`category`、`review_status`、`comparison_tag` 和两侧 `unit_id`。保留已排除与已解决的历史条目，客户端据复核状态及修订标签展示。`limit` 默认 50、最大 100；`offset` 从 0 开始。分页返回 `total` 和 `next_offset`，翻页仍读取当前审查状态；桌面结果发生更改后从第一页刷新。
- 工具返回计数、类别、状态和记录 ID。查看原文、定位和报告，以及导入、开始审查和人工确认，均在桌面应用内操作。即使客户端运行在同一台机器，其模型也可能在云端，请按项目授权范围选择允许接收这些字段的客户端。
