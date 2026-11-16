import threading
import time
from fastapi.testclient import TestClient
from backend.app import create_app

def test_shutdown_does_not_wait_for_inflight_model(monkeypatch,tmp_path):
    entered=threading.Event(); release=threading.Event()
    monkeypatch.setenv('DRAWCHECK_DATA_DIR',str(tmp_path/'data'))
    class SlowJob:
        def __init__(self,*args,**kwargs): self.extras={}
        def start_review(self,version_id,*,run,disclosure_id):
            return {'ok':True,'task_id':'test-task','status':'待开始'}
        def run_task(self,task_id):
            entered.set(); release.wait(5)
    monkeypatch.setattr('backend.jobs.JobService',SlowJob)
    try:
        with TestClient(create_app('local-token')) as client:
            result=client.post('/api/versions/test/tasks',headers={'Authorization':'Bearer local-token'})
            assert result.status_code==200
            assert entered.wait(2)
            started=time.monotonic()
        assert time.monotonic()-started<1
    finally:
        release.set()
