# 自制测试图纸集（DC-05 · **非金样本**）

> **声明**：本目录全部为工程自制开发用夹具与答案，**不是金样本**，不能替代最终验收（DECISIONS D2）。  
> 不含任何客户样图内容；客户样本仅可通过环境变量 `DRAWCHECK_SAMPLES` 只读参照形式。

## 目录结构

| 路径 | 是否进仓 | 说明 |
|---|---|---|
| `answers.json` | 是 | 开发答案（问题、目录期望、修订汇总、容差边界） |
| `MANIFEST.json` | 是 | 稳定清单（无时间戳；optional DWG 只记路径/partial，不含可变 sha256） |
| `replay/A/index.json` `replay/B/index.json` | 是 | Mock 回放索引 |
| `anomalies/*` | 是 | 极小异常文件（≥8 种） |
| `build/` | **否**（`.gitignore` 的 `build/`） | 生成的 A/B 套 PDF、可选 DWG；用脚本现生成 |
| `schema/` | （校验在代码） | 结构校验见 `tools/fixtures/schema.py` |

## 生成

```bash
# 在仓库根（worktree）
PYTHONPATH=. python -m tools.fixtures --out fixtures/synthetic
# 或
PYTHONPATH=. python -m tools.fixtures.generate --out fixtures/synthetic

# 确定性自检（第6条：PDF + answers/replay/anomalies；不含 MANIFEST 时间戳与 optional DWG）
PYTHONPATH=. python -m tools.fixtures --compare-twice
```

依赖：`reportlab`（确定性 `invariant=1`）、本机 Poppler（`pdftoppm` 150dpi 审查图哈希）、可选 LibreDWG `dxf2dwg`（`DRAWCHECK_LIBREDWG_BIN`）。

文字全部画成**笔画路径**，无字体嵌入、无文字层（pypdf 提取字符数应为 0）。
说明（N1）：reportlab 仍可能注册未使用的 `/Helvetica`；`pdftotext` 每页或仅见换页符 `\f`，验收第 7 条以 pypdf=0 为准。

## 回放格式（供 DC-09 适配）

`replay/<A|B>/index.json`：

- `poppler_version`：生成时 Poppler 版本；渲染哈希随版本变化时用备用键。
- `key_primary`：`review_png_sha256`（与 DC-06 `PDFParser` 150dpi 审查图一致）。
- `key_fallback`：`pdf_sha256:页序`（例 `abc...:1`）。
- `entries[]`：每张图一条，含 `facts[]`：
  - `category` / `text` / `numeric_value` / `unit` / `bbox`（0～1、左上原点）/
    `confidence` / `unclear` / `source`（`看图识别`）。

查找顺序建议：先按审查图 SHA；未命中则按备用键，并**提示** Poppler 可能漂移（测试不得静默通过）。

回放事实与 `answers.json` 两侧值、坐标框应对得上（本单测试会自动比对）。

## A / B 套

- **A 套**：首版问题集（9 类应报/不应报、规则 A 容差边界、模糊字待确认、多图框、无图签、范围外装修图等）。
- **B 套**：按图号与 A 匹配（Q-09）。含仍存在 / 已解决 / 新出现各 ≥1；`S-001` 与 A **逐字节相同**（供 DC-18 未改动图不重复调用）。
- 容差裁定：差值 **≤** 容差不报，**>** 才报；未标单位且无有效卷册声明时，保持未核验。
- 目录不一致：「目录列了但缺图」「有图但目录未列」**都报、每张各一条**，类别「目录与图签不符」。

## 异常文件期望（DC-06）

详见 `MANIFEST.json` → `anomalies`。摘要：

| 文件 | 期望码 |
|---|---|
| corrupt.pdf | PDF_CORRUPT |
| encrypted.pdf | PDF_ENCRYPTED |
| empty.pdf | EMPTY_FILE |
| disguised_dxf.pdf / sample.dxf / sample.docx | UNSUPPORTED_FORMAT |
| path_traversal.zip | UNSAFE_ARCHIVE_PATH |
| nested.zip | NESTED_ARCHIVE |
| password.zip | ARCHIVE_ENCRYPTED |
| fake.dwg | `expect_import_status=pending_parse`；`expect_dc07=UNREADABLE`（非自造 DC-06 枚举码） |

无 7z 时 RAR/7z 例本单未强制提供；若后续补上，缺工具则 skip。

## 给后续单的用法

- **DC-09**：导入 `build/A`，用回放按审查图 SHA 返回事实；目录期望见 `answers.json` → `catalog.A`。
- **DC-11/12**：按 `issues` 中 `expectation` / `category` / `tolerance` 断言应报与不应报。
- **DC-13**：可用同一回放做复核输入。
- **DC-16**：`pending_confirmations` + `ISSUE-BLUR`（`unclear=true`）。
- **DC-18**：导入 B 套；`revision_summary` 与 `unchanged_drawings`。

## 许可

- 全部图纸/答案/回放：**自制**。
- 字形：脚本内简易笔画，非系统字体、非客户描摹。
- reportlab：BSD。
