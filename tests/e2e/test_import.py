"""A4：上传、拖放、改名及同数据目录重启。"""
import io
import secrets

import httpx
from playwright.sync_api import expect
from pypdf import PdfWriter

from backend.server import start_server


def test_import_and_persisted_rename(imported, client, dc_server, screenshot):
    page = imported
    expect(page.get_by_test_id("import-summary")).to_have_text("已导入 1 个文件 · 拆出 2 张图")
    expect(page.get_by_test_id("sheet-row")).to_have_count(2)
    expect(page.get_by_test_id("unreadable-list")).to_contain_text("说明.txt")
    expect(page.get_by_test_id("unreadable-list")).to_contain_text("不支持的格式")
    row = page.get_by_test_id("sheet-row").first
    unit_id = row.get_attribute("data-unit")
    page.get_by_test_id("sheet-rename").first.click()
    page.get_by_test_id("in-sheet-name").fill("人工修正后的图名")
    page.get_by_test_id("in-sheet-name").press("Enter")
    expect(page.get_by_test_id("sheet-rename").first).to_contain_text("人工修正后的图名")
    screenshot(page, "03-imported.png")
    # 独立服务重启，重新打开 Repository，不能依赖浏览器或内存任务表。
    from backend.store import Repository
    with Repository() as repo:
        version = repo.get("drawing_units", unit_id)["version_id"]
    client.patch("/api/settings", json={"monthly_limit_yuan":321,"dimension_tolerance_mm":3})
    dc_server.stop(join_timeout=5)
    restarted = start_server(secrets.token_urlsafe(18))
    try:
        with httpx.Client(base_url=restarted.base_url, trust_env=False,
                          headers={"Authorization": f"Bearer {restarted.token}"}) as other:
            rows = other.get("/api/units", params={"version_id":version}).json()["items"]
            assert rows[0]["name"] == "人工修正后的图名"
            assert other.get("/api/settings").json()["dimension_tolerance_mm"] == 3
    finally:
        restarted.stop()


def test_dragdrop_real_upload(page, rendered_pdf):
    writer = PdfWriter(); writer.add_blank_page(width=100, height=100)
    buffer = io.BytesIO(); writer.write(buffer)
    payload = list(buffer.getvalue())
    page.locator("#drop-zone").evaluate("""(element, bytes) => {
      const transfer = new DataTransfer();
      transfer.items.add(new File([new Uint8Array(bytes)], '拖入自制.pdf', {type:'application/pdf'}));
      element.dispatchEvent(new DragEvent('drop', {bubbles:true, cancelable:true, dataTransfer:transfer}));
    }""", payload)
    expect(page.get_by_test_id("import-summary")).to_have_text("已导入 1 个文件 · 拆出 1 张图", timeout=15000)


def test_folder_picker_and_cancel(page, pdf_files, rendered_pdf):
    with page.expect_file_chooser() as chooser:
        page.get_by_test_id("btn-import").click()
    # 取消不改变空状态。
    chooser.value.set_files([])
    expect(page.locator("body")).to_have_attribute("data-state", "idle")
    with page.expect_file_chooser() as folder:
        page.get_by_test_id("link-import-folder").click()
    from pathlib import Path
    folder.value.set_files(str(Path(pdf_files[0]).parent))
    expect(page.locator("body")).to_have_attribute("data-state", "imported", timeout=15000)
    expect(page.get_by_test_id("sheet-row")).to_have_count(2)


def test_import_rest_guards(client, dc_server):
    assert httpx.get(dc_server.base_url + "/api/units?version_id=x", trust_env=False).status_code == 401
    assert client.post("/api/import", json={"paths":["/private/file"]}).status_code == 422
    assert client.post("/api/import", json={"selection_id":"expired"}).status_code == 422
    assert client.get("/api/import/missing").status_code == 404
    assert client.post("/api/import", files={"files":("../escape.pdf",b"bad")}).status_code == 422
    assert client.patch("/api/units/missing", json={"name":""}).status_code == 422
    assert client.patch("/api/units/missing", json={"name":"图名"}).status_code == 404


def test_missing_poppler_reported(page, pdf_files, monkeypatch):
    from backend.parsers.pdf import PDFParser, PDFError
    def missing(*args):
        raise PDFError("POPPLER_UNAVAILABLE", "缺少 PDF 渲染工具，读不了")
    monkeypatch.setattr(PDFParser, "_run", staticmethod(missing))
    page.get_by_test_id("in-import-files").set_input_files(pdf_files[0])
    expect(page.get_by_test_id("import-summary")).to_have_text("已导入 0 个文件 · 拆出 0 张图", timeout=15000)
    expect(page.get_by_test_id("unreadable-list")).to_contain_text("缺少 PDF 渲染工具，读不了")
    expect(page.get_by_test_id("btn-review")).to_be_disabled()


def test_native_selection_ticket_contract(client, dc_server, pdf_files, rendered_pdf, monkeypatch):
    """原生对话框替身验证可信选择凭证；不宣称真实 WebView/Windows 验收。"""
    import sys
    import time
    from types import SimpleNamespace
    from app.launcher import ImportBridge
    dialogs = []
    def choose(kind, allow_multiple):
        dialogs.append((kind, allow_multiple))
        return [pdf_files[0]]
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(OPEN_DIALOG=1, FOLDER_DIALOG=2))
    application = SimpleNamespace(_window=SimpleNamespace(create_file_dialog=choose), server=dc_server)
    bridge = ImportBridge(application)
    selection = bridge.choose_import()
    assert list(selection) == ["selection_id"]
    assert pdf_files[0] not in str(selection)
    response = client.post("/api/import", json=selection)
    assert response.status_code == 202
    assert client.post("/api/import", json=selection).status_code == 422
    job_id = response.json()["job_id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = client.get(f"/api/import/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            break
        time.sleep(.05)
    assert job["status"] == "completed"
    assert job["result"]["unit_count"] == 2
    application._window.create_file_dialog = lambda *args, **kwargs: None
    assert bridge.choose_import(folder=True) is None
    assert dialogs == [(1, True)]
