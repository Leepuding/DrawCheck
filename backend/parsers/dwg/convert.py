"""DXF 完整性门槛：退出码零不等于文件完整；不静默修复残缺转换。"""
from pathlib import Path


def load_complete_dxf(path: Path):
    if not path.is_file() or not path.stat().st_size:
        raise ValueError("DXF 无产出")
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - 256))
        tail = stream.read().replace(b"\r", b"").rstrip()
    if not tail.endswith(b"0\nEOF"):
        raise ValueError("DXF 残缺，缺少 EOF")
    import ezdxf

    try:
        return ezdxf.readfile(path)
    except Exception as exc:
        raise ValueError(f"ezdxf 读取 DXF 失败：{exc}") from exc
