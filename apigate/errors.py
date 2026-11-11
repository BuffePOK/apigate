"""Error types. Everything the client sees is rendered in OpenAI error format."""
from __future__ import annotations


class GateError(Exception):
    """An error that is safe to show to the client."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        type_: str | None = None,
        retry_after: float | None = None,
        param: str | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.type = type_ or _default_type(status)
        self.retry_after = retry_after
        self.param = param

    def body(self) -> dict:
        return {"error": {"message": self.message, "type": self.type, "param": self.param, "code": self.code}}


def _default_type(status: int) -> str:
    if status == 401:
        return "authentication_error"
    if status == 403:
        return "permission_error"
    if status == 404:
        return "not_found_error"
    if status == 429:
        return "rate_limit_error"
    if status == 402:
        return "insufficient_quota"
    if status >= 500:
        return "server_error"
    return "invalid_request_error"


class UpstreamError(Exception):
    """A failure talking to a provider. `kind` drives the retry / cooldown policy."""

    RETRYABLE = {"rate_limit", "quota", "auth", "permission", "not_found", "server", "network", "timeout"}

    def __init__(self, kind: str, status: int, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.message = message
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.kind in self.RETRYABLE
