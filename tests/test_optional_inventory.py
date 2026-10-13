import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'collectors/system'))
import optional_inventory as inv
import optional_catalog as catalog
import windows_components as wc
from completeness import Coverage,publish,section

class OptionalInventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.env=patch.dict(os.environ,{'CONFIGBACKUP_SECTIONS':'{}'});self.env.start()
    def tearDown(self):self.env.stop();self.tmp.cleanup()
    def test_catalog_off_by_default_and_read_only_discovery(self):
        from collector_catalog import catalog as public_catalog
        rows=public_catalog();self.assertEqual(len(rows),18);self.assertTrue(all(r['default'] is False for r in rows))
        seen=[]
        def run(args,**kw):seen.append(args);return subprocess.CompletedProcess(args,0,b'["DnsServer","NPS"]',b'')
        value=catalog.discover(windows=True,which=lambda n:'fake' if n=='powershell.exe' else None,execute=run)
        self.assertEqual(value['windows_modules'],['DnsServer','NPS']);self.assertIn('Get-Module',seen[0][-1]);self.assertNotIn('Import-Module',seen[0][-1])
    def test_bad_discovery_response_cannot_claim_available(self):
        data=catalog.discover(windows=True,which=lambda _:'fake',execute=lambda *a,**k:subprocess.CompletedProcess([],0,b'{"bad":1}',b''))
        self.assertTrue(data['module_probe_error']);self.assertEqual(data['windows_modules'],[])
    def test_nginx_order_and_secrets_and_includes(self):
        raw='http { server {listen 443 ssl;server_name example.test;include /etc/nginx/private/*.conf;proxy_set_header Authorization "Bearer never-save";set $key secret-value;}}'
        parsed=inv.web_configuration('nginx',raw);data=json.dumps(parsed)
        self.assertNotIn('never-save',data);self.assertNotIn('secret-value',data);self.assertIn('/etc/nginx/private/*.conf',data)
        directives=parsed['directives'][0]['children'][0]['children'];self.assertEqual(directives[0]['directive'],'listen');self.assertEqual(directives[1]['directive'],'server_name')
        self.assertFalse(parsed['includes_followed'])
    def test_apache_nested_structure_preserves_precedence_and_hides_unknown_values(self):
        parsed=inv.web_configuration('apache','<VirtualHost *:443>\nServerName example.test\nSetEnv PASSWORD never-save\n<Directory /srv/web>\nRequire all denied\n</Directory>\n</VirtualHost>')
        data=json.dumps(parsed);self.assertNotIn('never-save',data);self.assertEqual(parsed['directives'][0]['children'][2]['children'][0]['arguments'],['all','denied'])
    def test_bad_web_syntax_fails_closed(self):
        for kind,text in [('nginx','http {server_name a;'),('nginx','set ${secret} value;'),('apache','<Directory /srv>\n</VirtualHost>')]:
            with self.assertRaises(ValueError):inv.web_configuration(kind,text)
    def test_credential_urls_are_masked_even_in_allowlisted_directives(self):
        data=inv.web_configuration('nginx','include https://u:never-save@example.test/config?token=also-secret;')
        self.assertNotIn('never-save',json.dumps(data));self.assertNotIn('also-secret',json.dumps(data))
    def test_missing_file_does_not_abort_good_web_scope_and_is_repeatable(self):
        source=self.root/'nginx.conf';source.write_text('http { server { listen 80; } }')
        output=self.root/'out';specs=['nginx='+str(self.root/'missing.conf'),'nginx='+str(source)]
        scopes,failures=inv.collect(output,web_configs=specs);publish(output,scopes)
        self.assertEqual(len(failures),1);self.assertEqual(len(Coverage(output).files),1)
        before={n:(output/n).read_bytes() for n in Coverage(output).files}
        scopes,_=inv.collect(output,web_configs=specs);publish(output,scopes)
        self.assertEqual(before,{n:(output/n).read_bytes() for n in Coverage(output).files})
    def test_private_key_bundle_refused_before_command(self):
        path=self.root/'bundle.pem';path.write_text('-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----')
        with self.assertRaises(ValueError):inv.public_certificates(path,execute=lambda *a,**k:self.fail('key was sent to tool'))
    @unittest.skipUnless(shutil.which('openssl'),'OpenSSL is required for real public certificate fixture')
    def test_real_public_certificate_metadata_is_deterministic_and_telemetry_separate(self):
        public=self.root/'public.pem';private=self.root/'private.pem'
        subprocess.run([shutil.which('openssl'),'req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(private),'-out',str(public),'-days','2','-subj','/CN=configbackup.test','-addext','subjectAltName=DNS:configbackup.test,DNS:alternate.test'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,check=True)
        one=inv.public_certificates(public);two=inv.public_certificates(public)
        self.assertEqual(one,two);self.assertIn('DNS:alternate.test',one[0]['subject_alternative_names'])
        output=self.root/'out';scopes,failures=inv.collect(output,certificates=[str(public)]);publish(output,scopes)
        self.assertFalse(failures);coverage=Coverage(output)
        self.assertTrue((output/'telemetry/certificate-health.json').exists());self.assertTrue(all('telemetry' not in n for n in coverage.files))
        self.assertNotIn('PRIVATE KEY',''.join((output/n).read_text() for n in coverage.files))
    def test_container_allowlist_omits_secret_and_runtime_values(self):
        data={'Id':'a','Name':'web','Created':'volatile','State':{'Status':'running'},'Config':{'Image':'nginx','Env':['TOKEN=never-save'],'Cmd':['password=never-save'],'Labels':{'password':'never-save'}},'HostConfig':{'Privileged':False,'LogConfig':{'Config':{'token':'never-save'}}},'NetworkSettings':{'Networks':{'front':{'IPAddress':'192.0.2.7'}}},'Mounts':[]}
        projected=inv.container_configuration(data);text=json.dumps(projected)
        self.assertNotIn('never-save',text);self.assertNotIn('192.0.2.7',text);self.assertNotIn('volatile',text)
        self.assertEqual(projected['configuration']['environment_names'],['TOKEN'])
    def test_network_volume_driver_option_values_are_never_written(self):
        for kind in ('network','volume'):
            result=inv.container_configuration({'Name':'x','Id':'fixture-id','Driver':'local','Options':{'password':'never-save'},'Labels':{'key':'never-save'}},kind)
            self.assertNotIn('never-save',json.dumps(result));self.assertEqual(result['option_names'],['password'])
    def test_incomplete_inspect_payload_cannot_replace_prior_metadata(self):
        for kind in ('container','network','volume'):
            with self.assertRaises(ValueError):inv.container_configuration({},kind)
        with self.assertRaises(ValueError):inv.container_configuration({'Id':'x','Name':'x'})
    def test_container_failed_object_and_discovery_are_independent(self):
        def execute(args,**kw):
            self.assertFalse(kw['shell'])
            if args[1:3]==['container','ls']:return subprocess.CompletedProcess(args,0,b'aaa\nbbb\n',b'')
            if args[1:3]==['container','inspect']:
                if args[-1]=='aaa':return subprocess.CompletedProcess(args,1,b'',b'secret provider error')
                return subprocess.CompletedProcess(args,0,b'[{"Id":"bbb","Name":"healthy","Config":{},"HostConfig":{}}]',b'')
            if args[1]=='network':return subprocess.CompletedProcess(args,1,b'',b'denied')
            return subprocess.CompletedProcess(args,0,b'',b'')
        output=self.root/'out';scopes,failures=inv.collect(output,engines=['docker'],which=lambda _:'fake',execute=execute);publish(output,scopes)
        self.assertEqual(len(failures),2);self.assertTrue(any(s['status']=='complete' and '/objects/' in s['path'] for s in scopes));self.assertNotIn('secret provider',json.dumps(scopes))
        self.assertFalse(Coverage(output).permits_deletion('containers/docker/container/objects/'+inv.key('aaa')+'/configuration.json'))
    def test_missing_engine_and_disabled_selection_never_succeed(self):
        scopes,failures=inv.collect(self.root/'out',engines=['podman'],which=lambda _:None)
        self.assertEqual(len(failures),3);self.assertTrue(all(s['status']=='failed' for s in scopes))
        with patch.dict(os.environ,{'CONFIGBACKUP_SECTIONS':'{"containers":false}'}):
            scopes,failures=inv.collect(self.root/'disabled',engines=['docker'],execute=lambda *a,**k:self.fail('ran disabled query'))
        self.assertFalse(failures);self.assertTrue(all(s['status']=='disabled' for s in scopes))
    def test_detail_implies_component_and_is_passed_as_one_argument(self):
        calls=[]
        def execute(args,**kw):
            calls.append(args);return subprocess.CompletedProcess(args,0,json.dumps({'schema_version':1,'component':'dns','records':[{'scope':'windows-components/dns/test','status':'complete','data':[]}]}).encode(),b'')
        scopes,failures=wc.collect(self.root,details=['dns-policies'],windows=True,powershell='fake',execute=execute)
        self.assertFalse(failures);self.assertEqual(calls[0][-1],'dns');self.assertEqual(json.loads(calls[0][calls[0].index('-DetailsJson')+1]),['dns-policies'])
    def test_old_optional_scopes_preserved_on_discovery_failure_in_real_archive(self):
        import configbackup as cb
        output=self.root/'source';source=self.root/'nginx.conf';source.write_text('listen 80;')
        config={'backup':{'root':str(self.root/'archive')},'options':{'log_level':'CRITICAL'},'tasks':[{'name':'web','type':'directory','source':str(output),'destination':'web','collection_manifest':True}]}
        def run():
            scopes,_=inv.collect(output,web_configs=['nginx='+str(source)]);publish(output,scopes)
            e=cb.BackupEngine(cb.ConfigLoader(self.root/'cfg')._resolve(config))
            with contextlib.redirect_stdout(io.StringIO()):code=e.run()
            return e,code
        first,code=run();self.assertEqual(code,0);before=json.loads(json.dumps(first.state.task('web')['files']))
        source.unlink();second,code=run();self.assertEqual(code,4);self.assertEqual(second.state.task('web')['files'],before)
    @unittest.skipIf(os.name=='nt','POSIX mock setup for broad core failure')
    def test_system_core_failure_still_runs_independent_optional_adapter(self):
        import collect_system as system
        import storage_inventory,host_details,network_inventory,firewall_inventory
        def fail(root,*args):system.stable_json(root/'os/partial.json',{'uncertain':True});raise PermissionError('core denied')
        def good(root,**kwargs):
            path=root/'certificates/files/good/configuration.json';system.stable_json(path,{'healthy':True});return [section(root,'certificates/files/good')],[]
        output=self.root/'out'
        with patch.object(system,'collect_macos',fail),patch.object(system,'collect_linux',fail),patch.object(storage_inventory,'collect',return_value=([],[])),patch.object(host_details,'collect',return_value=([],[])),patch.object(network_inventory,'collect',return_value=([],[])),patch.object(firewall_inventory,'collect',return_value=([],[])),patch.object(inv,'collect',side_effect=good),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            code=system.main(['--output',str(output),'--public-certificate','fixture'])
        self.assertEqual(code,6);coverage=Coverage(output);self.assertIn('certificates/files/good/configuration.json',coverage.files);self.assertNotIn('os/partial.json',coverage.files)

if __name__=='__main__':unittest.main()
