"""领域错误到 HTTP 状态的映射。"""


class CoordError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message, *, code=None, status=None, details=None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status:
            self.status = status
        self.details = details or {}

    def to_dict(self):
        body = {"code": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return body


class ValidationError(CoordError):
    status = 400
    code = "invalid_payload"


class AuthError(CoordError):
    status = 401
    code = "unauthenticated"


class PermissionError(CoordError):  # noqa: A001 - 领域内有意遮蔽内建名
    status = 403
    code = "forbidden"


class NotFound(CoordError):
    status = 404
    code = "not_found"


class Conflict(CoordError):
    status = 409
    code = "conflict"


class StaleVersion(Conflict):
    code = "stale_version"


class RuleViolation(Conflict):
    code = "rule_violation"


class AlreadyAdvanced(Conflict):
    """同一逻辑事件已推进过状态（自然键去重）。"""

    code = "already_advanced"

    def __init__(self, event_id, message="该事件已处理过，状态不重复推进"):
        super().__init__(message)
        self.event_id = event_id
