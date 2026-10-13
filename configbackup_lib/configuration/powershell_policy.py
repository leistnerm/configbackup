"""Explicit child-process execution policy; never change persistent Windows policy."""
import os

POLICIES = ('inherit', 'RemoteSigned', 'Bypass', 'AllSigned', 'Restricted', 'Unrestricted')


def validate(value):
    if not isinstance(value, str) or value not in POLICIES:
        raise ValueError('options.powershell_execution_policy must be one of: ' + ', '.join(POLICIES))
    return value


def environment(config, env=None, *, platform=None):
    value = validate((config.get('options') or {}).get('powershell_execution_policy', 'inherit'))
    result = dict(os.environ if env is None else env)
    if (os.name if platform is None else platform) == 'nt' and value != 'inherit':
        # PowerShell reads this as Process-scope policy, including nested collectors.
        # Explicit -ExecutionPolicy arguments and Group Policy still take precedence.
        result['PSExecutionPolicyPreference'] = value
    return result


def wizard(config, input_fn=input):
    from configbackup_lib.ui.configure import choice
    current = (config.get('options') or {}).get('powershell_execution_policy', 'inherit')
    print('Current PowerShell policy: ' + str(current))
    print('Applies to Windows collector child processes only. No machine/user policy is changed. '
          'Group Policy still applies; Bypass disables policy checks in these processes. '
          'Regenerate Windows startup launchers after changing this setting.')
    selected = choice('PowerShell execution policy', list(POLICIES), input_fn)
    if selected is not None:
        config.setdefault('options', {})['powershell_execution_policy'] = validate(selected)
