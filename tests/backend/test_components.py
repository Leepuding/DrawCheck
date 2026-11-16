from backend.components import components

def test_component_diagnostics_have_no_paths(monkeypatch):
    monkeypatch.setattr('backend.components.shutil.which',lambda name:None)
    result=components()
    assert not result['available']
    assert {row['name'] for row in result['items']} == {
        'pypdf', 'ezdxf', 'pywebview', 'openpyxl', 'rapidocr', 'onnxruntime', 'pdfplumber',
        'PDF预览', 'PDF读取', 'DWG转换', 'DWG读取', '压缩包'}
    assert all(set(row)=={'name','version','available'} for row in result['items'])
