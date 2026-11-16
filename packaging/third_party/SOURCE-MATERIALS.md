# 对应源码与构建材料

安装包将 Poppler、LibreDWG、7-Zip 作为独立命令行工具调用。独立子进程方式不免除再分发义务。

本工程选择**随包提供对应源码**，不以一个可能变更的下载页冒充源码交付。维护人员须为实际选用的每个二进制提供：

1. 匹配版本的完整对应源码归档，包含二进制发行者的修改、补丁。
2. 构建/安装脚本、构建配置、依赖及工具链版本等必要材料；放在 vendor 中并逐文件哈希。
3. 许可证、版权声明和所需附加条款的原文。
4. `vendor-manifest.json` 中的准确来源 URL、版本和 `source_notes`，指明归档及构建说明位置。

安装后在 `_internal/third_party/vendor/sources/` 查阅源码，其他构建材料的位置见 `vendor-manifest.json`。发行记录须保留二进制、对应源码、原始清单、离线 wheels 及其 SHA-256，确保日后仍可取得同一组材料。

上游入口（用于定位来源，不替代匹配归档）：

- [Poppler](https://poppler.freedesktop.org/)
- [GNU LibreDWG](https://www.gnu.org/software/libredwg/)
- [7-Zip](https://www.7-zip.org/)
- [Microsoft WebView2](https://developer.microsoft.com/en-us/microsoft-edge/webview2/)

不得将网上任意预编译包与另一个版本的上游源码拼配后标成已满足义务。本目录没有虚构或预填已完成的源码提供承诺；实际匹配性需由发行审查确认。
