"""DWG 文件状态、协议适配器与同步/异步解析入口。"""
from .libredwg import DwgOptions, discover_tools
from .parser import DwgParser, parse_dwg_file, parse_dwg_file_async, parse_dwg_files
from .status import COMPONENT_UNAVAILABLE, DwgFileResult, DwgFileStatus

__all__ = ["DwgParser", "DwgOptions", "DwgFileStatus", "DwgFileResult", "COMPONENT_UNAVAILABLE",
           "discover_tools", "parse_dwg_file", "parse_dwg_file_async", "parse_dwg_files"]
