# 第三方许可证说明（DC-02 骨架）

本文件列出当前开发依赖的许可证。安装包阶段（DC-03/DC-19）会附上完整第三方文本。

| 组件 | 用途 | 许可证 |
|---|---|---|
| Python | 运行时 | PSF |
| FastAPI | 本机 HTTP API | MIT |
| Starlette | FastAPI 底层 | BSD-3-Clause |
| Uvicorn | ASGI 服务器 | BSD-3-Clause |
| pywebview | 桌面窗口壳 | BSD-3-Clause |
| httpx | 测试 HTTP 客户端 | BSD-3-Clause |
| pytest | 测试 | MIT |
| Playwright | 端到端测试 | Apache-2.0 |
| pypdf（本机测试 6.21.4） | PDF 页面信息与拆页 | BSD-3-Clause |
| Poppler（pdftoppm / pdfinfo） | PDF 校验与渲图，仅独立 CLI 子进程 | GPL-2.0-or-later |
| 7-Zip（7z / 7za / 7zz） | RAR / 7z 列表与解包，仅独立 CLI 子进程；可用 DRAWCHECK_7Z 指定路径 | 主体 LGPL-2.1-or-later，部分 BSD，含 unRAR 附加限制 |
| Inter 字体（frontend/assets/fonts） | 界面西文 | [OFL-1.1](docs/third-party/font-inter-LICENSE.txt) |
| Material Symbols Outlined | 图标字体 | [Apache-2.0](docs/third-party/font-material-symbols-LICENSE.txt) |

未引入 Electron、React、Vite。未引入 AGPL（如 PyMuPDF）或商业专有 CAD SDK。  
DC-06 没有捆绑上述外部二进制；缺少 Poppler 或 7-Zip 时，依赖真实工具的测试会自动跳过。7-Zip 的 unRAR 附加限制只影响分发打包，不影响本机 CLI 调用。安装包阶段须核对所选二进制的许可证和源码提供义务，附完整文本及获取说明。独立子进程调用不代替履行分发义务。LibreDWG 不由 DC-06 调用。

## DC-07 DWG 管线新增依赖

| 组件 | 用途 | 许可证 |
|---|---|---|
| ezdxf 1.4.5 | DXF 布局、实体与绘制前端 | MIT |
| matplotlib（本机安装 3.11.2） | PNG 光栅预览 | Matplotlib 许可证（基于 PSF；附带资源另有许可，分发时保留原文） |
| NumPy | ezdxf / matplotlib 数值计算 | BSD-3-Clause；当前 wheel 另含 0BSD、MIT、Zlib、CC0-1.0 等，见随包许可证 |
| contourpy | matplotlib 等值线依赖 | BSD-3-Clause |
| cycler | matplotlib 配色依赖 | BSD-3-Clause |
| fonttools | 字体处理 | MIT |
| kiwisolver | matplotlib 布局依赖 | BSD-3-Clause |
| packaging | 版本解析 | Apache-2.0 OR BSD-2-Clause |
| Pillow | PNG 处理依赖 | MIT-CMU；随包第三方组件保留各自许可证 |
| pyparsing | ezdxf / matplotlib 解析依赖 | MIT |
| python-dateutil | matplotlib 日期依赖 | BSD-3-Clause（本机安装版本 4.0.0） |
| typing_extensions | ezdxf 类型兼容依赖 | PSF-2.0 |
| LibreDWG 0.13.3（外部安装，未随本单下载/分发） | dwg2dxf / dwgread 独立子进程 | GPL-3.0-or-later |

Python 依赖的许可证以本机安装包元数据与随包原文核对；版本和原文定位保存于 `_evidence/dependency-licenses.json`。
LibreDWG 只通过命令行调用，不在主进程链接；子进程方式不免除未来分发时的许可证与对应源码义务，完整文本/源码获取方式由打包单补齐。
本单不引入 ODA、商业 CAD SDK、AGPL 依赖，不附带或下载 CAD 字体文件。

## DC-05 自制测试图纸集新增依赖

| 组件 | 用途 | 许可证 |
|---|---|---|
| reportlab（本机安装 4.5.1） | 确定性 PDF 生成（路径笔画、无文字层） | BSD-3-Clause |

本单夹具全部自制：笔画字形内嵌于 `tools/fixtures/glyphs.py`，不捆绑系统字体、不引入客户样图、不使用 ODA / 商业 CAD SDK / AGPL 组件。可选 DWG 经 ezdxf（已有）+ LibreDWG `dxf2dwg` 子进程生成，仅作开发自检。

## DC-08 模型接入与设置后端新增依赖

| 组件 | 用途 | 许可证 |
|---|---|---|
| keyring（本机安装 25.7.0） | Windows 凭据管理器抽象；Linux 无后端时降级内存替身（仅假密钥） | MIT |
| jaraco.classes / jaraco.context / jaraco.functools | keyring 依赖 | MIT |
| SecretStorage / jeepney | Linux keyring 可选后端 | BSD-3-Clause / MIT |
| Pillow | 真调用自制极小测试图（不发客户样图） | MIT-CMU（随包第三方组件保留各自许可证） |

未新增 OpenAI 官方 SDK（使用既有 httpx）。未引入 AGPL。SiliconFlow 为外部 HTTP 服务，不捆绑。

## DC-09 看图识别与自动目录

本单未新增第三方依赖。复用 DC-08 httpx 适配器与 DC-05 replay 夹具；默认 Mock，不外连。未引入 AGPL / ODA / 商业 CAD SDK。客户样图不进仓库。

## DC-14

浏览器 multipart 上传新增 python-multipart（Apache-2.0）；沿用现有本地字体与前端资源，无新增 CDN。

## DC-10 审查引擎

本单未新增第三方依赖。复用 DC-08 设置/费用门闸、DC-09 ExtractService（默认 Mock）与 DC-04 store 任务表。未引入 AGPL / ODA / 商业 CAD SDK。客户样图不进仓库；密钥不进返回/日志/证据。

## DC-15

本单未新增第三方依赖。结果页复用既有本地字体与前端资源；Mock 问题由运行时注入 store，不引入客户样图、不新增 CDN。
