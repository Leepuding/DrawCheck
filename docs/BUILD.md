# 构建 DrawCheck 安装包

在目标平台上构建。Windows 使用 x64 构建环境，macOS 使用 Apple Silicon 构建环境。两个入口都会先启动冻结后的程序做自检，检查应用资源、图纸工具、本机服务和中文 PDF 字体。自检使用临时数据目录。

## Windows x64

需要 Python 3.12 x64、Inno Setup 6.3～6.x 和一份离线 Python wheelhouse。构建机还需要可供 x64 Python 加载的 Microsoft Visual C++ v14 运行库，供 ONNX Runtime 使用。准备同架构的 Poppler、LibreDWG、7-Zip 与 WebView2 Evergreen Standalone Installer，同时整理对应的许可证、源码材料与文件哈希。清单结构见 [vendor.example.json](../packaging/vendor.example.json)。组件交付要求见 [第三方材料说明](../packaging/third_party/SOURCE-MATERIALS.md)、[Microsoft VC++ 运行库](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist)和 [WebView2 分发说明](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution)。

1. 把已核对的文件放入 `packaging/vendor/`。
2. 将示例复制为 `packaging/vendor.local.json`，用真实版本、来源、许可证、对应源码和逐文件 SHA-256 替换示例值。
3. 在仓库根目录运行以下命令，用实际 wheelhouse 路径替换 `C:\wheelhouse`。

```powershell
py -3.12 -m venv .venv-win
.\.venv-win\Scripts\python.exe -m pip install --no-index --find-links C:\wheelhouse -r packaging\requirements-build.txt
.\.venv-win\Scripts\python.exe packaging\preflight.py lock --output packaging\requirements-win.lock
.\packaging\build.ps1 -Version 0.2.0 -CheckOnly
.\packaging\build.ps1 -Version 0.2.0
```

安装器输出为 `packaging/dist/DrawCheck-0.2.0-win-x64-setup.exe`，同目录包含哈希、冻结程序自检和构建记录。构建会重置 `packaging/stage/`、`packaging/build/` 和 `packaging/dist/`，先移出需要归档的产物。

安装器为当前用户安装，缺少 WebView2 时由随包的离线程序补装。程序目录在 `%LOCALAPPDATA%\Programs\DrawCheck`，项目数据在 `%LOCALAPPDATA%\DrawCheck`。覆盖升级前先备份项目数据，卸载默认保留项目。

## macOS Apple Silicon

需要原生 arm64 Python 3.12 或 3.13，以及 Poppler、LibreDWG 和 7-Zip。使用 Homebrew 时可以这样准备。

```bash
brew install poppler libredwg sevenzip
python3 -m venv .venv
.venv/bin/python -m pip install -r packaging/requirements-build.txt
bash packaging/build-macos.sh 0.2.0
```

DMG、SHA-256 和自检报告位于 `packaging/output/macos/`。打开 DMG 后将 `DrawCheck.app` 拖入“应用程序”。应用自带 Python 运行时和桌面界面，目标机器仍需安装上述三个图纸工具。应用会查找 `/opt/homebrew/bin`、`/usr/local/bin` 与当前 PATH。项目数据默认存放在 `~/Library/Application Support/DrawCheck`。

当前入口输出本机测试包。公开分发前应使用 Developer ID 签名并完成 Apple 公证。操作步骤见 [Apple 的 macOS 软件分发文档](https://developer.apple.com/documentation/xcode/packaging-mac-software-for-distribution)。

## 安装包验收

- 在普通用户的目标系统上安装，从桌面或 Finder 启动。核对系统提示，关闭后检查程序及本机服务退出。
- 用合成 PDF、DWG 和压缩包验证导入、原图预览、结果定位和 PDF／Excel 导出，核对原图 SHA-256。
- 用独立测试项目检查异常输入、未核验和部分完成时的结果提示。使用模型时按部署环境配置模型和费用上限。
- 覆盖安装后核对历史项目和人工复核结果，卸载时核对数据保留方式。
