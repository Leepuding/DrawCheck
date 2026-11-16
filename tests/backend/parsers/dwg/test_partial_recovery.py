import json
import hashlib
from dataclasses import asdict
from backend.parsers.base import ParseInput, ParsedUnit
from backend.parsers.dwg import parser
from backend.parsers.dwg.libredwg import LibreDwgTools, CommandResult

def test_timeout_keeps_completed_and_unrendered_frame_inventory(tmp_path, monkeypatch):
    source=tmp_path/'input'/'sample.dwg'; source.parent.mkdir(); source.write_bytes(b'AC1018-dummy')
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(parser,'discover_tools',lambda:LibreDwgTools('dwg2dxf','dwgread'))
    def timed_out(command,work_dir,*args,**kwargs):
        ready=work_dir/'unit-1.png'; ready.write_bytes(b'preview')
        relative=ready.relative_to(tmp_path/'cache').as_posix()
        units=[ParsedUnit('dwg_frame',1,'完成框','',[],relative),ParsedUnit('dwg_frame',2,'未完成框','',[],None)]
        (work_dir/'partial.json').write_text(json.dumps({'units':[asdict(u) for u in units],'incomplete':['另有 1 张图渲染未完成'],'statistics':{'frame_count':2}}))
        return CommandResult(-15,'解析工作进程 超时，已终止子进程')
    monkeypatch.setattr(parser,'run_command',timed_out)
    result=parser.parse_dwg_file(ParseInput('id',source,digest,tmp_path/'cache','dwg'))
    assert len(result.result.units)==2
    assert result.result.units[0].preview_relative_path
    assert result.result.units[1].preview_relative_path is None
    assert any('超时' in row for row in result.result.incomplete)
    assert result.sha256_before==result.sha256_after==digest


def test_invalid_cad_text_is_explicitly_incomplete_and_utf8_safe():
    from backend.parsers.dwg._worker import safe_result
    payload = {"units": [{"name": "图框\udcbf", "texts": [{"text": "2400\udcbf"}]}], "incomplete": [], "statistics": {}}
    safe = safe_result(payload)
    assert safe['units'][0]['texts'][0]['text'] == '2400\ufffd'
    assert '编码无法识别' in safe['incomplete'][0]
    json.dumps(safe, ensure_ascii=False).encode('utf-8')
    assert payload['units'][0]['texts'][0]['text'].endswith('\udcbf')
