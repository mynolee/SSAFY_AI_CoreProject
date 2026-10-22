"""Public, deliberately small service errors."""


class ServiceError(Exception):
    def __init__(self, code: str, message: str = "요청을 처리할 수 없습니다.", status: int = 422, retryable: bool = False):
        super().__init__(code)
        self.code, self.message, self.status, self.retryable = code, message, status, retryable

    def as_dict(self):
        return {"code": self.code, "message": self.message, "retryable": self.retryable}
