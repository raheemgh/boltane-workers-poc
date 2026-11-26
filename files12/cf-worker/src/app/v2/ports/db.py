"""db - the database port: one call to a database function.

Layer:   ports
Stage:   V2-02B
Flags:   none
Raises:  V2Error DB_UNAVAILABLE, DB_CONSTRAINT or DB_UNEXPECTED, raised by the adapter behind it
Budget:  one subrequest per call
Tests:   tests/v2/ports/test_db.py
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class DbPort(Protocol):
    async def rpc(self, name: str, params: dict) -> object:
        """Call the database function `name` with named `params`; return its JSON answer (dict, list, bool or None)."""
        ...
