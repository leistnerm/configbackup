"""Keyboard-level session commands, field drafts, save races and grouped navigation."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import yaml
import test_console_ui as console_tests

@unittest.skipUnless(console_tests.AVAILABLE,'Optional console dependencies unavailable')
class GlobalNavigationTests(unittest.TestCase):
    drive=console_tests.ConsoleTests.drive
    def run_editor(self, config, steps, check=None):
        from configbackup_lib.ui.configure import wizard
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.yaml';path.write_text(yaml.safe_dump(config))
            self.path=path;before=path.read_bytes()
            ui=self.drive(lambda ui:wizard(config,path,'schedule',ui),steps)
            result=yaml.safe_load(path.read_text())
            if check:check(path,before)
            return result,ui

    def browser(self):return [('ConfigBackup configuration editor','advanced\r'),('Advanced configuration','current\r')]
    def exit(self,prompt):return [(prompt,'\x1b[21~'),('Session complete','\r')]

    def test_save_active_text_then_save_again_stays_in_nested_menu(self):
        config={'sources':[], 'custom':{'label':'old'}}
        steps=self.browser()+[('Current configuration','custom\r'),('Current configuration/custom','label\r'),('label','\x01new\x0b\x13'),('Current configuration/custom','label\r'),('label','\x01newer\x0b\x1b[12~')]+self.exit('Current configuration/custom')
        result,ui=self.run_editor(config,steps,lambda p,b:self.assertEqual(len(list(p.parent.glob('*.bak'))),2))
        self.assertEqual(result['custom']['label'],'newer');self.assertFalse(ui.dirty)

    def test_validate_and_changes_preserve_unsubmitted_text_and_cursor(self):
        config={'sources':[], 'custom':{'label':'old'}}
        def inspect(ui):
            self.assertEqual(ui.text.text,'draft');self.assertEqual(ui.text.buffer.cursor_position,2)
            return '\x05\x13'
        steps=self.browser()+[('Current configuration','custom\r'),('Current configuration/custom','label\r'),('label','\x01draft\x0b\x1b[D\x1b[D\x1b[D\x1b[13~'),('label','\x1b[14~'),('label',inspect)]+self.exit('Current configuration/custom')
        result,ui=self.run_editor(config,steps)
        self.assertEqual(result['custom']['label'],'draft');self.assertIn('Configuration is valid',ui.log_text)

    def test_invalid_number_never_saves_then_correction_saves(self):
        config={'sources':[], 'custom':{'count':3}}
        def inspect(ui):
            self.assertEqual(yaml.safe_load(self.path.read_text())['custom']['count'],3)
            return '\x01not-number\x0b\x13'
        steps=self.browser()+[('Current configuration','custom\r'),('Current configuration/custom','count\r'),('count',inspect),('count','\x0142\x0b\x13')]+self.exit('Current configuration/custom')
        result,ui=self.run_editor(config,steps)
        self.assertEqual(result['custom']['count'],42);self.assertIn('Save deferred',ui.log_text)

    def test_escape_field_retains_earlier_edits_and_exit_can_be_cancelled(self):
        config={'sources':[], 'custom':{'label':'old'}}
        steps=self.browser()+[('Current configuration','custom\r'),('Current configuration/custom','label\r'),('label','\x01accepted\x0b\r'),('Current configuration/custom','label\r'),('label','unfinished\x1b'),('Advanced configuration','\x1b[21~'),('Discard unsaved configuration changes and unfinished input?','\r'),('Advanced configuration','\x13')]+self.exit('Advanced configuration')
        result,_=self.run_editor(config,steps);self.assertEqual(result['custom']['label'],'accepted')

    def test_external_edit_blocks_save_without_losing_memory(self):
        config={'sources':[], 'custom':{'label':'old'}}
        def change_disk(ui):self.path.write_text('sources: []\nexternal: keep\n');return '\x13'
        steps=self.browser()+[('Current configuration','custom\r'),('Current configuration/custom','label\r'),('label','\x01mine\x0b\r'),('Current configuration/custom',change_disk),('Current configuration/custom','\x1b[21~'),('Discard unsaved configuration changes and unfinished input?','yes\r'),('Session complete','\r')]
        result,ui=self.run_editor(config,steps);self.assertEqual(result['external'],'keep');self.assertEqual(config['custom']['label'],'mine');self.assertIn('changed on disk',ui.log_text)

    def test_partial_new_source_saves_only_completed_items(self):
        config={'sources':[]}
        steps=[('ConfigBackup configuration editor','monitoring\r'),('Monitoring & reports','sources\r'),('Schedule sources','add\r'),('Snapshot directory','/capture\x13'),('An item is still being created','save completed\r'),('Host label','host-a\r'),('Source type','sql\r'),('IANA timezone','\x15UTC\r'),('Schedule sources','\x13')]+self.exit('Schedule sources')
        result,ui=self.run_editor(config,steps,lambda p,b:self.assertEqual(yaml.safe_load(sorted(p.parent.glob('*.bak'))[-1].read_text()),{'sources':[]}))
        self.assertEqual(result['sources'][0]['host'],'host-a')

    def test_undo_redo_from_submenu_preserve_saved_baseline(self):
        config={'sources':[], 'custom':{'label':'old'}}
        steps=self.browser()+[('Current configuration','custom\r'),('Current configuration/custom','label\r'),('label','\x01new\x0b\r'),('Current configuration/custom','\x1b[15~'),('ConfigBackup configuration editor','\x1b[17~'),('ConfigBackup configuration editor','\x13')]+self.exit('ConfigBackup configuration editor')
        result,ui=self.run_editor(config,steps);self.assertEqual(result['custom']['label'],'new');self.assertIn('Undone.',ui.log_text);self.assertIn('Redone.',ui.log_text)

    def test_group_inventory_has_no_duplicate_actions_and_commands_are_separate(self):
        from configbackup_lib.ui.navigation import GROUPS, COMMANDS
        values=[x for items in GROUPS.values() for x in items]
        self.assertEqual(len(values),len(set(values)));self.assertFalse(set(values)&set(COMMANDS))

    def test_boolean_field_save_accepts_selection(self):
        config={'sources':[], 'custom':{'enabled':True}}
        steps=self.browser()+[('Current configuration','custom\r'),('Current configuration/custom','enabled\r'),('enabled','\x1b[B\x13')]+self.exit('Current configuration/custom')
        result,_=self.run_editor(config,steps);self.assertFalse(result['custom']['enabled'])
        # Legacy guided report fields must commit before the global save callback,
        # while ordinary operational confirmations remain protected by another test.
        from configbackup_lib.ui.configure_features import reporting_wizard
        captured=[];report_config={}
        def work(ui):
            ui.command_handler=lambda *args:captured.append(copy.deepcopy(report_config))
            reporting_wizard(report_config,ui)
        self.drive(work,[('Generate the offline HTML review portal','yes\x13'),('Report directory','\x01/private/reports\x0b\x13'),('Display time zone (IANA)','\x03\x03'),('Session complete','\r')])
        self.assertTrue(captured[0]['reporting']['enabled'])
        self.assertEqual(captured[1]['reporting']['output'],'/private/reports')


    def test_save_validation_failure_keeps_edit_and_original_file(self):
        config={'sources':[{'type':'sql','host':'host','path':'/capture','timezone':'UTC'}]}
        steps=self.browser()+[('Current configuration','sources\r'),('Current configuration/sources','\r'),('Current configuration/sources/0','host\r'),('host','\x01\x0b\x13'),('Current configuration/sources/0','\x1b[21~'),('Discard unsaved configuration changes and unfinished input?','yes\r'),('Session complete','\r')]
        result,ui=self.run_editor(config,steps,lambda p,b:self.assertEqual(p.read_bytes(),b))
        self.assertEqual(config['sources'][0]['host'],'');self.assertIn('Save failed; edits retained',ui.log_text)

    def test_save_io_failure_keeps_memory_and_original_file(self):
        config={'sources':[], 'custom':{'label':'old'}}
        steps=self.browser()+[('Current configuration','custom\r'),('Current configuration/custom','label\r'),('label','\x01new\x0b\x13'),('Current configuration/custom','\x1b[21~'),('Discard unsaved configuration changes and unfinished input?','yes\r'),('Session complete','\r')]
        with patch('configbackup_lib.ui.configure.os.replace',side_effect=OSError('write failure')):
            result,ui=self.run_editor(config,steps,lambda p,b:self.assertEqual(p.read_bytes(),b))
        self.assertEqual(config['custom']['label'],'new');self.assertIn('write failure',ui.log_text)

    def test_declined_undo_preserves_text(self):
        config={'sources':[], 'custom':{'label':'old'}}
        def inspect(ui):self.assertEqual(ui.text.text,'draft');return '\x13'
        steps=self.browser()+[('Current configuration','custom\r'),('Current configuration/custom','label\r'),('label','\x01draft\x0b\x1b[15~'),('Discard the current unfinished field/item and return to the main menu?','\r'),('label',inspect)]+self.exit('Current configuration/custom')
        result,_=self.run_editor(config,steps);self.assertEqual(result['custom']['label'],'draft')

    def test_menu_filter_selection_and_log_focus_survive_commands(self):
        config={'sources':[], 'custom':{'label':'old','other':'keep'}}
        def inspect(ui):
            self.assertEqual(ui.search,'oth');self.assertTrue(ui.in_log)
            return '\t\x1b[21~'
        steps=self.browser()+[('Current configuration','custom\r'),('Current configuration/custom','oth\t\x1b[13~'),('Current configuration/custom',inspect),('Session complete','\r')]
        self.run_editor(config,steps)
        from configbackup_lib.ui.configure_features import select_many
        from configbackup_lib.ui.configure import choice
        selected=[];seen=[]
        def work(ui):
            ui.command_handler=lambda *args:seen.append(list(selected))
            selected.extend(select_many('Pages',['A','B'],['A'],ui))
            choice('Continue',['Done'],ui)
        self.drive(work,[('Pages','\x1b[B \x1b[13~'),('Pages','\x13'),('Continue','\r'),('Session complete','\r')])
        self.assertEqual(seen,[[],['A','B']])

    def test_save_at_operational_confirmation_never_approves_action(self):
        from configbackup_lib.ui.configure import boolean
        observed=[]
        def work(ui):
            ui.command_handler=lambda action,mode,draft:observed.append(action)
            observed.append(boolean('Apply operation?',False,ui))
        self.drive(work,[('Apply operation?','\x1b[B\x13'),('Apply operation?','\x1b'),('Session complete','\r')])
        self.assertEqual(observed,['Save',False])

    def test_busy_commands_are_blocked(self):
        import asyncio
        import threading
        from configbackup_lib.ui.console_ui import ConsoleUI
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        invoked=[];release=threading.Event();entered=threading.Event()
        def work(ui):
            ui.command_handler=lambda *args:invoked.append(args)
            entered.set();release.wait(5)
        async def exercise():
            with create_pipe_input() as keys:
                ui=ConsoleUI('fixture.yaml',input=keys,output=DummyOutput())
                task=asyncio.create_task(ui.run_async(work))
                try:
                    for _ in range(100):
                        if entered.is_set():break
                        await asyncio.sleep(.01)
                    keys.send_text('\x13\x1b[21~');await asyncio.sleep(.1)
                    self.assertEqual(invoked,[]);self.assertIn('unavailable',ui.status)
                    release.set()
                    for _ in range(100):
                        if ui.title=='Session complete':break
                        await asyncio.sleep(.01)
                    keys.send_text('\r');await asyncio.wait_for(task,3)
                finally:release.set()
        asyncio.run(exercise())

    def test_finish_new_item_then_save_automatically(self):
        config={'sources':[]}
        steps=[('ConfigBackup configuration editor','monitoring\r'),('Monitoring & reports','sources\r'),('Schedule sources','add\r'),('Snapshot directory','/capture\x13'),('An item is still being created','\r'),('Host label','host-a\r'),('Source type','sql\r'),('IANA timezone','\x15UTC\r')]+self.exit('Schedule sources')
        result,_=self.run_editor(config,steps);self.assertEqual(len(result['sources']),1)

    def test_cancel_new_item_cancels_deferred_save(self):
        config={'sources':[]}
        steps=[('ConfigBackup configuration editor','monitoring\r'),('Monitoring & reports','sources\r'),('Schedule sources','add\r'),('Snapshot directory','/capture\x13'),('An item is still being created','\r'),('Host label','\x1b')]+self.exit('Monitoring & reports')
        result,_=self.run_editor(config,steps,lambda p,b:self.assertEqual(list(p.parent.glob('*.bak')),[]));self.assertEqual(result,{'sources':[]})
