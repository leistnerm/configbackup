#!/usr/bin/env python3
"""Requires an ISOLATED disposable Linux network namespace; changes only a unique nft table."""
import argparse,json,subprocess,sys,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path[:0]=[str(ROOT),str(ROOT/'collectors/system')]
from firewall_inventory import collect
from completeness import Coverage,publish

def run(args):return subprocess.run(args,check=True,capture_output=True,text=True).stdout

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--confirm-isolated-network-namespace',action='store_true');p.add_argument('--output',type=Path,required=True);a=p.parse_args()
 if not sys.platform.startswith('linux') or not a.confirm_isolated_network_namespace:p.error('Requires explicit disposable network namespace confirmation')
 out=a.output;out.mkdir(parents=True,exist_ok=False);name='cbtest_'+uuid.uuid4().hex[:10];created=False;result={}
 try:
  run(['nft','add','table','inet',name]);created=True
  run(['nft','add','chain','inet',name,'input','{ type filter hook input priority 1000; policy accept; }'])
  run(['nft','add','rule','inet',name,'input','tcp','dport','54321','counter','accept'])
  snapshots=[]
  for n in (1,2):
   if n==2:
    import socket
    with socket.socket() as sock:
     sock.settimeout(.5)
     try:sock.connect(('127.0.0.1',54321))
     except OSError:pass
   (out/f'raw{n}.json').write_text(run(['nft','-j','list','ruleset']))
   root=out/str(n);root.mkdir();scopes,failures=collect(root,'linux');publish(root,scopes);snapshots.append(Coverage(root))
  scope=next(s for s in snapshots[0].sections if s['path']=='firewall/nftables');assert scope['status']=='complete',scope
  assert (out/'1/firewall/nftables/configuration.json').read_bytes()==(out/'2/firewall/nftables/configuration.json').read_bytes()
  assert (out/'raw1.json').read_bytes()!=(out/'raw2.json').read_bytes(),'Fixture traffic did not change counters'
  result={'status':'passed','checks':['real nft rule collection','packet counters changed while certified policy stayed byte-identical'],'other_scopes':[{k:s[k] for k in ('path','status','error')} for s in snapshots[0].sections]}
 except Exception as exc:result={'status':'failed','error':str(exc)};raise
 finally:
  if created:
   try:run(['nft','delete','table','inet',name]);result['cleanup']='passed'
   except Exception as exc:result.update(status='failed',cleanup=str(exc))
  (out/'results.json').write_text(json.dumps(result,indent=2)+'\n')
 print(json.dumps(result,indent=2));return 0 if result['status']=='passed' else 1
if __name__=='__main__':raise SystemExit(main())
