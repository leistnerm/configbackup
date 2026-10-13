# ConfigBackup 1.3.1

- New configurator PowerShell execution-policy menu; default inherits policy.
  Explicit settings apply to Windows collector child processes, diagnostics and
  newly generated startup launchers without changing persistent system policy.
- PR worktrees remain until journal finalization. Missing worktrees recover when
  the local PR branch matches the recorded commit; changed branches require review.
- Explicit journal reset requires acknowledgement, preserves journal/state evidence,
  and leaves archive and Git files unchanged. It does not repair incomplete runs.
- [Setup walkthrough](setup-walkthrough.md) links SSIS, shared connections,
  sensitive approvals, permissions and troubleshooting documentation.

See [validation limitations](../VALIDATION.md). Native Windows execution-policy
and SSIS recovery behavior have not been validated on a Windows host.
