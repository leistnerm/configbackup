"""Full-screen adapter for the configurator's existing synchronous workflows."""
from __future__ import annotations
import asyncio
import concurrent.futures
import contextlib
import io
import re
from pathlib import Path

from prompt_toolkit import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, VSplit, Window, Layout, ConditionalContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea, Frame


def clean(value):
    # Configuration names/provider diagnostics must never emit terminal controls.
    return re.sub(r'[\x00-\x08\x0b-\x1f\x7f-\x9f]', '', str(value))


class ScreenLog(io.TextIOBase):
    def __init__(self, screen):self.screen=screen
    def write(self, value):
        self.screen.loop.call_soon_threadsafe(self.screen.append_log, clean(value))
        return len(value)
    def flush(self):pass


class ConsoleUI:
    console_ui = True
    def __init__(self, path, *, input=None, output=None):
        self.path=Path(path)
        self.mode='busy';self.title='Opening configuration';self.items=[];self.selected=set()
        self.index=0;self.search='';self.history={};self.pending=None;self.loop=None
        self.command_handler=None;self.checkpoint=None;self.command_active=False;self.queued_command=None;self.draft_active=False;self.field_active=False;self.save_after_draft=False
        self.log_text='';self.confirming=False;self.status='Ready';self.error=None;self.closed=False;self.dirty=False
        self.text=TextArea(multiline=False, wrap_lines=False, style='class:input')
        self.log=TextArea(text='Select a configuration area to begin.\nChanges are written only when you use Save.',read_only=True,scrollbar=True,wrap_lines=True,style='class:log')
        self.menu_control=FormattedTextControl(self.menu_text,focusable=True,get_cursor_position=lambda:Point(0,self.index))
        self.menu=Window(self.menu_control,right_margins=[ScrollbarMargin()],wrap_lines=False)
        active=Condition(lambda:self.mode in ('menu','multi'))
        field=Condition(lambda:self.mode=='text')
        left=HSplit([
            Window(FormattedTextControl(lambda:[('class:subtitle',clean(self.title))]),height=2,wrap_lines=True),
            Window(FormattedTextControl(lambda:'Filter: '+self.search if self.mode in ('menu','multi') else ''),height=1,style='class:muted'),
            ConditionalContainer(self.menu,active),
            ConditionalContainer(self.text,field),
            ConditionalContainer(Window(FormattedTextControl('Working… results appear on the right.')),Condition(lambda:self.mode=='busy')),
        ])
        self.root=HSplit([
            Window(FormattedTextControl([('class:brand','  CONFIGBACKUP  '),('class:header','Configuration studio')]),height=1,style='class:header'),
            Window(FormattedTextControl(lambda:'  '+clean(str(self.path.absolute()))+('  [UNSAVED]' if self.dirty else '')),height=1,style='class:muted'),
            Window(FormattedTextControl(lambda:'  Ctrl+S/F2 Save  F3 Validate  F4 Changes  F5 Undo  F6 Redo  F10 Exit' if self.command_handler and self.mode!='busy' and not self.command_active else '  Commands unavailable during operations' if self.command_handler else '  Configuration  /  '+clean(self.title)),height=1,style='class:bar'),
            VSplit([Frame(left,title='Configuration',width=Dimension(weight=1)),Frame(self.log,title='Details & results',width=Dimension(weight=1))],padding=1),
            Window(FormattedTextControl(self.footer),height=2,style='class:footer',wrap_lines=True),
        ])
        kb=KeyBindings()
        for key,delta in [('up',-1),('down',1),('pageup',-8),('pagedown',8)]:
            kb.add(key,filter=active & Condition(lambda:not self.in_log))(lambda e,d=delta:self.move(d))
        kb.add('home',filter=active & Condition(lambda:not self.in_log))(lambda e:self.move(-len(self.visible)))
        kb.add('end',filter=active & Condition(lambda:not self.in_log))(lambda e:self.move(len(self.visible)))
        @kb.add('enter',filter=Condition(lambda:self.mode!='busy' and not self.in_log))
        def enter(event):self.accept()
        @kb.add(' ',filter=Condition(lambda:self.mode=='multi' and not self.in_log))
        def toggle(event):
            if self.visible:
                value=self.visible[self.index]
                if value in self.selected:self.selected.remove(value)
                else:self.selected.add(value)
        @kb.add('escape',filter=Condition(lambda:self.mode in ('menu','multi') and not self.in_log))
        def back(event):
            if self.search:self.search='';self.index=0
            else:self.resolve(None if self.mode=='menu' else self.original_selection)
        @kb.add('escape',filter=field)
        def cancel_text(event):self.resolve(None)
        @kb.add('backspace',filter=active & Condition(lambda:not self.in_log))
        def remove(event):self.search=self.search[:-1];self.index=0
        @kb.add('<any>',filter=active & Condition(lambda:not self.in_log))
        def search(event):
            if event.data.isprintable():self.search+=event.data;self.index=0
        @kb.add('tab')
        def focus(event):
            if self.mode=='busy':self.app.layout.focus(self.log);return
            if self.in_log:self.app.layout.focus(self.text if self.mode=='text' else self.menu)
            else:self.app.layout.focus(self.log)
        @kb.add('c-c')
        def cancel(event):
            if self.pending is not None:
                if not self.confirming:
                    self.confirming=True;self.status='Press Ctrl+C again to discard unsaved edits and exit, or continue editing.'
                else:self.resolve(KeyboardInterrupt())
            else:self.status='An operation is running. Wait for it to finish before exiting.'
        for key,action in [('c-s','Save'),('f2','Save'),('f3','Validate'),('f4','Preview changes'),('f5','Undo last action'),('f6','Redo last action'),('f10','Quit without saving')]:
            kb.add(key, eager=True)(lambda e,a=action:self.global_command(a))
        style=Style.from_dict({'header':'bg:#172b4d #ffffff','brand':'bg:#087e8b #ffffff bold','bar':'bg:#254566 #ffffff','subtitle':'#63d4db bold','muted':'#a6b4c7','selected':'bg:#087e8b #ffffff bold','log':'#cbd5e1','input':'bg:#24364a #ffffff','footer':'bg:#172b4d #cbd5e1','frame.border':'#42617d'})
        self.app=Application(layout=Layout(self.root,focused_element=self.menu),key_bindings=kb,style=style,full_screen=True,input=input,output=output)

    @property
    def is_root(self):return self.mode=='menu' and self.title=='ConfigBackup configuration editor'
    @property
    def in_log(self):return self.app.layout.has_focus(self.log)
    @property
    def visible(self):return [v for v in self.items if self.search.casefold() in str(v).casefold()]
    def menu_text(self):
        result=[]
        for i,value in enumerate(self.visible):
            marker=('[x] ' if value in self.selected else '[ ] ') if self.mode=='multi' else '  '
            result.append(('class:selected' if i==self.index else '',('› ' if i==self.index else '  ')+marker+clean(value)+'\n'))
        return result or [('class:muted','  No matches; Backspace or Esc clears the filter.')]
    def footer(self):
        if self.confirming:return self.status
        if self.mode=='busy':return '  Working • '+self.status+'\n  Tab: view results'
        return ('  ↑↓ Navigate  Enter '+('Done  Space Toggle' if self.mode=='multi' else 'Select')+'  Esc Back  Type to filter' if self.mode!='text' else '  Enter Continue  ←→ Edit  Home/End Move')+'\n  Tab: switch to results (arrows/Page Up/Down scroll)  Ctrl+C twice: cancel'
    def move(self, delta):self.index=max(0,min(max(0,len(self.visible)-1),self.index+delta))
    def append_log(self, text):
        self.log_text=(self.log_text+text)[-50000:]
        self.log.buffer.set_document(Document(self.log_text,len(self.log_text)),bypass_readonly=True)
        self.app.invalidate()
    def present(self, mode, prompt, values, default, future):
        self.pending=future;self.mode=mode;self.title=prompt;self.items=list(values);self.search='';self.confirming=False
        self.index=min(self.history.get((prompt,tuple(values)),0),max(0,len(values)-1))
        if mode=='multi':self.selected=set(default);self.original_selection=[v for v in values if v in self.selected]
        if mode=='text':self.text.text='' if default is None else str(default);self.text.buffer.cursor_position=len(self.text.text)
        self.app.layout.focus(self.text if mode=='text' else self.menu);self.app.invalidate()
    def global_command(self, action):
        if not self.command_handler or self.command_active or self.mode=='busy' or self.pending is None:
            self.status='Commands are unavailable while an operation or command is running.';self.app.invalidate();return
        if action=='Save' and ((self.mode in ('text','multi') and (self.field_active or self.draft_active)) or (self.mode=='menu' and self.field_active)):
            self.queued_command=(action,self.title)
            self.accept()  # The workflow parses/assigns the field before the next prompt.
        else:
            self.resolve(('__command__',action))

    def request(self, mode, prompt, values=(), default=None):
        if self.closed:raise EOFError()
        if not self.command_active and self.checkpoint:self.checkpoint()
        if self.save_after_draft and not self.draft_active and not self.command_active:
            self.save_after_draft=False;self.execute_command('Save',mode)
        if self.queued_command and not self.command_active:
            action,previous=self.queued_command;self.queued_command=None
            if mode=='text' and prompt==previous:
                self.write_message('Save deferred: correct the invalid field first.\n')
            else:self.execute_command(action, mode, draft=self.draft_active)
        resume=None
        while True:
            future=concurrent.futures.Future()
            self.loop.call_soon_threadsafe(self.present,mode,prompt,values,default,future)
            if resume is not None:
                def restore(state=resume):
                    self.search,self.index,text,selected,cursor,was_log=state
                    if mode=='text':self.text.text=text;self.text.buffer.cursor_position=cursor
                    if mode=='multi':self.selected=selected
                    if was_log:self.app.layout.focus(self.log)
                    self.app.invalidate()
                self.loop.call_soon_threadsafe(restore)
            value=future.result()
            if isinstance(value,tuple) and len(value)==3 and value[0]=='__command__':
                _,action,resume=value
                self.execute_command(action,mode,draft=self.draft_active)
                continue
            return value

    def execute_command(self, action, mode, *, draft=False):
        self.command_active=True
        try:self.command_handler(action,mode,draft)
        finally:self.command_active=False

    def choose(self, prompt, values):
        if not values:self.write_message('No options available.\n');return None
        return self.request('menu',prompt,values)
    def select_many(self,prompt,values,selected):return self.request('multi',prompt,values,selected)
    def ask(self,prompt,default=None):return self.request('text',prompt,default=default)
    def __call__(self,prompt=''):return self.ask(prompt)
    def run_external(self, action):
        from prompt_toolkit.application import run_in_terminal
        async def invoke():return await run_in_terminal(action,in_executor=True)
        return asyncio.run_coroutine_threadsafe(invoke(),self.loop).result()
    def write_message(self,text):self.loop.call_soon_threadsafe(self.append_log,text)
    def accept(self):
        if self.mode=='text':self.resolve(self.text.text)
        elif self.mode=='multi':self.resolve([v for v in self.items if v in self.selected])
        elif self.visible:self.resolve(self.visible[self.index])
    def resolve(self,value):
        if self.pending is None:return
        if self.mode=='menu' and value in self.items:self.history[(self.title,tuple(self.items))]=self.items.index(value)
        if isinstance(value,tuple) and value[:1]==('__command__',):
            value=(*value,(self.search,self.index,self.text.text,set(self.selected),self.text.buffer.cursor_position,self.in_log))
        future=self.pending;self.pending=None;self.mode='busy';self.confirming=False;self.status='Processing selection'
        if isinstance(value,BaseException):future.set_exception(value)
        else:future.set_result(value)
        self.app.invalidate()
    async def run_async(self, workflow):
        self.loop=asyncio.get_running_loop()
        async def worker():
            def execute():
                with contextlib.redirect_stdout(ScreenLog(self)),contextlib.redirect_stderr(ScreenLog(self)):
                    try:workflow(self)
                    except (EOFError,KeyboardInterrupt):print('Cancelled; unsaved configuration edits discarded.')
                    except Exception as exc:self.error=exc;print('Configuration editor failed: '+clean(exc))
            await asyncio.to_thread(execute)
            if self.closed:return
            self.mode='menu';self.title='Session complete';self.items=['Close'];self.index=0;self.search=''
            self.pending=concurrent.futures.Future()
            self.app.layout.focus(self.menu);self.app.invalidate()
            try:await asyncio.wrap_future(self.pending)
            except (EOFError,KeyboardInterrupt):pass
            if not self.closed and self.app.future is not None and not self.app.future.done():self.app.exit()
        task=asyncio.create_task(worker())
        try:await self.app.run_async()
        finally:
            self.closed=True
            if self.pending is not None and not self.pending.done():self.pending.set_exception(EOFError())
            await task
        return 1 if self.error else 0
    def run(self,workflow):return asyncio.run(self.run_async(workflow))
