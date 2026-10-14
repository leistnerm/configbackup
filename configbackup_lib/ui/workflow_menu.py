"""Guided entry points for coverage, recovery maintenance and repeat checks."""
import json
from pathlib import Path
import yaml


def wizard(config,path,input_fn=input):
    from configbackup_lib.ui.configure import ask,choice,boolean
    while True:
        action=choice('Recovery and review workflows',['Coverage report','Refresh recovery exports','Check export freshness','Compare saved captures','Repeat managed database collection','Discover configuration includes','Retention and space preview','Inspect interrupted run','Recovery copy retention','Archive integrity and repair','Recovery status by scope','Offline deployment pack','Back'],input_fn)
        if action in (None,'Back'):return
        if action=='Offline deployment pack':
            from configbackup_lib.recovery.deployment_pack import wizard as deployment_wizard
            deployment_wizard(input_fn);continue
        if action in ('Archive integrity and repair','Recovery status by scope'):
            print('Uses saved configuration. Use Ctrl+S/F2 to save edits first.')
            output=ask('Private report directory',input_fn=input_fn)
            if action=='Recovery status by scope':
                from configbackup_lib.recovery.recovery_status import main
                main(['--config',str(path),'--output',output]);continue
            from configbackup_lib.recovery.archive_integrity import main
            selected=choice('Archive integrity',['Bounded scrub','Plan current-version repair','Apply reviewed repair','Back'],input_fn)
            verb={'Bounded scrub':'scrub','Plan current-version repair':'plan-repair','Apply reviewed repair':'repair'}.get(selected)
            if verb:
                args=[verb,'--config',str(path),'--output',output]
                if verb=='repair':args+=['--plan-id',ask('Plan ID from plan-repair',input_fn=input_fn)]
                main(args)
            continue
        if action in ('Retention and space preview','Inspect interrupted run'):
            from configbackup_lib.engine.configbackup import BackupEngine,ConfigLoader,single_instance_lock
            from configbackup_lib.engine.discovery_review import write_report
            engine=BackupEngine(ConfigLoader(Path(path)).load(),dry_run=True)
            try:
                with single_instance_lock(engine.internal_root/'configbackup.lock'):
                    if action=='Retention and space preview':
                        from configbackup_lib.recovery.retention_planning import report
                        result=report(engine,int(ask('Estimated incoming bytes (0 if unknown)',0,input_fn)),int(ask('Estimated recovery export bytes (0 if unknown)',0,input_fn)))
                    else:
                        from configbackup_lib.recovery.journal_recovery import inspect,reconcile
                        from configbackup_lib.recovery.transaction_journal import Journal
                        journal=__import__('importlib').import_module('configbackup_lib.recovery.transaction_journal').for_engine(engine)
                        result=inspect(journal);print(json.dumps(result,indent=2))
                        if result.get('can_reconcile') and boolean('Apply this exact recovery plan now',False,input_fn):result=reconcile(journal,result['plan_id'])
                write_report(result,ask('Private report directory',input_fn=input_fn),action)
            finally:engine._close_logging()
            print(json.dumps(result,indent=2));continue
        if action=='Recovery copy retention':
            from configbackup_lib.recovery.recovery_maintenance import retain
            directory=ask('Managed recovery directory',input_fn=input_fn);keep=int(ask('Verified copies to keep (minimum 1)',3,input_fn))
            password=__import__('getpass').getpass('Recovery passphrase: ') if boolean('Verify encrypted copies',False,input_fn) else None
            print(json.dumps(retain(directory,keep,password=password),indent=2))
            if boolean('Recheck and apply retention now',False,input_fn):print(json.dumps(retain(directory,keep,password=password,apply=True),indent=2))
            continue
        if action=='Discover configuration includes':
            from configbackup_lib.engine.discovery_review import includes,decide
            source=ask('Starting configuration file',input_fn=input_fn);root=ask('Allowed configuration directory',str(Path(source).parent),input_fn)
            report=includes([source],[root]);print(json.dumps(report,indent=2))
            from configbackup_lib.ui.configure_features import select_many
            files=[r['path'] for r in report['candidates'] if r['status']=='available']
            for selected in select_many('Select files to add (unsaved)',files,[],input_fn):decide(config,report,selected,'accepted')
            continue
        if action=='Coverage report':
            from configbackup_lib.engine.discovery_review import coverage,write_report
            from configbackup_lib.engine.schedule_discovery import discover
            data=discover(include_services=True,user_timers=True) if boolean('Read local scheduled jobs and services',True,input_fn) else None
            from configbackup_lib.engine.configbackup import ConfigLoader,BackupEngine
            engine=BackupEngine(ConfigLoader(Path(path)).load(),dry_run=True)
            try:result=coverage(config,path,data,engine.state.data)
            finally:engine._close_logging()
            write_report(result,ask('Private report directory',input_fn=input_fn))
        elif action in ('Refresh recovery exports','Check export freshness'):
            from configbackup_lib.recovery.recovery_maintenance import refresh,freshness
            saved=yaml.safe_load(Path(path).read_text());print('Uses saved configuration; use Ctrl+S/F2 to save edits first.')
            if action=='Refresh recovery exports':
                directory=ask('Managed recovery export directory',input_fn=input_fn);secondary=ask('Optional separate copy directory (blank for none)',input_fn=input_fn)
                password=__import__('getpass').getpass('Recovery passphrase: ') if secondary and boolean('Encrypt the secondary copy',True,input_fn) else None
                result=refresh(saved,path,directory,secondary or None,password=password)
            else:result=freshness(saved,path,ask('Recovery bundle directory',input_fn=input_fn))
        elif action=='Compare saved captures':
            from configbackup_lib.engine.determinism import compare
            result=compare([ask('First collected snapshot directory',input_fn=input_fn),ask('Second collected snapshot directory',input_fn=input_fn)])
        else:
            from configbackup_lib.engine.determinism import repeat
            names=[t['name'] for t in config.get('tasks',[]) if t.get('connection') and t.get('enabled',True)]
            if not names:print('No enabled managed database collectors.');continue
            name=choice('Database collection to repeat in disposable scratch',names,input_fn)
            if not name:continue
            count=int(ask('Number of captures (2..10)',3,input_fn))
            print('This runs the selected read-only collector repeatedly using its saved connection. It may acquire schema locks and consume database resources.')
            if not boolean('Run repeated collection now',False,input_fn):continue
            result=repeat(config,path,name,count)
        print(json.dumps(result,indent=2))
