"""可重放的纯本地迁移演示；只生成自制字节并输出可核对证据。"""
import json
import tempfile
from pathlib import Path

from backend.store import BBox, Evidence, Repository, Source, StoreError
from backend.store.db import connect
from backend.store.migrate import migrate


def main():
    with tempfile.TemporaryDirectory(prefix="drawcheck-dc04-") as temp:
        root = Path(temp) / "数据目录"
        connection = connect(root)
        try:
            for index in (1, 2):
                print(f"空库迁移第 {index} 次：版本 {migrate(connection)}")
            print("表：", ", ".join(row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")))
            print("外键检查：", list(connection.execute("PRAGMA foreign_key_check")))
        finally:
            connection.close()
        source = Path(temp) / "中文 空格 T0105(二）.pdf"
        source.write_bytes(b"%PDF-TEST-ONLY\n2400\n")
        with Repository(root) as store:
            project = store.create_project("演示 项目（二）")
            version = store.create_version(project["id"], "A")
            imported = store.import_original(version["id"], source)["record"]
            unit = store.create_unit(version["id"], imported["id"], kind="pdf_page", ordinal=1, name=source.stem)
            evidence = Evidence(unit["id"], Source.PDF_TEXT, "2400", BBox(.1, .2, .3, .4))
            problem = store.create_issue(version["id"], "模拟问题", evidence, evidence, rule_result={"模拟": True})
            task = store.create_task(version["id"])
            before = store.verify_originals(version["id"])
            with store.review_session(task["id"]):
                store.update_task(task["id"], status="已完成", checkpoints={"本地模拟": "完成"},
                                  incomplete=[], estimated_cost_micros=0)
            after = store.verify_originals(version["id"])
            print("原图文件名：", imported["name"])
            print("入库相对路径：", imported["relative_path"])
            print("只读权限位：", oct(store.original_path(imported["id"]).stat().st_mode & 0o777))
            print("审查前 SHA-256：", next(iter(before.values())))
            print("审查后 SHA-256：", next(iter(after.values())))
            print("审查前后相同：", before == after)
            print("同内容再次导入跳过：", store.import_original(version["id"], source)["reused"])
            store.create_version(project["id"], "B")
            try:
                store.update_issue(problem["id"], review_status="已确认")
            except StoreError as exc:
                print("更新旧版问题错误码：", exc.code.value)
            print("旧版问题保留：", store.get("issues", problem["id"])["review_status"])
            print("设置默认值：", json.dumps(store.get_settings(), ensure_ascii=False))


if __name__ == "__main__":
    main()
