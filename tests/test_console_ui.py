"""Real terminal-key dispatch into existing configurator workflows."""
import asyncio
import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest

AVAILABLE=importlib.util.find_spec('prompt_toolkit') is not None

@unittest.skipUnless(AVAILABLE,'Optional console dependencies unavailable')
class ConsoleTests(unittest.TestCase):
    def drive(self,workflow,steps,resize=None):
        self.screens=[]
        from configbackup_lib.ui.console_ui import ConsoleUI
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        async def exercise():
            with create_pipe_input() as keys:
                ui=ConsoleUI('fixture.yaml',input=keys,output=DummyOutput())
                task=asyncio.create_task(ui.run_async(workflow))
                try:
                    for title,send in steps:
                        for _ in range(400):
                            if ui.title==title and ui.pending is not None:break
                            await asyncio.sleep(.005)
                        else:self.fail('Prompt unavailable: '+title+'; current: '+ui.title)
                        await asyncio.sleep(.04)
                        screen=ui.app.renderer.last_rendered_screen
                        if screen:self.screens.append((title,screen))
                        if resize is not None and len(self.screens)==1:
                            from prompt_toolkit.data_structures import Size
                            ui.app.output.get_size=lambda:Size(rows=resize[0],columns=resize[1])
                            ui.app.invalidate();await asyncio.sleep(.08)
                            self.resized_screen=ui.app.renderer.last_rendered_screen
                        keys.send_text(send)
                        await asyncio.sleep(.04)
                    await asyncio.wait_for(task,5)
                    self.assertIsNone(ui.error,str(ui.error))
                    return ui
                finally:
                    if not task.done():
                        if ui.pending is not None:ui.resolve(EOFError())
                        task.cancel()
        return asyncio.run(exercise())

    def test_arrows_filter_checklist_text_and_results(self):
        from configbackup_lib.ui.configure import choice,ask,boolean
        from configbackup_lib.ui.configure_features import select_many
        answers=[]
        def work(ui):
            answers.append(choice('Pick',['Alpha','Beta','Gamma'],ui))
            answers.append(choice('Filter',['One','Two','Three'],ui))
            answers.append(select_many('Pages',['A','B','C'],['A'],ui))
            answers.append(ask('Name','draft',ui))
            answers.append(boolean('Approve',False,ui))
            print('Finished without changing a file.')
        ui=self.drive(work,[('Pick','\x1b[B\r'),('Filter','thr\r'),('Pages','\x1b[B \r'),('Name','\x01final\x0b\r'),('Approve','\r'),('Session complete','\r')])
        self.assertEqual(answers,['Beta','Three',['A','B'],'final',False])
        self.assertIn('Finished without changing',ui.log_text)

    def test_checklist_escape_cancels_changes(self):
        from configbackup_lib.ui.configure_features import select_many
        answers=[]
        self.drive(lambda ui:answers.append(select_many('Pages',['A','B'],['A'],ui)),[('Pages',' \x1b'),('Session complete','\r')])
        self.assertEqual(answers,[['A']])

    def test_actual_wizard_edit_validate_save_and_backup(self):
        from configbackup_lib.ui import configure
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'config.yaml';path.write_text('sources: []\n# preserve comment\n')
            config={'sources':[]}
            def work(ui):configure.wizard(config,path,'schedule',ui)
            self.drive(work,[('ConfigBackup configuration editor','report/analysis\r'),('Settings','\r'),('reports (JSON)','\x01{"enabled": true}\x0b\r'),('ConfigBackup configuration editor','\x1b[13~'),('ConfigBackup configuration editor','\x1b[12~'),('Save configuration','\r'),('Session complete','\r')])
            self.assertTrue(list(path.parent.glob('*.bak')))
            self.assertIn('# preserve comment',path.read_text())
            self.assertIn('enabled: true',path.read_text())

    def test_optional_console_dependency_closure(self):
        from configbackup_lib.recovery.deployment_pack import dependencies
        result=dependencies(['console'])
        self.assertIn('prompt-toolkit',result)
        self.assertIn('wcwidth',result)
        self.assertEqual(result,dependencies(['console']))

    def test_external_operation_suspends_and_returns_to_editor(self):
        from configbackup_lib.ui.configure import choice
        values=[]
        def work(ui):
            values.append(ui.run_external(lambda:42))
            values.append(choice('Continue',['Done'],ui))
        self.drive(work,[('Continue','\r'),('Session complete','\r')])
        self.assertEqual(values,[42,'Done'])

    def test_escape_does_not_approve_confirmation(self):
        from configbackup_lib.ui.configure import boolean
        answers=[]
        self.drive(lambda ui:answers.append(boolean('Publish',True,ui)),[('Publish','\x1b'),('Session complete','\r')])
        self.assertEqual(answers,[False])

    def test_redraw_has_header_menu_and_scrollable_results(self):
        from configbackup_lib.ui.configure import choice
        self.drive(lambda ui:choice('ConfigBackup configuration editor',['Tasks','Validate','Save'],ui),[('ConfigBackup configuration editor','\r'),('Session complete','\r')],resize=(24,100))
        screen=self.screens[0][1]
        lines=[''.join(screen.data_buffer[y][x].char for x in range(80)) for y in range(40)]
        text='\n'.join(lines)
        for label in ('CONFIGBACKUP','Configuration studio','F2 Save','Details & results','Tasks'):self.assertIn(label,text)
        footer=''.join(self.resized_screen.data_buffer[23][x].char for x in range(100))
        self.assertIn('Ctrl+C',footer)

    def test_cancel_does_not_write_and_controls_are_not_rendered(self):
        from configbackup_lib.ui.console_ui import clean
        from configbackup_lib.ui.configure import ask
        self.assertNotIn('\x1b',clean('name\x1b[2J'))
        self.assertNotIn('\x07',clean('name\x07'))
        ui=self.drive(lambda ui:ask('Name',input_fn=ui),[('Name','\x03\x03'),('Session complete','\r')])
        self.assertIn('Cancelled',ui.log_text)


    def test_escape_text_and_boolean_preserve_existing_settings(self):
        from configbackup_lib.ui.editor_forms import EditCancelled,edit_boolean
        from configbackup_lib.ui.configure import ask
        answers=[]
        def work(ui):
            answers.append(edit_boolean('Enabled',True,ui))
            try:ask('Path','original',ui)
            except EditCancelled:answers.append('cancelled')
        self.drive(work,[('Enabled','\x1b'),('Path','changed\x1b'),('Session complete','\r')])
        self.assertEqual(answers,[True,'cancelled'])
