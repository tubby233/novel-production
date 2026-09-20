"""统一异常与错误分类。

所有面向用户的错误都带中文 message，界面直接展示，不做二次解析。
"""

from __future__ import annotations

from enum import StrEnum


class ApiErrorKind(StrEnum):
    AUTH = "auth"                    # 401/403：密钥无效
    RATE_LIMIT = "rate_limit"        # 429：并发/限速
    TIMEOUT = "timeout"              # 请求超时
    CONNECTION = "connection"        # 连接失败 / DNS / 证书
    BAD_REQUEST = "bad_request"      # 400：参数或模型名错误
    SERVER = "server"                # 5xx
    CANCELLED = "cancelled"          # 用户停止
    UNKNOWN = "unknown"


KIND_HINTS: dict[ApiErrorKind, str] = {
    ApiErrorKind.AUTH: "请在设置中检查 api_key 是否正确、是否已过期。",
    ApiErrorKind.RATE_LIMIT: "已触发账号并发/限速限制，请在设置中降低并发数后重试。",
    ApiErrorKind.TIMEOUT: "请求超时。可在设置中调大“请求超时”，或降低 max_tokens / 思考强度。",
    ApiErrorKind.CONNECTION: "无法连接服务器，请检查网络、代理与 base_url 设置。",
    ApiErrorKind.BAD_REQUEST: "请求参数被服务端拒绝，请检查 model 名称与各参数取值。",
    ApiErrorKind.SERVER: "服务端返回错误，通常为临时故障，可稍后重试。",
    ApiErrorKind.CANCELLED: "任务已被用户停止。",
    ApiErrorKind.UNKNOWN: "发生未知错误，详见下方原始信息。",
}


class AppError(Exception):
    """应用内所有可预期错误的基类。"""

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - 纯展示
        return f"{self.message}\n{self.detail}".strip()


class ApiError(AppError):
    """API 调用错误。"""

    def __init__(
        self,
        kind: ApiErrorKind,
        message: str,
        *,
        status_code: int | None = None,
        raw: str = "",
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message, detail=raw)
        self.kind = kind
        self.status_code = status_code
        self.raw = raw
        if retryable is None:
            retryable = kind in (
                ApiErrorKind.RATE_LIMIT,
                ApiErrorKind.TIMEOUT,
                ApiErrorKind.CONNECTION,
                ApiErrorKind.SERVER,
            )
        self.retryable = retryable

    @property
    def hint(self) -> str:
        return KIND_HINTS.get(self.kind, KIND_HINTS[ApiErrorKind.UNKNOWN])

    def user_text(self) -> str:
        """界面展示用：错误说明 + 建议（原始报文另外放详情区）。"""
        return f"{self.message}\n建议：{self.hint}"


class TaskCancelled(AppError):
    """用户主动停止。"""

    def __init__(self, message: str = "任务已被用户停止。") -> None:
        super().__init__(message)


class TemplateError(AppError):
    """模板占位符相关错误（仅在 strict 模式下抛出）。"""


class ContextOverflowError(ApiError):
    """输入 + max_tokens 超过模型上下文窗口。"""

    def __init__(self, message: str) -> None:
        super().__init__(ApiErrorKind.BAD_REQUEST, message, retryable=False)
