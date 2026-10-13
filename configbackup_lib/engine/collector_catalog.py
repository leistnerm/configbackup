"""Public stdlib interface for optional system collector configuration."""
from collectors.system.optional_catalog import WINDOWS_COMPONENTS, WINDOWS_DETAILS, discover
from collectors.system.optional_catalog import catalog as _catalog

def catalog():
    rows=_catalog()
    for row in rows:
        row.update(scopes=[row['scope']+'/*'],options=row.get('values',[row.get('value','PATH')]),
                   default=False,cost='Per-object local queries; can take minutes on large services' if row['id'] in WINDOWS_COMPONENTS or row['id'] in WINDOWS_DETAILS else 'Bounded local reads; no mutations')
    return rows
