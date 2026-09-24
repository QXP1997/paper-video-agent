import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

from langchain_core.prompts import ChatPromptTemplate

from paper_video_agent.chat import (
    LLM_BASE_URL,
    LLM_EXTRA_BODY,
    LLM_MAX_TOKENS,
    LLM_MODEL,
    LLM_TEMPERATURE,
    get_llm,
    invoke_structured_with_retry,
)
from paper_video_agent.editor import _normalize_edited_script
from paper_video_agent.models import (
    EditedScriptChapters,
    PaperScript,
    PaperScriptAudit,
    ScriptValidationIssue,
    ScriptValidationMetrics,
    ScriptValidationReport,
)

FINALIZER_VERSION = 2
FINAL_CACHE_VERSION = 1
MAX_NUMBERS_PER_SEGMENT = 3
MAX_SEGMENT_CHARACTERS = 450
FUZZY_DUPLICATE_THRESHOLD = 0.94
MIN_FUZZY_DUPLICATE_CHARACTERS = 40
MAX_EXPANSION_RATIO = 1.05


repair_prompt = ChatPromptTemplate.from_messages([
    (
        "system",
        """
你是论文视频口播流程的最终返修编辑。候选稿已经完成全局编辑，现在只需要解决 deterministic_issues 中列出的具体问题。不要借机重新创作，不要扩大修改范围，也不要增加新的论文事实。

你只能依据 candidate_script 和 fact_audit：

- 对重复内容，保留最适合主线的位置，删除或合并其余表达；
- 对数字过密的段落，只保留支撑该结论最有代表性的一至两个数字，或者拆分逻辑不同的内容；
- 对未经允许的新数字，删除该数字，或恢复为 fact_audit 明确支持的表达；不得自行推算替代值；
- 对仍然原样保留的审核问题，按照 fact_audit 的 issue 和 suggested_revision 做最小修正；没有可靠修正时删除该陈述；
- 对过长段落，按主要意思拆分，或删除不推动主线的细节；
- 对脚本膨胀，优先删除模板化开场、重复总结、表格朗读和次要实现细节；
- 不得把 may、suggest、indicate、相关性或消融现象升级成确定因果；
- 不得出现“审核”“校验”“返修”等流程元信息。

输出 EditedScriptChapters，包含返修后的完整候选稿：

- 章节顺序必须保持不变；
- 不得新增 candidate_script 中不存在的 chapter_id；
- 可以删除完全重复的章节，但至少保留四章；
- title 保持不变；
- 每章至少一个 segment；
- segment.page 只能使用候选稿已有页面，或原章节规划的 source_pages；
- segment.visual_id 只能使用 candidate_script.visuals 中存在且与 segment.page 同页的 ID；没有明确视觉焦点时为 null；
- 不输出解释、Markdown 或额外字段。
""",
    ),
    (
        "human",
        """
请只修复列出的问题，并返回完整返修稿。

候选稿：

{candidate_script}

事实审核：

{fact_audit}

确定性校验问题：

{deterministic_issues}

上一次输出的结构或约束错误（首次调用为空）：

{validation_feedback}
""",
    ),
])


@dataclass(frozen=True)
class _NumberMention:
    raw: str
    value: Decimal
    unit: str
    approximate: bool


_NUMBER_PATTERN = re.compile(
    r"(?<![\d.])"
    r"(?P<number>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<unit>个百分点|[%％倍]|[KkMmBb])?"
)
_APPROXIMATION_MARKERS = (
    "约",
    "大约",
    "接近",
    "近",
    "左右",
    "超过",
    "不到",
    "低于",
    "高于",
)


@lru_cache(maxsize=1)
def _get_repair_chain():
    return repair_prompt | get_llm().with_structured_output(
        EditedScriptChapters,
        method="function_calling",
        include_raw=True,
    )


def _prompt_messages(prompt: ChatPromptTemplate) -> list[dict[str, str]]:
    return [
        {
            "role": type(message).__name__,
            "template": message.prompt.template,
        }
        for message in prompt.messages
    ]


