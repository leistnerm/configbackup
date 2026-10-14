"""Typed configuration fields and cancellation without changing hidden values."""
from configbackup_lib.ui.navigation import field_editor
from configbackup_lib.engine.configbackup import PHASES
class EditCancelled(Exception):
    pass

ENUMS = {
    'storage': ['filesystem','git','both'],
    'access_profile': ['read-only','full'],
    'powershell_execution_policy': ['inherit','RemoteSigned','Bypass','AllSigned','Restricted','Unrestricted'],
    'log_level': ['DEBUG','INFO','WARNING','ERROR'],
    'phase': list(PHASES),
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
    selected = choice(prompt, ['true', 'false', 'Cancel'], input_fn, current='true' if current else 'false')
    return current if selected in (None, 'Cancel') else selected == 'true'


@field_editor
def scalar(name, value, input_fn=input, *, literal=False):
    from configbackup_lib.ui.configure import ask, choice, json_value
    from configbackup_lib.configuration.config_schema import field_type
    from configbackup_lib.configuration.configuration_profiles import help_text
    print(help_text(str(name)))
    if not literal and name in ENUMS:
        selected = choice(str(name)+' (configured: '+str(value)+')', list(dict.fromkeys(([value] if isinstance(value,str) else [])+ENUMS[name]))+['Cancel'], input_fn, current=value)
        return value if selected in (None,'Cancel') else selected
    if name == 'timezone' and not literal:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        zones=['UTC','America/New_York','America/Chicago','America/Los_Angeles','Europe/London','Europe/Berlin','Asia/Tokyo','Australia/Sydney']
        if isinstance(value,str) and value not in zones:zones.insert(0,value)
        selected=choice('IANA timezone',zones+['Custom IANA name','Cancel'],input_fn,current=value)
        if selected in (None,'Cancel'):return value
        if selected=='Custom IANA name':
            selected=validated('IANA timezone',value,input_fn,lambda text:(ZoneInfo(text),text)[1],error='Use an installed IANA timezone name; Python dependencies can install tzdata.')
        else:
            try:ZoneInfo(selected)
            except (ValueError,ZoneInfoNotFoundError):
                selected=validated('IANA timezone',selected,input_fn,lambda text:(ZoneInfo(text),text)[1],error='Use an installed IANA timezone name; Python dependencies can install tzdata.')
        return selected
    kind = type(value) if literal else field_type(str(name), value)
    if kind in (dict,list):
        import json
        def convert(raw):
            parsed=json.loads(raw)
            if not isinstance(parsed,kind):raise ValueError()
            return parsed
        return validated(str(name)+' (JSON)',json.dumps(value),input_fn,convert,
                         error='Enter a JSON '+('object' if kind is dict else 'array')+'; the previous setting is unchanged.')
    if kind is bool:return edit_boolean(str(name), value, input_fn)
    if kind in (int,float):
        draft=value
        while True:
            draft=ask(str(name),draft,input_fn)
            try:
                result=kind(draft)
                if getattr(input_fn,'console_ui',False):input_fn.field_error=''
                return result
            except ValueError:
                message='Enter a valid '+kind.__name__+'; the previous setting is unchanged.'
                if getattr(input_fn,'console_ui',False):input_fn.field_error=message
                print(message)
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


@field_editor
def validated(prompt,current,input_fn=input,convert=str,*,error='Invalid value; correct this field.'):
    from configbackup_lib.ui.configure import ask
    raw=current
    while True:
        raw=ask(prompt,raw,input_fn)
        try:
            result=convert(raw)
            if getattr(input_fn,'console_ui',False):input_fn.field_error=''
            return result
        except (ValueError,TypeError,KeyError):
            if getattr(input_fn,'console_ui',False):input_fn.field_error=error
            print(error)


def integer(prompt,current,input_fn=input,minimum=None,maximum=None):
    def convert(text):
        number=int(text)
        if minimum is not None and number<minimum or maximum is not None and number>maximum:raise ValueError()
        return number
    return validated(prompt,current,input_fn,convert,error=f'Enter an integer between {minimum if minimum is not None else "-infinity"} and {maximum if maximum is not None else "infinity"}.')


@field_editor
def text(prompt,current='',input_fn=input):
    from configbackup_lib.ui.configure import ask
    return ask(prompt,current,input_fn)


@field_editor
def timezone(prompt,current='UTC',input_fn=input):
    from zoneinfo import ZoneInfo
    return validated(prompt,current,input_fn,lambda value:(ZoneInfo(value),value)[1],
                     error='Use an installed IANA timezone name; Python dependencies can install tzdata.')


@field_editor
def flag(prompt,default=True,input_fn=input):
    """A configuration boolean; operational approvals use configure.boolean."""
    from configbackup_lib.ui.configure import boolean
    return boolean(prompt,default,input_fn)


@field_editor
def selection(prompt,values,input_fn=input,*,current=None):
    from configbackup_lib.ui.configure import choice
    selected=choice(prompt,values,input_fn,current=current)
    if selected is None:raise EditCancelled()
    return selected
