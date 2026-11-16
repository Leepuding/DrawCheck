# 图纸审查 · DrawCheck

导入一套 PDF 或 DWG 图纸，找出图纸之间不一致的地方，回到原图核对，并导出标注 PDF 和 Excel 问题清单。

[怎么用](#怎么用) · [源码运行](#源码运行) · [构建安装包](#构建安装包) · [接入-agent-平台](#接入-agent-平台)

## 能查什么

面向光伏项目的土建与电气图纸，包含通信与视频监控。适合在交接、会审或换版后检查同一项目内的图纸是否一致。

| 检查 | 可以核对的内容 |
| --- | --- |
| 目录与图签 | 目录中的图号、图名与实际图纸的图签是否一致 |
| 跨图数据 | 有明确对应关系的尺寸、标高、编号、规格或数量是否一致 |
| 图纸引用 | 详图索引、图号引用与实际图纸能否对应 |
| 人工复核 | 查看差异两侧的原图位置，确认影响后续判断的模糊信息 |

结果区分**有差异、待确认、未核验和未完成**。如果缺少可配对的证据，或者有图纸未能读取，零条差异仍只代表已核验范围内没有发现差异。

## 怎么用

1. **导入图纸。** 选择 PDF、DWG、文件夹或 ZIP、RAR、7z 压缩包。导入后可以看图和核对图号，原文件保留原样。
2. **开始审查。** 在设置中配置主模型和月度费用上限，可选第二模型复核。开始前，应用会说明图纸内容会发给哪些模型服务商，确认后才执行。
3. **核对结果。** 打开问题查看两侧原文和位置，处理需要人工确认的条目。可以从历史继续工作，用新图创建修订后重审。
4. **导出。** 生成标注 PDF 与 Excel 问题清单，保留差异来源、人工判断与未完成项。

项目数据存放在本机，模型密钥由操作系统凭据服务保存。Windows 默认数据目录为 `%LOCALAPPDATA%\DrawCheck`，macOS 为 `~/Library/Application Support/DrawCheck`。

## 源码运行

以下命令在项目源码目录中执行。

使用 Python 3.12 或 3.13。安装 Poppler、LibreDWG 和 7-Zip 后，请确保其命令可从 PATH 找到。Windows 桌面窗口还需要 WebView2。

**macOS**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
bash scripts/start.command
```

**Windows PowerShell**

```powershell
py -3.12 -m venv .venv-win
.\.venv-win\Scripts\python.exe -m pip install -r requirements.txt
.\scripts\start.ps1
```

开发预览可以运行 `python -m app.launcher --browser`。用当前虚拟环境执行 `python -m pytest tests/app tests/backend/test_health.py packaging/tests`，可检查启动、健康接口和打包约束。安装 Playwright Chromium 后，还可执行 `python -m pytest tests/e2e` 验证界面。

## 构建安装包

| 目标平台 | 构建入口 | 产物 |
| --- | --- | --- |
| Windows x64 | Windows 上运行 `packaging/build.ps1` | Inno Setup `-win-x64-setup.exe` |
| macOS Apple Silicon | macOS 上运行 `bash packaging/build-macos.sh` | 可拖入应用程序目录的 `.dmg` |

Windows 构建使用离线工具清单。macOS 应用自带 Python 运行时，Poppler、LibreDWG 和 7-Zip 由目标机器另行安装。构建前置条件、输出位置和验收步骤见项目源码内的 `docs/BUILD.md`。

## 接入 Agent 平台

首版可在同一台机器上，通过 **MCP stdio** 或 **本机 HTTP API** 读取用户指定版本的审查结果。支持 MCP stdio 的客户端可以注册工具，支持自定义 HTTP 请求的平台可以配置本机接口。

Agent 可以获取审查状态、结果汇总与问题索引。发起审查、处理待确认和导出报告在桌面应用中完成。使用源码环境启动接口，接入命令、授权方式和调用示例见项目源码内 `docs/API.md` 的「本机 Agent 只读接口 v1」。

## 第三方组件

依赖、字体与外部工具的许可见项目源码内的 `LICENSES.md`。