def finalizer_cache_material() -> dict:
    """Return deterministic inputs that affect final validation and repair."""
    return {
        "finalizer_version": FINALIZER_VERSION,
        "thresholds": {
            "max_numbers_per_segment": MAX_NUMBERS_PER_SEGMENT,
            "max_segment_characters": MAX_SEGMENT_CHARACTERS,
            "fuzzy_duplicate_threshold": FUZZY_DUPLICATE_THRESHOLD,
            "min_fuzzy_duplicate_characters": (
                MIN_FUZZY_DUPLICATE_CHARACTERS
            ),
            "max_expansion_ratio": MAX_EXPANSION_RATIO,
        },
        "llm": {
            "model": LLM_MODEL,
            "base_url": LLM_BASE_URL,
            "temperature": LLM_TEMPERATURE,
            "max_tokens": LLM_MAX_TOKENS,
            "extra_body": LLM_EXTRA_BODY,
        },
        "prompts": {
            "repair": _prompt_messages(repair_prompt),
        },
        "schemas": {
            "edited_script_chapters": EditedScriptChapters.model_json_schema(),
            "validation_report": ScriptValidationReport.model_json_schema(),
            "paper_script": PaperScript.model_json_schema(),
        },
    }


def _normalize_text(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", text).lower()


def _number_mentions(text: str) -> list[_NumberMention]:
    mentions = []
    for match in _NUMBER_PATTERN.finditer(text):
        number_text = match.group("number").replace(",", "")
        try:
            value = Decimal(number_text)
        except InvalidOperation:
            continue

        unit = (match.group("unit") or "").lower().replace("％", "%")
        context = text[
            max(0, match.start() - 4):min(len(text), match.end() + 4)
        ]
        mentions.append(_NumberMention(
            raw=match.group(0).strip(),
            value=value,
            unit=unit,
            approximate=any(
                marker in context for marker in _APPROXIMATION_MARKERS
            ),
        ))
    return mentions


def _all_audit_text(audit: PaperScriptAudit) -> list[str]:
    values = []
    for chapter in audit.chapters:
        for segment in chapter.segments:
            for claim in segment.claims:
                values.extend(filter(None, [
                    claim.claim,
                    claim.evidence,
                    claim.issue,
                    claim.suggested_revision,
                ]))
    return values


def _number_is_allowed(
    candidate: _NumberMention,
    allowed: list[_NumberMention],
) -> bool:
    comparable = [item for item in allowed if item.unit == candidate.unit]
    if any(item.value == candidate.value for item in comparable):
        return True
    if not candidate.approximate:
        return False

    for item in comparable:
        difference = abs(item.value - candidate.value)
        if difference <= Decimal("0.1"):
            return True
        scale = max(abs(item.value), Decimal("1"))
        if difference / scale <= Decimal("0.05"):
            return True
    return False


def _segment_records(script: PaperScript) -> list[tuple[str, int, str]]:
    return [
        (chapter.chapter_id, index, segment.text)
        for chapter in script.chapters
        for index, segment in enumerate(chapter.segments, start=1)
    ]


def _duplicate_pairs(
    script: PaperScript,
) -> tuple[list[tuple], list[tuple]]:
    records = _segment_records(script)
    exact_pairs = []
    fuzzy_pairs = []
    for left_index, left in enumerate(records):
        left_normalized = _normalize_text(left[2])
        if len(left_normalized) < 15:
            continue
        for right in records[left_index + 1:]:
            right_normalized = _normalize_text(right[2])
            if left_normalized == right_normalized:
                exact_pairs.append((left, right))
                continue
            if min(len(left_normalized), len(right_normalized)) < (
                MIN_FUZZY_DUPLICATE_CHARACTERS
            ):
                continue
            similarity = SequenceMatcher(
                None,
                left_normalized,
                right_normalized,
            ).ratio()
            if similarity >= FUZZY_DUPLICATE_THRESHOLD:
                fuzzy_pairs.append((left, right, similarity))
    return exact_pairs, fuzzy_pairs


def _script_metrics(script: PaperScript) -> ScriptValidationMetrics:
    records = _segment_records(script)
    number_counts = [_number_mentions(text) for _, _, text in records]
    exact_pairs, fuzzy_pairs = _duplicate_pairs(script)
    return ScriptValidationMetrics(
        chapter_count=len(script.chapters),
        segment_count=len(records),
        character_count=sum(len(text) for _, _, text in records),
        number_mentions=sum(len(items) for items in number_counts),
        numeric_dense_segments=sum(
            len(items) > MAX_NUMBERS_PER_SEGMENT for items in number_counts
        ),
        exact_duplicate_pairs=len(exact_pairs),
        fuzzy_duplicate_pairs=len(fuzzy_pairs),
    )


def validate_final_script(
    candidate: PaperScript,
    raw_script: PaperScript,
    audit: PaperScriptAudit,
) -> tuple[list[ScriptValidationIssue], ScriptValidationMetrics]:
    """Run deterministic structural, factual-risk and density checks."""
    issues = []
    raw_chapters = {chapter.chapter_id: chapter for chapter in raw_script.chapters}
    raw_plans = {chapter.chapter_id: chapter for chapter in raw_script.plan.chapters}
    visual_pages = {visual.id: visual.page for visual in raw_script.visuals}
    candidate_ids = [chapter.chapter_id for chapter in candidate.chapters]
    raw_ids = [chapter.chapter_id for chapter in raw_script.chapters]
    plan_ids = [chapter.chapter_id for chapter in candidate.plan.chapters]

    if len(candidate_ids) < 4:
        issues.append(ScriptValidationIssue(
            code="too_few_chapters",
            severity="high",
            message="最终稿少于四章，无法保持完整的论文解释结构。",
        ))
    if len(candidate_ids) != len(set(candidate_ids)):
        issues.append(ScriptValidationIssue(
            code="duplicate_chapter_id",
            severity="high",
            message="最终稿包含重复的 chapter_id。",
        ))
    if any(chapter_id not in raw_chapters for chapter_id in candidate_ids):
        issues.append(ScriptValidationIssue(
            code="unknown_chapter_id",
            severity="high",
            message="最终稿包含原始脚本中不存在的章节。",
        ))
    if candidate_ids != [item for item in raw_ids if item in candidate_ids]:
        issues.append(ScriptValidationIssue(
            code="chapter_order_changed",
            severity="high",
            message="最终稿章节顺序与原始叙事顺序不一致。",
        ))
    if plan_ids != candidate_ids:
        issues.append(ScriptValidationIssue(
            code="plan_chapter_mismatch",
            severity="high",
            message="最终稿计划章节与实际口播章节不一致。",
        ))

    allowed_numbers = [
        mention
        for _, _, text in _segment_records(raw_script)
        for mention in _number_mentions(text)
    ]
    allowed_numbers.extend(
        mention
        for text in _all_audit_text(audit)
        for mention in _number_mentions(text)
    )

    for chapter in candidate.chapters:
        raw_chapter = raw_chapters.get(chapter.chapter_id)
        chapter_plan = raw_plans.get(chapter.chapter_id)
        if raw_chapter is None or chapter_plan is None:
            continue
        if chapter.title != raw_chapter.title:
            issues.append(ScriptValidationIssue(
                code="chapter_title_changed",
                severity="medium",
                chapter_id=chapter.chapter_id,
                message="最终稿修改了章节标题。",
            ))
        allowed_pages = {
            segment.page for segment in raw_chapter.segments
        } | set(chapter_plan.source_pages)

        for segment_index, segment in enumerate(chapter.segments, start=1):
            if segment.page not in allowed_pages:
                issues.append(ScriptValidationIssue(
                    code="invalid_page",
                    severity="high",
                    chapter_id=chapter.chapter_id,
                    segment_index=segment_index,
                    message=f"segment 使用了不允许的 PDF 页码 {segment.page}。",
                ))

            if segment.visual_id is not None and (
                segment.visual_id not in visual_pages
                or visual_pages[segment.visual_id] != segment.page
            ):
                issues.append(ScriptValidationIssue(
                    code="invalid_visual_id",
                    severity="high",
                    chapter_id=chapter.chapter_id,
                    segment_index=segment_index,
                    message=(
                        "segment 使用了不存在或不在当前背景页的视觉元素 "
                        f"{segment.visual_id}。"
                    ),
                ))

            number_mentions = _number_mentions(segment.text)
            if len(number_mentions) > MAX_NUMBERS_PER_SEGMENT:
                issues.append(ScriptValidationIssue(
                    code="numeric_density",
                    severity="medium",
                    chapter_id=chapter.chapter_id,
                    segment_index=segment_index,
                    message=(
                        f"单段包含 {len(number_mentions)} 个数字，超过 "
                        f"{MAX_NUMBERS_PER_SEGMENT} 个的上限。"
                    ),
                ))

            unapproved = sorted({
                mention.raw
                for mention in number_mentions
                if not _number_is_allowed(mention, allowed_numbers)
            })
            if unapproved:
                issues.append(ScriptValidationIssue(
                    code="unapproved_number",
                    severity="high",
                    chapter_id=chapter.chapter_id,
                    segment_index=segment_index,
                    message=(
                        "编辑稿出现原始脚本和事实审核均未提供的数字: "
                        + "、".join(unapproved)
                    ),
                ))

            if len(segment.text) > MAX_SEGMENT_CHARACTERS:
                issues.append(ScriptValidationIssue(
                    code="segment_too_long",
                    severity="low",
                    chapter_id=chapter.chapter_id,
                    segment_index=segment_index,
                    message=(
                        f"单段包含 {len(segment.text)} 个字符，超过 "
                        f"{MAX_SEGMENT_CHARACTERS} 个字符。"
                    ),
                ))

    candidate_text_by_chapter = {
        chapter.chapter_id: [
            _normalize_text(segment.text) for segment in chapter.segments
        ]
        for chapter in candidate.chapters
    }
    for chapter_audit in audit.chapters:
        candidate_texts = candidate_text_by_chapter.get(
            chapter_audit.chapter_id,
            [],
        )
        for segment_audit in chapter_audit.segments:
            for claim in segment_audit.claims:
                if (
                    claim.verdict == "supported"
                    or claim.severity not in {"medium", "high"}
                ):
                    continue
                normalized_claim = _normalize_text(claim.claim)
                normalized_suggestion = _normalize_text(
                    claim.suggested_revision or ""
                )
                if normalized_suggestion and any(
                    normalized_suggestion in text for text in candidate_texts
                ):
                    continue
                if len(normalized_claim) >= 8 and any(
                    normalized_claim in text for text in candidate_texts
                ):
                    issues.append(ScriptValidationIssue(
                        code="audited_claim_retained",
                        severity=claim.severity,
                        chapter_id=chapter_audit.chapter_id,
                        message=(
                            f"审核标记为 {claim.verdict} 的问题陈述仍原样存在: "
                            f"{claim.claim}"
                        ),
                    ))

    exact_pairs, fuzzy_pairs = _duplicate_pairs(candidate)
    for left, right in exact_pairs[:20]:
        issues.append(ScriptValidationIssue(
            code="exact_duplicate",
            severity="medium",
            chapter_id=right[0],
            segment_index=right[1],
            message=(
                f"该段与 {left[0]} 的第 {left[1]} 段完全重复。"
            ),
        ))
    for left, right, similarity in fuzzy_pairs[:20]:
        issues.append(ScriptValidationIssue(
            code="fuzzy_duplicate",
            severity="medium",
            chapter_id=right[0],
            segment_index=right[1],
            message=(
                f"该段与 {left[0]} 的第 {left[1]} 段高度相似"
                f"（{similarity:.0%}）。"
            ),
        ))

    raw_characters = sum(
        len(text) for _, _, text in _segment_records(raw_script)
    )
    candidate_characters = sum(
        len(text) for _, _, text in _segment_records(candidate)
    )
    if raw_characters and candidate_characters > raw_characters * MAX_EXPANSION_RATIO:
        issues.append(ScriptValidationIssue(
            code="script_expanded",
            severity="medium",
            message=(
                f"编辑稿由 {raw_characters} 字增长到 {candidate_characters} 字，"
                "没有达到去重和精简目标。"
            ),
        ))

    return issues, _script_metrics(candidate)


def generate_repaired_script(
    candidate: PaperScript,
    raw_script: PaperScript,
    audit: PaperScriptAudit,
    issues: list[ScriptValidationIssue],
    max_attempts: int = 3,
) -> PaperScript:
    """Constrain the LLM to repair only deterministic validation failures."""
    candidate_ids = {chapter.chapter_id for chapter in candidate.chapters}
    values = {
        "candidate_script": candidate.model_dump_json(indent=2),
        "fact_audit": audit.model_dump_json(indent=2),
        "deterministic_issues": json.dumps(
            [issue.model_dump() for issue in issues],
            ensure_ascii=False,
            indent=2,
        ),
        "validation_feedback": "",
    }
    errors = []

    for attempt in range(1, max_attempts + 1):
        try:
            result = invoke_structured_with_retry(
                chain=_get_repair_chain(),
                values=values,
                stage="最终脚本返修",
                max_attempts=1,
            )
            repaired = _normalize_edited_script(result, raw_script)
            repaired_ids = {
                chapter.chapter_id for chapter in repaired.chapters
            }
            if not repaired_ids.issubset(candidate_ids):
                raise ValueError("返修稿新增了候选稿中不存在的章节")
            return repaired
        except Exception as exc:
            errors.append(f"第 {attempt} 次: {type(exc).__name__}: {exc}")
            values["validation_feedback"] = (
                "请修正下列错误并重新输出完整返修稿：\n"
                f"{type(exc).__name__}: {exc}"
            )
            if attempt < max_attempts:
                print(
                    "最终脚本返修结果不完整，"
                    f"正在重试 {attempt + 1}/{max_attempts}..."
                )

    raise RuntimeError(
        f"最终脚本连续 {max_attempts} 次返修失败\n"
        + "\n".join(errors)
    )


class ScriptFinalizationError(RuntimeError):
    """Raised when deterministic checks still fail after repair."""

    def __init__(self, report: ScriptValidationReport):
        self.report = report
        blocking_issues = [
            issue
            for issue in report.final_issues
            if issue.severity == "high"
        ]
        super().__init__(
            "最终脚本校验失败: "
            + "；".join(issue.message for issue in blocking_issues[:5])
        )


def finalize_script(
    edited_script: PaperScript,
    raw_script: PaperScript,
    audit: PaperScriptAudit,
    max_repair_rounds: int = 2,
) -> tuple[PaperScript, ScriptValidationReport]:
    """Validate, minimally repair when needed, and approve the final script."""
    initial_issues, initial_metrics = validate_final_script(
        edited_script,
        raw_script,
        audit,
    )
    candidate = edited_script
    final_issues = initial_issues
    final_metrics = initial_metrics
    repair_rounds = 0
    blocking_issues = [
        issue for issue in final_issues if issue.severity == "high"
    ]

    while blocking_issues and repair_rounds < max_repair_rounds:
        repair_rounds += 1
        print(
            f"最终校验发现 {len(blocking_issues)} 个高风险问题，"
            f"正在进行第 {repair_rounds}/{max_repair_rounds} 轮局部返修..."
        )
        candidate = generate_repaired_script(
            candidate,
            raw_script,
            audit,
            blocking_issues,
        )
        final_issues, final_metrics = validate_final_script(
            candidate,
            raw_script,
            audit,
        )
        blocking_issues = [
            issue for issue in final_issues if issue.severity == "high"
        ]

    report = ScriptValidationReport(
        passed=not blocking_issues,
        repair_rounds=repair_rounds,
        initial_metrics=initial_metrics,
        final_metrics=final_metrics,
        initial_issues=initial_issues,
        final_issues=final_issues,
    )
    if blocking_issues:
        raise ScriptFinalizationError(report)
    return candidate, report


def _canonical_sha256(data: object) -> str:
    serialized = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def build_final_cache_metadata(
    raw_script: PaperScript,
    audit: PaperScriptAudit,
    edited_script: PaperScript,
) -> dict:
    """Fingerprint all inputs that can change the approved final script."""
    inputs = {
        "raw_script_sha256": _canonical_sha256(raw_script.model_dump()),
        "script_audit_sha256": _canonical_sha256(audit.model_dump()),
        "edited_script_sha256": _canonical_sha256(edited_script.model_dump()),
        "finalizer_sha256": _canonical_sha256(finalizer_cache_material()),
    }
    return {
        "version": FINAL_CACHE_VERSION,
        "fingerprint": _canonical_sha256(inputs),
        "inputs": inputs,
    }


def _write_json_atomic(output_path: Path, data: dict) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(output_path)


def load_cached_finalization(
    script_path: str | Path,
    report_path: str | Path,
    cache_path: str | Path,
    expected_cache: dict,
) -> tuple[PaperScript, ScriptValidationReport] | None:
    script_path = Path(script_path)
    report_path = Path(report_path)
    cache_path = Path(cache_path)
    if not all(path.is_file() for path in (script_path, report_path, cache_path)):
        return None

    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if (
            cache.get("version") != FINAL_CACHE_VERSION
            or cache.get("fingerprint") != expected_cache["fingerprint"]
        ):
            return None
        script = PaperScript.model_validate_json(
            script_path.read_text(encoding="utf-8")
        )
        report = ScriptValidationReport.model_validate_json(
            report_path.read_text(encoding="utf-8")
        )
        if not report.passed:
            return None
        return script, report
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def save_finalization(
    script: PaperScript | None,
    report: ScriptValidationReport,
    script_path: str | Path,
    report_path: str | Path,
    cache_path: str | Path | None = None,
    cache_metadata: dict | None = None,
) -> None:
    if script is not None:
        _write_json_atomic(Path(script_path), script.model_dump())
    _write_json_atomic(Path(report_path), report.model_dump())
    if (
        script is not None
        and report.passed
        and cache_path is not None
        and cache_metadata is not None
    ):
        _write_json_atomic(Path(cache_path), cache_metadata)
