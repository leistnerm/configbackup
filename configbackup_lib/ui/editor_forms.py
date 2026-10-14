"""Typed configuration fields and cancellation without changing hidden values."""
from configbackup_lib.ui.navigation import field_editor
class EditCancelled(Exception):
    pass

ENUMS = {
    'storage': ['filesystem','git','both'],
    'access_profile': ['read-only','full'],
    'powershell_execution_policy': ['inherit','RemoteSigned','Bypass','AllSigned','Restricted','Unrestricted'],
    'log_level': ['DEBUG','INFO','WARNING','ERROR'],
    'phase': ['pre_run','backup','post_run'],
}


@field_editor
def edit_boolean(prompt, current, input_fn=input):
    from configbackup_lib.ui.configure import choice,ask
    if not getattr(input_fn,'console_ui',False):
        while True:
            value=ask(prompt+' (yes/no, q cancels)','yes' if current else 'no',input_fn).lower()
            if value=='q':return current
            if value in ('yes','y','true','1'):return True
            if value in ('no','n','false','0'):return False
            print('Choose yes, no, or q.')
    selected = choice(prompt, ['true', 'false', 'Cancel'], input_fn)
    return current if selected in (None, 'Cancel') else selected == 'true'


@field_editor
def scalar(name, value, input_fn=input, *, literal=False):
    from configbackup_lib.ui.configure import ask, choice, json_value
    from configbackup_lib.configuration.config_schema import field_type
    from configbackup_lib.configuration.configuration_profiles import help_text
    print(help_text(str(name)))
    if not literal and name in ENUMS:
        selected = choice(str(name)+' (configured: '+str(value)+')', ENUMS[name]+['Cancel'], input_fn)
        return value if selected in (None,'Cancel') else selected
    if name == 'timezone' and not literal:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        zones=['UTC','America/New_York','America/Chicago','America/Los_Angeles','Europe/London','Europe/Berlin','Asia/Tokyo','Australia/Sydney']
        if isinstance(value,str) and value not in zones:zones.insert(0,value)
        selected=choice('IANA timezone',zones+['Custom IANA name','Cancel'],input_fn)
        if selected in (None,'Cancel'):return value
        if selected=='Custom IANA name':selected=ask('IANA timezone',value,input_fn)
        try:ZoneInfo(selected)
        except (ValueError,ZoneInfoNotFoundError):raise ValueError('Timezone is unavailable. Check the IANA name or use Python dependencies to install tzdata.') from None
        return selected
    kind = type(value) if literal else field_type(str(name), value)
    if kind is bool:return edit_boolean(str(name), value, input_fn)
    if kind in (int,float):
        draft=value
        while True:
            draft=ask(str(name),draft,input_fn)
            try:return kind(draft)
            except ValueError:print('Enter a valid '+kind.__name__+'; the previous setting is unchanged.')
    if value is None:return json_value(str(name),None,input_fn)
    return ask(str(name),value,input_fn)


class EditHistory:
    def __init__(self, limit=100):self.undo_stack=[];self.redo_stack=[];self.limit=limit
    def record(self, before, after):
        import copy
        if before != after:
            self.undo_stack.append(copy.deepcopy(before));self.undo_stack=self.undo_stack[-self.limit:];self.redo_stack=[]
    def undo(self, config):
        import copy
        if not self.undo_stack:return False
        self.redo_stack.append(copy.deepcopy(config));config.clear();config.update(self.undo_stack.pop());return True
    def redo(self, config):
        import copy
        if not self.redo_stack:return False
        self.undo_stack.append(copy.deepcopy(config));config.clear();config.update(self.redo_stack.pop());return True
