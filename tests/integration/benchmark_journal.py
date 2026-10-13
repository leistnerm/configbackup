"""Benchmark durable undo using temporary 1 KiB files; optional first argument is output JSON."""
import json,sys,tempfile,time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import configbackup_lib.recovery.transaction_journal as tj
rows=[]
for n in [100,1000,5000]:
    with tempfile.TemporaryDirectory(prefix='cbjournal-benchmark-') as td:
        root=Path(td);folder=root/'files';folder.mkdir()
        for i in range(n):(folder/str(i)).write_bytes(b'x'*1024)
        j=tj.Journal(root/'journal',[root]);written=[0];original=tj.atomic
        def atomic(path,data):written[0]+=len(data);return original(path,data)
        begin=time.perf_counter()
        with patch.object(tj,'atomic',atomic):
            j.begin()
            for p in sorted(folder.iterdir()):j.remember(p);p.write_bytes(b'changed')
        captured=time.perf_counter()-begin;start=time.perf_counter();result=j.recover();elapsed=time.perf_counter()-start
        assert result['files']==n and all(p.read_bytes()==b'x'*1024 for p in folder.iterdir())
        rows.append({'files':n,'before_bytes':n*1024,'metadata_bytes_written':written[0],'capture_seconds':round(captured,3),'recover_seconds':round(elapsed,3),'restored':True})
Path(sys.argv[1] if len(sys.argv)>1 else 'journal-benchmark.json').write_text(json.dumps({'environment':__import__('platform').platform()+'; local temporary filesystem; latency is environment-specific','rows':rows},indent=2)+'\n')
print(json.dumps(rows))
