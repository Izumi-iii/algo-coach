"""Safe error categories: provider bodies and credentials never enter checkpoints."""

from pydantic import ValidationError


class TeachingPolicyError(ValueError):
    pass


class TeachingOutputError(ValueError):
    pass


def teaching_failure(exc: Exception) -> tuple[str, str, bool]:
    """Return a stable category, controlled user message and whether retry can help."""
    if isinstance(exc, TeachingPolicyError):
        return "reply_policy_rejected", "模型回复超过当前提示范围或包含无依据结论", True
    if isinstance(exc, (TeachingOutputError, ValidationError)):
        return "invalid_model_output", "模型未返回有效的结构化教学内容", True
    status = getattr(exc, "status_code", None)
    if status == 401:
        return "authentication_failed", "模型服务认证失败（401），请检查本地 API Key", False
    if status == 402:
        return "insufficient_balance", "模型服务余额不足（402），请检查账户余额", False
    if status == 403:
        return "permission_denied", "模型服务拒绝访问（403），请检查模型使用权限", False
    if status == 404:
        return "model_or_endpoint_not_found", "模型或接口不存在（404），请检查模型名与地址", False
    if status == 429:
        return "rate_limited", "模型服务请求受限（429），请稍后重试", False
    if status == 400 or status == 422:
        return "request_rejected", "模型服务拒绝请求参数，请核对接口和工具调用兼容性", False
    if isinstance(exc, TimeoutError) or "Timeout" in type(exc).__name__:
        return "model_timeout", "模型服务请求超时，可稍后重试", True
    return "model_service_error", "模型服务调用失败，请检查网络或服务状态", True
