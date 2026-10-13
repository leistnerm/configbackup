import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('repeat',Path(__file__).with_name('component_repeat.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class RepeatTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def write(self,number,value='value',status='complete',scopes=None):
        root=self.root/('component-run-%d'%number);root.mkdir()
        name='windows-components/iis/sites/example/settings';path=root/name/'configuration.json';path.parent.mkdir(parents=True);path.write_text(json.dumps({'value':value}))
        row={'path':name,'status':status,'error':'Denied' if status=='failed' else '', 'files':{name+'/configuration.json':hashlib.sha256(path.read_bytes()).hexdigest()} if status=='complete' else {}}
        (root/'collection-manifest.json').write_text(json.dumps({'schema_version':1,'finalized':True,'sections':[row] if scopes is None else scopes}))
        return root,path
    def test_same_files_pass(self):
        self.write(1);self.write(2);self.assertEqual(m.compare(self.root,['iis'])['status'],'PASS')
    def test_changed_values_have_full_diff_and_review(self):
        self.write(1);self.write(2,'updated');data=m.compare(self.root,['iis']);self.assertEqual(data['status'],'REVIEW');self.assertTrue((self.root/data['scopes'][0]['diff_file']).is_file())
    def test_failed_scope_not_equal_pass(self):
        self.write(1,status='failed');self.write(2,status='failed');self.assertEqual(m.compare(self.root,['iis'])['status'],'INCOMPLETE')
    def test_disappeared_objects_not_assumed_deleted(self):
        self.write(1);self.write(2,scopes=[]);self.assertEqual(m.compare(self.root,['iis'])['status'],'INCOMPLETE')
    def test_missing_requested_component_is_incomplete(self):
        self.write(1);self.write(2);self.assertEqual(m.compare(self.root,['iis','dns'])['status'],'INCOMPLETE')
    def test_modified_certified_file_rejected(self):
        self.write(1);root,path=self.write(2);path.write_text('altered')
        with self.assertRaisesRegex(ValueError,'hash mismatch'):m.compare(self.root,['iis'])
    def test_unsafe_manifest_path_rejected(self):
        root,_=self.write(1,scopes=[{'path':'../../escape','status':'complete','files':{}}])
        with self.assertRaisesRegex(ValueError,'scope path'):m.verified_scopes(root,['iis'])
    def test_unrequested_component_rejected(self):
        root,_=self.write(1,scopes=[{'path':'windows-components/dns/server/settings','status':'failed','files':{}}])
        with self.assertRaisesRegex(ValueError,'scope path'):m.verified_scopes(root,['iis'])
    def test_parent_symlink_rejected(self):
        root,path=self.write(1)
        target=root/'moved';path.parent.rename(target);path.parent.symlink_to(target,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'Unsafe'):m.verified_scopes(root,['iis'])
    def test_duplicate_manifest_rejected(self):
        root,_=self.write(1);p=root/'collection-manifest.json';data=json.loads(p.read_text());data['sections']*=2;p.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError,'Duplicate'):m.verified_scopes(root,['iis'])

    def test_new_components_have_independent_certified_scopes(self):
        wanted=['certificates','dfs','nps','wef','wsus','adcs']
        for number in (1,2):
            root=self.root/('component-run-%d'%number);root.mkdir();sections=[]
            for component in wanted:
                name='windows-components/'+component+'/server/settings'
                path=root/name/'configuration.json';path.parent.mkdir(parents=True);path.write_text('{}\n')
                sections.append({'path':name,'status':'complete','files':{name+'/configuration.json':hashlib.sha256(path.read_bytes()).hexdigest()}})
            (root/'collection-manifest.json').write_text(json.dumps({'schema_version':1,'finalized':True,'sections':sections}))
        data=m.compare(self.root,wanted)
        self.assertEqual(data['status'],'PASS');self.assertEqual(len(data['scopes']),6)
        with self.assertRaisesRegex(ValueError,'scope path'):m.verified_scopes(root,['certificates'])

    def test_capture_forwards_details_to_both_reads_and_keeps_partial_results(self):
        calls=[]
        def execute(args,output,log,env,timeout):
            calls.append((args,timeout))
            number=len(calls)
            self.write(number,status='failed')
            return 6
        data=m.capture(self.root/'release',self.root,['iis'],'fixture-python',{},execute,details=['iis-extended'])
        self.assertEqual(data['status'],'INCOMPLETE');self.assertEqual(len(calls),2)
        for args,timeout in calls:
            self.assertEqual(args[args.index('--component')+1],'iis')
            self.assertEqual(args[args.index('--detail')+1],'iis-extended')
            self.assertEqual(args.count('--detail'),1)
        self.assertTrue((self.root/'component-comparison.json').is_file())

if __name__=='__main__':unittest.main(verbosity=2)
