"""supabase_rest - calls Supabase PostgREST functions over HTTPS and maps every failure to a V2 error code.

Layer:   adapters
Stage:   V2-02B
Flags:   none
Raises:  V2Error DB_CONSTRAINT (the data was refused), DB_UNAVAILABLE (network, timeout, 5xx), DB_UNEXPECTED
Budget:  one subrequest per call, 5 s timeout, no retry here (Meta retries the whole delivery on a 5xx)
Tests:   tests/v2/adapters/test_supabase_rest.py
"""

from app.v2.core.errors import ErrorCode, V2Error
from app.v2.core.safe import as_dict

TIMEOUT_SECONDS = 5.0
DATA_CLASSES = ("22", "23")  # SQLSTATE classes: data exception, integrity constraint violation (ADR-10, item 6)
RETRY_STATUSES = (408, 429)


def sqlstate_of(body: object) -> str | None:
    """The SQLSTATE in a PostgREST error body, or None (PostgREST's own codes look like PGRST202)."""
    code = as_dict(body).get("code")
    return code if isinstance(code, str) and len(code) == 5 and code.isalnum() else None


def classify_failure(status: int, body: object) -> ErrorCode:
    """Which V2 error a failed PostgREST answer means."""
    state = sqlstate_of(body)
    if state is not None and state.startswith(DATA_CLASSES):
        return ErrorCode.DB_CONSTRAINT
    if status >= 500 or status in RETRY_STATUSES:
        return ErrorCode.DB_UNAVAILABLE
    return ErrorCode.DB_UNEXPECTED


def _json(response: object) -> object:
    try:
        return response.json()
    except ValueError:  # an empty body (204) or a body that is not JSON
        return None


class SupabaseRest:
    """DbPort over PostgREST: POST {url}/rest/v1/rpc/{name} with the service key."""

    def __init__(self, url: str, key: str) -> None:
        self._base = url.rstrip("/") + "/rest/v1/rpc/"
        self._headers = {"apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json"}

    async def rpc(self, name: str, params: dict) -> object:
        import httpx  # inside the function: adapters may not import it at module level (ADR-9)

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                response = await client.post(self._base + name, headers=self._headers, json=params)
        except httpx.HTTPError as exc:
            raise V2Error(ErrorCode.DB_UNAVAILABLE, kind=type(exc).__name__) from exc
        body = _json(response)
        if 200 <= response.status_code < 300:
            return body
        raise V2Error(classify_failure(response.status_code, body), http=response.status_code,
                      sqlstate=sqlstate_of(body) or "")
