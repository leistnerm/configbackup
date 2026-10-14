"""Stable configuration areas and session-wide commands."""
from contextlib import contextmanager

GROUPS = {
    'Overview': ['Configuration summary', 'Effective preview', 'Check setup'],
    'Backup & collection': ['Tasks', 'Shared connections', 'Optional collectors and discovery', 'Collection profiles', 'Saved collection profiles', 'Discover files from scheduled jobs', 'Registry selections', 'Default registry selections'],
    'Storage & security': ['Backup settings', 'Task defaults', 'Retention settings', 'Git settings', 'Sensitive collection', 'Deletion settings'],
    'Monitoring & reports': ['Monitoring sources', 'Alert rules', 'Notification channels', 'Monitoring settings', 'HTML review reports', 'Performance recording and reports', 'Sources', 'Report/analysis settings'],
    'Setup & launchers': ['Autodetect and guided setup', 'Python dependencies', 'Generate startup launcher', 'Launcher manager', 'PowerShell execution policy', 'Windows readiness', 'Database connection tests', 'Generate database access scripts'],
    'Recovery & maintenance': ['Recovery status and reset', 'Recovery files and export', 'Verify and rehearse setup recovery', 'Recovery readiness targets', 'Recovery and review workflows'],
    'Advanced configuration': ['Current configuration', 'Show configuration'],
}
LABELS = {'Quit without saving':'Exit','Backup settings':'Archive location & setup backup', 'Retention settings':'Named retention policies','Collection profiles':'Collector section presets', 'Saved collection profiles':'Named reusable profiles', 'Database connection tests':'Standalone diagnostic profiles'}
COMMANDS = ['Save', 'Validate', 'Preview changes', 'Undo last action', 'Redo last action', 'Quit without saving']

class Navigate(BaseException):
    def __init__(self, destination):self.destination=destination

@contextmanager
def draft(input_fn):
    """An object being constructed is not yet part of the saved configuration."""
    previous=getattr(input_fn,'draft_active',False)
    if getattr(input_fn,'console_ui',False):input_fn.draft_active=True
    try:yield
    finally:
        if getattr(input_fn,'console_ui',False):input_fn.draft_active=previous


def grouped_choice(available, state, input_fn):
    from configbackup_lib.ui.configure import choice
    groups={name:[item for item in items if item in available] for name,items in GROUPS.items()}
    groups={name:items for name,items in groups.items() if items}
    while True:
        if not state[0]:
            selected=choice('ConfigBackup configuration editor',list(groups)+['Commands'],input_fn)
            if selected is None:return 'Quit without saving'
            state[0]=selected
        options=COMMANDS if state[0]=='Commands' else groups[state[0]]
        labels=[LABELS.get(item,item) for item in options]
        selected=choice(state[0],labels+['Back'],input_fn)
        if selected in (None,'Back'):state[0]=None;continue
        return options[labels.index(selected)]


def field_editor(function):
    """Mark value selection/input, excluding operational confirmations."""
    import functools, inspect
    signature=inspect.signature(function)
    @functools.wraps(function)
    def wrapped(*args,**kwargs):
        arguments=signature.bind(*args,**kwargs);arguments.apply_defaults()
        ui=arguments.arguments.get('input_fn')
        previous=getattr(ui,'field_active',False)
        if getattr(ui,'console_ui',False):ui.field_active=True
        try:return function(*args,**kwargs)
        finally:
            if getattr(ui,'console_ui',False):ui.field_active=previous
    return wrapped
