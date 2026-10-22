"""Safe error categories: provider bodies and credentials never enter checkpoints."""

from pydantic import ValidationError

POLICY_FEEDBACK = {
    "question_budget": (
        "当前诊断只聚焦一个卡点。删去多余追问，最多保留一个与本次错误直接相关的问题；"
        "若用户明确要求直接回答，则不提问。不要把多个问题用逗号串成一个问号。"
    ),
    "browser_claim": (
        "只能按 browser_feedback 非空时的 verdict 描述当前版本，明确标注浏览器回传报告；"
        "没有匹配报告时不要引用历史判题状态，不将它当作本地执行或独立认证。"
    ),
    "history_evidence": (
        "历史引文必须填写 history_citations，使用提供的 message_id、逐字原文 quote 和原版本哈希；"
        "找不到来源就不要引用或编造原话。旧标签未知时保持 null，不猜测归属。"
    ),
    "counterexample_evidence": (
        "counterexamples 的输入必须满足已审核题面约束，预期输出必须独立推算；"
        "两数之和恰有一个有效下标对。正文所有 nums/head 数组字面量都须有对应结构化数据，"
        "不要在正文试错或列出未登记的废弃候选；讨论违反约束的输入用 kind=constraint_example。"
        "检查两个输出下标对应的元素之和，不将元素值当下标。只保留一个核对后的简短样例；"
        "不得声称已执行用户代码。"
    ),
    "hint_limit": (
        "实际披露和 hint_level_used 均不得超过应用上限；评价用户已说明的思路不应添加新算法。"
    ),
    "code_block": (
        "当前级别不允许代码块。请用中文和行号分析已提交代码，不复述代码块、不提供修改后实现。"
    ),
    "code_declaration": (
        "当前未授权完整实现。不要重复 class/def 声明；只用中文或行号说明现有代码问题。"
    ),
    "platform_claim": "没有平台提交证据，不得声称 LeetCode Accepted 或平台已通过。",
    "test_claim": "没有当前代码的有效执行通过报告。只说明已有验证状态，不得宣称测试通过。",
    "answer_missing": (
        "本轮是显式答案请求。请给完整思路、复杂度和包含当前题目方法实现的 Python 代码块，"
        "hint_level_used=4、full_solution=true；不能只提问或复述原代码。"
    ),
    "answer_failed": (
        "参考实现未通过独立本地验证，请依据反馈重新生成完整实现；不要声称原实现已通过。"
    ),
}


class TeachingPolicyError(ValueError):
    def __init__(self, message, *, rule=None):
        super().__init__(message)
        # Only application-owned reasons may be sent back to the model.
        self.rule = rule if rule in POLICY_FEEDBACK else None


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
