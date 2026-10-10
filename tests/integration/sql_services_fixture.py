#!/usr/bin/env python3
"""SOAP protocol fixture, not a live SSRS validation. Exercises the actual PowerShell adapter."""
import argparse, base64, contextlib, http.server, io, json, subprocess, sys, threading
from pathlib import Path
import xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import configbackup as cb
from completeness import Coverage
from collectors.common.compare_snapshots import audit
NS='http://schemas.microsoft.com/sqlserver/reporting/2010/03/01/ReportServer'
class Handler(http.server.BaseHTTPRequestHandler):
    fail=False
    def log_message(self,*args):pass
    def do_POST(self):
        request=ET.fromstring(self.rfile.read(int(self.headers['Content-Length'])))
        operation=list(list(request)[0])[0];method=operation.tag.split('}')[-1]
        params={x.tag.split('}')[-1]:x.text or '' for x in operation}
        content=''
        if method=='ListChildren':
            content='<CatalogItems>'+''.join(f'<CatalogItem><Path>/{x}</Path><TypeName>Report</TypeName><ID>{x}</ID></CatalogItem>' for x in ('healthy','failing'))+'</CatalogItems>'
        elif method=='GetItemDefinition':
            if self.fail and params.get('ItemPath')=='/failing':
                self.send_response(500);self.end_headers();self.wfile.write(b'Simulated denied item');return
            definition='<Report><Description>'+('changed' if self.fail else 'original')+'</Description></Report>'
            content='<Definition>'+base64.b64encode(definition.encode()).decode()+'</Definition>'
        elif method=='ListSchedules':
            content='<Schedules><Schedule><ScheduleID>schedule-1</ScheduleID><Name>Daily</Name><LastRunTime>changing</LastRunTime><Definition><State>keep-me</State></Definition></Schedule></Schedules>'
        elif method=='ListSubscriptions':content='<Subscriptions/>'
        elif method=='GetPolicies':content='<Policies><Policy><GroupUserName>Readers</GroupUserName></Policy></Policies><InheritParent>true</InheritParent>'
        elif method=='GetSystemProperties':content='<Properties><Property><Name>EnableMyReports</Name><Value>true</Value></Property></Properties>'
        elif method=='GetSystemPolicies':content='<Policies/>'
        else:self.send_error(400);return
        body=f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><{method}Response xmlns="{NS}">{content}</{method}Response></s:Body></s:Envelope>'.encode()
        self.send_response(200);self.send_header('Content-Type','text/xml');self.end_headers();self.wfile.write(body)
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pwsh',required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    a.output=a.output.resolve();a.output.mkdir(parents=True,exist_ok=False)
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def collect(label):
        target=a.output/label
        cp=subprocess.run([a.pwsh,'-NoProfile','-File',str(ROOT/'collectors/sqlserver/Collect-SqlServices.ps1'),'-OutputDirectory',str(target),'-ReportServerUri',f'http://127.0.0.1:{server.server_port}/ReportService2010.asmx'],text=True,capture_output=True)
        (a.output/(label+'.log')).write_text(cp.stdout+cp.stderr)
        assert cp.returncode==(6 if Handler.fail else 0),(cp.returncode,cp.stderr,cp.stdout)
        Coverage(target);return target
    def archive(target):
        cfg={'backup':{'root':str(a.output/'archive')},'git':{'repository':str(a.output/'git')},'options':{'log_level':'CRITICAL'},'deletion':{'missing_runs':1},'tasks':[{'name':'services','type':'directory','source':str(target),'destination':'services','collection_manifest':True,'storage':'both'}]}
        engine=cb.BackupEngine(cb.ConfigLoader(Path('unused'))._resolve(cfg))
        with contextlib.redirect_stdout(io.StringIO()):code=engine.run()
        return engine,code
    try:
        first=collect('first');second=collect('second');difference=audit(first,second)
        assert not difference['changed'] and not difference['added'] and not difference['removed'],difference
        engine,code=archive(first);assert code==0
        frozen={k:json.dumps(v,sort_keys=True) for k,v in engine.state.task('services')['files'].items()}
        Handler.fail=True;partial=collect('partial');coverage=Coverage(partial)
        failed=[s['path'] for s in coverage.sections if s['status']=='failed'];assert len(failed)==1
        engine,code=archive(partial);assert code==4
        for k,v in frozen.items():
            if k.startswith('services/'+failed[0]+'/'):
                assert json.dumps(engine.state.task('services')['files'][k],sort_keys=True)==v
                if k.endswith('definition.xml'):assert 'original' in (a.output/'git'/k).read_text()
        assert any('changed' in f.read_text() for f in (a.output/'git/services/ssrs/items').rglob('definition.xml'))
        schedule=next((first/'ssrs/schedules').glob('*.xml')).read_text()
        assert 'LastRunTime' not in schedule and 'keep-me' in schedule
        (a.output/'results.json').write_text(json.dumps({'status':'passed','scope':'simulated SOAP only; no live SSRS server','checks':['actual PowerShell HTTP SOAP requests','definitions and policies','stable two-run bytes','narrow runtime-field removal','one item failure preserves archive and Git','healthy item updates'],'determinism':difference['counts']},indent=2)+'\n')
        print('PASS: SSRS SOAP fixture; live server remains unverified')
    finally:server.shutdown();server.server_close();thread.join()
if __name__=='__main__':main()
