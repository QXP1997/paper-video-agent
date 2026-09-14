"""证据身份、输入有效性和四态归约。数据保存在已有 Loop Artifact 账本。"""

import re

from qharness.loop.models import CheckStatus
from qharness.loop.repository import digest
from qharness.tools.base import ToolExecutionResult
from qharness.verification.contracts import CheckKind, CheckObservation, CheckSpec


def combine(statuses) -> CheckStatus:
    values = tuple(statuses)
    # 已证实的业务失败不能被另一个环境错误或模型评分掩盖。
    for status in (CheckStatus.FAIL, CheckStatus.ERROR, CheckStatus.INCONCLUSIVE):
        if status in values:
            return status
    return CheckStatus.PASS if values else CheckStatus.INCONCLUSIVE


def interpret(spec: CheckSpec, result: ToolExecutionResult) -> CheckObservation:
    def answer(status, reason, count=None, signature=None):
        return CheckObservation(status=status, reason=reason, tests_run=count,
                                failure_signature=signature, artifact_id=result.artifact_id)
    if not result.success:
        return answer(CheckStatus.ERROR, f"工具执行未得到可用结果：{result.error_code or 'tool_error'}")
    data = result.data
    if not isinstance(data, dict) or type(data.get("exit_code")) is not int:
        return answer(CheckStatus.ERROR, "缺少命令退出状态")
    if data.get("cancelled") or data.get("timed_out"):
        return answer(CheckStatus.ERROR, "检查取消或超时")
    if data.get("stdout_truncated") or data.get("stderr_truncated"):
        return answer(CheckStatus.INCONCLUSIVE, "沙箱日志不完整，不能证明检查通过")
    if not all(isinstance(data.get(k), str) for k in ("stdout", "stderr")):
        return answer(CheckStatus.ERROR, "检查日志格式错误")
    code = data["exit_code"]
    log = data["stdout"] + "\n" + data["stderr"]
    if spec.kind == CheckKind.COMMAND:
        if code == 0:
            return answer(CheckStatus.PASS, "受信任命令断言通过")
        if code in spec.failure_exit_codes:
            return answer(CheckStatus.FAIL, "命令业务断言失败", signature=digest([code, log]))
        return answer(CheckStatus.ERROR, f"未定义的命令失败退出码：{code}")
    if spec.kind == CheckKind.UNITTEST:
        ran = re.findall(r"(?m)^Ran (\d+) tests? in .+$", log)
        endings = re.findall(r"(?m)^(OK(?: \([^\n]*\))?|FAILED \([^\n]*\))\s*$", log)
        if len(ran) != 1 or len(endings) != 1:
            return answer(CheckStatus.INCONCLUSIVE, "没有唯一完整的 unittest 结果摘要")
        count, ending = int(ran[0]), endings[0]
        if "errors=" in ending and "failures=" not in ending:
            return answer(CheckStatus.ERROR, "unittest 存在执行错误，需要诊断环境或检查代码", count)
        skipped = re.search(r"skipped=(\d+)", ending)
        expected = re.search(r"expected failures=(\d+)", ending)
        executed = count - (int(skipped[1]) if skipped else 0) - (int(expected[1]) if expected else 0)
        if count == 0 or executed <= 0:
            return answer(CheckStatus.INCONCLUSIVE, "没有实际通过或失败的测试", count)
        if ending.startswith("FAILED") and code == 1:
            signature = re.sub(r"(?m)^Ran \d+ tests? in .+$", "", log)
            return answer(CheckStatus.FAIL, "unittest 业务断言失败", count, digest(signature))
        if ending.startswith("OK") and code == 0:
            return answer(CheckStatus.PASS, "unittest 测试通过", count)
        return answer(CheckStatus.ERROR, "unittest 摘要与退出码不一致", count)
    if code in (2, 3, 4):
        return answer(CheckStatus.ERROR, "pytest 中断、收集错误或配置错误")
    if code == 5:
        return answer(CheckStatus.INCONCLUSIVE, "pytest 没有收集到测试", 0)
    # 只接受最终统计行，不能从任意测试输出中匹配一个 passed 单词。
    summaries = re.findall(r"(?m)^[= ]*((?:\d+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed)(?:, )?)+) in [^\n]+$", log)
    if len(summaries) != 1:
        return answer(CheckStatus.INCONCLUSIVE, "没有唯一完整的 pytest 统计摘要")
    counts = {key: int(value) for value, key in re.findall(r"(\d+) (\w+)", summaries[0])}
    count = counts.get("passed", 0) + counts.get("failed", 0)
    if counts.get("error", 0) or counts.get("errors", 0):
        return answer(CheckStatus.ERROR, "pytest 存在 setup/teardown 等执行错误", count)
    if counts.get("failed", 0) and code == 1:
        signature = re.sub(r" in [\d.]+s(?: \([^)]*\))?", " in <duration>", log)
        return answer(CheckStatus.FAIL, "pytest 业务断言失败", count, digest(signature))
    if count > 0 and not counts.get("failed", 0) and code == 0:
        return answer(CheckStatus.PASS, "pytest 测试通过", count)
    if count == 0:
        return answer(CheckStatus.INCONCLUSIVE, "没有实际通过或失败的测试", 0)
    return answer(CheckStatus.ERROR, "pytest 摘要与退出码不一致", count)
