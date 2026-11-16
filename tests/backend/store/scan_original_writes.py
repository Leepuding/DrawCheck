"""保守扫描写入入口；唯一原图内容创建点必须是私有导入函数的 xb。"""
from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STORE = ROOT / "backend/store"


def scan() -> dict:
    violations, openings, mutations = [], [], []
    banned = {"write_bytes", "write_text", "writelines", "rename", "replace", "unlink",
              "truncate", "copy", "copy2", "copyfile", "move", "remove", "rmdir", "rmtree"}

    class Visitor(ast.NodeVisitor):
        def __init__(self, relative):
            self.relative = relative
            self.function = ""

        def visit_FunctionDef(self, node):
            old = self.function
            self.function = node.name
            self.generic_visit(node)
            self.function = old

        def visit_Call(self, node):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            site = f"{self.relative}:{node.lineno}:{self.function}"
            if name == "open":
                mode = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else None
                allowed = (mode == "rb" or (mode == "xb" and self.relative == "originals.py" and self.function == "_import_copy"))
                openings.append({"位置": site, "模式": mode})
                if not allowed:
                    violations.append(f"未批准的打开模式：{site}")
            if name in banned:
                violations.append(f"未批准的文件变更：{site}:{name}")
            if name in {"write", "chmod"}:
                mutations.append({"位置": site, "操作": name})
                if (self.relative, self.function) not in {("originals.py", "_import_copy"), ("paths.py", "ensure_writable")}:
                    violations.append(f"导入/权限探针以外的写操作：{site}")
            self.generic_visit(node)

    for file in sorted(STORE.rglob("*.py")):
        content = file.read_text(encoding="utf-8")
        Visitor(file.relative_to(STORE).as_posix()).visit(ast.parse(content))
        # 公开 API 不得增加重写原图入口。
        for forbidden in ("def write_original", "def update_original", "def delete_original", "UPDATE original_files", "DELETE FROM original_files"):
            if forbidden in content:
                violations.append(f"发现原图改写接口：{file.name}:{forbidden}")
    return {"扫描目录": "backend/store", "文件打开": openings, "内容/属性变更": mutations,
            "越界项": violations, "说明": "仅首次导入允许 xb 创建；权限探针不写已有文件。静态扫描不替代运行时核验。"}


if __name__ == "__main__":
    result = scan()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(bool(result["越界项"]))
