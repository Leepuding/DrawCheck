# 安装包第三方材料索引

此目录由 `packaging/build.ps1` 生成并装入 `_internal/third_party/`。原文来源于实际构建环境和经哈希校验的 vendor 输入；本索引不是许可证全文。

| 组件 | 安装包中的材料 | 用途 |
|---|---|---|
| Python | `PYTHON-LICENSE.txt` | 运行时；包含所用 Python 发行版的完整版权/许可文本 |
| Python 运行库与构建环境依赖 | `python-components.json`、`python/<包名>/` | 精确版本与各包原始许可文件；包括随 wheel 附带的第三方许可 |
| Poppler | `vendor/poppler/`、`vendor/sources/` | pdfinfo/pdftoppm、所需 DLL、GPL 原文及匹配源码 |
| LibreDWG | `vendor/libredwg/`、`vendor/sources/` | dwg2dxf/dwgread、所需 DLL、GPL 原文及匹配源码 |
| 7-Zip | `vendor/7zip/`、`vendor/sources/` | 含 LGPL/BSD/unRAR 附加条款；所需 DLL 和匹配源码 |
| WebView2 | `vendor/webview2/` | Microsoft Evergreen x64 离线安装程序及再分发条款 |
| Inter / Material Symbols | `vendor/fonts/` | 与前端实际字体匹配的 OFL / Apache 原文和来源记录 |
| 中文报告字体 | 运行时使用目标机器已有 CJK 字体 | PDF 内嵌所用 TrueType 子集；不把 Windows/Mac 系统字体文件复制到源码、vendor 或安装包；自定义字体由维护人员确认使用权限 |
| 所有 vendor 文件 | `vendor-manifest.json`、`components.json` | 文件 SHA-256、精确版本、来源、实测 CLI 版本、源码说明 |
| Python 环境 | `requirements-win.lock` | 经核对的 Windows 构建环境全部精确版本 |

`LICENSES.md` 保留仓库原有依赖说明。完整依赖以本次生成的文件为准，不能把历史文档版本当成本次二进制的版本。

构建会拒绝缺少许可原文的包。旧式 Python 包若只带 `License: MIT` 等简写，须从其**精确版本**的上游源码取得全文，放到 `vendor/python-licenses/<规范化包名>/`，并在 vendor 清单中补一个带哈希的组件。不得用许可证名称替代原文。

工程检查只能证明这些材料存在且与清单哈希相符；不能自动证明来源可信、许可文本与所有二进制准确匹配、GPL 对应源码完整或全部再分发义务已满足。正式外发前仍需完成规格 D6 的分发审查。
