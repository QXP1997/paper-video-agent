import hashlib
import json
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
from paper_video_agent.models import (
    ChapterFactAudit,
    PaperScript,
    PaperScriptAudit,
    ScriptAuditSummary,
    VideoChapterPlan,
    VideoChapterScript,
)

AUDIT_GENERATION_VERSION = 1
AUDIT_CACHE_VERSION = 1


audit_prompt = ChatPromptTemplate.from_messages([
    (
        "system",
        """
你是一名严谨、保守的论文解说事实审核员。你的唯一任务，是检查视频口播对论文的陈述是否得到指定论文页面支持。你不评价文风、篇幅、选题价值或讲解是否足够精彩，也不重写整篇脚本。

# 一、封闭证据范围

paper_evidence 只包含 current_chapter.source_pages 对应的页面，它们是本次审核允许使用的全部证据：

- 只能依据 paper_evidence，不得使用外部知识、常识记忆或未提供的论文页面补证；
- 不得根据关键词猜测其他页面可能写了什么；
- evidence_pages 必须是 current_chapter.source_pages 的子集；
- 如果这些页面不足以支持口播，即使陈述可能在论文其他页面成立，也应判为 unsupported，并说明“指定来源页中没有证据”；
- 页面文本可能来自 PDF 自动提取。若公式、表格或排版损坏到无法可靠判断，应判为 not_verifiable，不要猜测。

# 二、逐段覆盖与原子化

必须对 narration_chapter 中的每个 segment 恰好输出一个 SegmentFactAudit，segment_index 与输入完全一致、从 1 连续编号，不得遗漏、合并或新增。

对每个 segment：

1. 将涉及论文的复合陈述拆成能够独立判断真假的原子 claim；
2. 核验方法、数据、实验设置、数值、比较、结论、作者动机、局限和因果解释；
3. 纯粹的过渡句、问句、修辞或不冒充论文事实的教学类比不必制造 claim，claims 输出空数组，并在 notes 中简要说明；
4. 不要因为一句话没有数字就跳过，也不要把多个数字和条件塞进同一个 claim；
5. claim 应准确复述待核验内容，不要在 claim 中先替原稿修正。

# 三、结论标准

- supported：指定页面明确支持该陈述，关键对象、条件、指标、方向和量级一致；
- partially_supported：核心意思有依据，但遗漏了会影响理解的条件、范围、比较对象或限定语；
- unsupported：指定页面没有依据，或者页面内容与陈述不符；
- conflicting：指定页面内部存在无法直接消解的不同数字或说法；
- not_verifiable：由于页面提取缺失、公式或表格损坏等原因，无法可靠判断。

evidence_type：

- direct：原文直接陈述或表格直接列出；
- derived：可由指定页中的明确数字或定义复算得到；
- contextual：不是逐字陈述，但由指定页的上下文清楚支持；
- none：没有可用证据或无法核验。

derived 必须在 evidence 中写清楚计算所用的数值与关系。不要把你自己的推断标成论文直接结论。

# 四、重点风险

审核数字时同时检查数值、单位、百分比与百分点、模型或方法、数据集与划分、基线、实验条件和统计口径。四舍五入只有在不改变结论时才可接受。

审核文字结论时保留原文的谨慎程度。原文中的 may、suggest、indicate、associated with 等措辞，不能被口播升级为“证明”“必然”“导致”。相关性、消融结果或作者解释不能自动当作严格因果证据。

教学解释可以比原文更通俗，但不能新增论文没有的方法机制、作者意图、能力边界或普遍化结论。论文自己的不同位置发生冲突时标记 conflicting，不替作者静默选择一个版本。

# 五、字段填写

- evidence：简短说明支持、反驳或无法核验的依据，并指出关键条件；不要大段复制原文；
- issue：只有存在事实风险时填写，具体说明缺了什么或错在哪里；
- severity：无问题为 none；不影响主旨的小瑕疵为 low；可能造成明显误解为 medium；核心方法或主要结论错误为 high；
- suggested_revision：只给最小必要修正，不进行风格润色；没有问题时为 null；
- supported 的 severity 必须为 none，issue 和 suggested_revision 必须为 null；
- unsupported 或 not_verifiable 的 evidence_type 必须为 none。

这套规则适用于模型、算法、Agent、系统、数据、评测、理论和应用等不同类型的论文，不要预设固定研究范式。
""",
    ),
    (
        "human",
        """
请审核下面这一完整视频章节。

整期视频与当前章节上下文：

{script_context}

当前章节规划：

{current_chapter}

待审核口播章节（segment_index 是审核定位编号）：

{narration_chapter}

本次允许使用的全部论文证据页：

{paper_evidence}

上一次输出的结构或约束错误（首次调用为空）：

{validation_feedback}
""",
    ),
])


@lru_cache(maxsize=1)
def _get_audit_chain():
    return audit_prompt | get_llm().with_structured_output(
        ChapterFactAudit,
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


def audit_generation_cache_material() -> dict:
    """Return deterministic, non-secret inputs that affect audit output."""
    return {
        "generation_version": AUDIT_GENERATION_VERSION,
        "llm": {
            "model": LLM_MODEL,
            "base_url": LLM_BASE_URL,
            "temperature": LLM_TEMPERATURE,
            "max_tokens": LLM_MAX_TOKENS,
            "extra_body": LLM_EXTRA_BODY,
        },
        "prompts": {
            "fact_audit": _prompt_messages(audit_prompt),
        },
        "schemas": {
            "chapter_fact_audit": ChapterFactAudit.model_json_schema(),
            "paper_script_audit": PaperScriptAudit.model_json_schema(),
        },
    }


def select_source_page_content(
    pages: list[dict],
    source_pages: list[int],
) -> list[dict]:
    """Return only requested PDF pages, preserving source_pages order."""
    page_by_number = {
        int(page["page"]): {
            "page": int(page["page"]),
            "text": str(page.get("text", "")),
        }
        for page in pages
    }
    missing_pages = [page for page in source_pages if page not in page_by_number]
    if missing_pages:
        raise ValueError(f"审核来源页不存在: {missing_pages}")

    return [page_by_number[page] for page in source_pages]


def _normalize_chapter_audit(
    audit: ChapterFactAudit,
    chapter: VideoChapterScript,
    chapter_plan: VideoChapterPlan,
) -> ChapterFactAudit:
    expected_indices = list(range(1, len(chapter.segments) + 1))
    actual_indices = [segment.segment_index for segment in audit.segments]
    if actual_indices != expected_indices:
        raise ValueError(
            "segment 审核必须按顺序完整覆盖本章: "
            f"expected={expected_indices}, actual={actual_indices}"
        )

    allowed_pages = set(chapter_plan.source_pages)
    invalid_evidence_pages = sorted({
        page
        for segment in audit.segments
        for claim in segment.claims
        for page in claim.evidence_pages
        if page not in allowed_pages
    })
    if invalid_evidence_pages:
        raise ValueError(
            f"审核引用了 source_pages 之外的页码: {invalid_evidence_pages}"
        )

    for segment in audit.segments:
        for claim in segment.claims:
            if claim.verdict == "supported" and (
                claim.severity != "none"
                or claim.issue is not None
                or claim.suggested_revision is not None
            ):
                raise ValueError("supported claim 不能同时包含问题或修改建议")
            if claim.verdict in {
                "supported",
                "partially_supported",
                "conflicting",
            } and not claim.evidence_pages:
                raise ValueError(
                    f"{claim.verdict} claim 必须标明实际证据页"
                )
            if claim.verdict != "supported" and (
                claim.severity == "none" or claim.issue is None
            ):
                raise ValueError(
                    f"{claim.verdict} claim 必须说明问题及其严重程度"
                )
            if claim.verdict in {"unsupported", "not_verifiable"} and (
                claim.evidence_type != "none"
            ):
                raise ValueError(
                    f"{claim.verdict} claim 的 evidence_type 必须为 none"
                )

    return audit.model_copy(update={
        "chapter_id": chapter.chapter_id,
        "source_pages": list(chapter_plan.source_pages),
    })


def generate_chapter_fact_audit(
    script: PaperScript,
    chapter: VideoChapterScript,
    chapter_plan: VideoChapterPlan,
    evidence_pages: list[dict],
    max_attempts: int = 3,
) -> ChapterFactAudit:
    """Audit one complete chapter against only its planned source pages."""
    narration_chapter = {
        "chapter_id": chapter.chapter_id,
        "title": chapter.title,
        "segments": [
            {
                "segment_index": index,
                "page": segment.page,
                "text": segment.text,
            }
            for index, segment in enumerate(chapter.segments, start=1)
        ],
    }
    values = {
        "script_context": json.dumps(
            {
                "title": script.title,
                "central_question": script.plan.central_question,
                "core_message": script.plan.core_message,
            },
            ensure_ascii=False,
            indent=2,
        ),
        "current_chapter": chapter_plan.model_dump_json(indent=2),
        "narration_chapter": json.dumps(
            narration_chapter,
            ensure_ascii=False,
            indent=2,
        ),
        "paper_evidence": json.dumps(
            evidence_pages,
            ensure_ascii=False,
            indent=2,
        ),
        "validation_feedback": "",
    }

    errors = []
    for attempt in range(1, max_attempts + 1):
        try:
            audit = invoke_structured_with_retry(
                chain=_get_audit_chain(),
                values=values,
                stage=f"章节“{chapter.title}”事实审核",
                max_attempts=1,
            )
            return _normalize_chapter_audit(audit, chapter, chapter_plan)
        except Exception as exc:
            errors.append(f"第 {attempt} 次: {type(exc).__name__}: {exc}")
            values["validation_feedback"] = (
                "请修正下列错误并重新输出完整审核结果：\n"
                f"{type(exc).__name__}: {exc}"
            )
            if attempt < max_attempts:
                print(
                    f"章节“{chapter.title}”审核结果不完整，"
                    f"正在重试 {attempt + 1}/{max_attempts}..."
                )

    raise RuntimeError(
        f"章节“{chapter.title}”连续 {max_attempts} 次审核失败\n"
        + "\n".join(errors)
    )


def _build_summary(chapters: list[ChapterFactAudit]) -> ScriptAuditSummary:
    claims = [
        claim
        for chapter in chapters
        for segment in chapter.segments
        for claim in segment.claims
    ]
    verdict_counts = {
        verdict: sum(claim.verdict == verdict for claim in claims)
        for verdict in (
            "supported",
            "partially_supported",
            "unsupported",
            "conflicting",
            "not_verifiable",
        )
    }
    return ScriptAuditSummary(
        total_segments=sum(len(chapter.segments) for chapter in chapters),
        total_claims=len(claims),
        supported=verdict_counts["supported"],
        partially_supported=verdict_counts["partially_supported"],
        unsupported=verdict_counts["unsupported"],
        conflicting=verdict_counts["conflicting"],
        not_verifiable=verdict_counts["not_verifiable"],
        high_severity_issues=sum(
            claim.severity == "high" for claim in claims
        ),
    )


def generate_script_fact_audit(
    pages: list[dict],
    script: PaperScript,
) -> PaperScriptAudit:
    """Audit every chapter without reading outside its source_pages."""
    plans_by_id = {
        chapter.chapter_id: chapter
        for chapter in script.plan.chapters
    }
    audited_chapters = []

    for index, chapter in enumerate(script.chapters, start=1):
        chapter_plan = plans_by_id.get(chapter.chapter_id)
        if chapter_plan is None:
            raise ValueError(f"脚本章节缺少对应规划: {chapter.chapter_id}")

        evidence_pages = select_source_page_content(
            pages,
            chapter_plan.source_pages,
        )
        print(
            f"正在审核视频章节 {index}/{len(script.chapters)}: "
            f"{chapter.title}（来源页 {chapter_plan.source_pages}）"
        )
        audited_chapters.append(generate_chapter_fact_audit(
            script=script,
            chapter=chapter,
            chapter_plan=chapter_plan,
            evidence_pages=evidence_pages,
        ))

    return PaperScriptAudit(
        script_title=script.title,
        chapters=audited_chapters,
        summary=_build_summary(audited_chapters),
    )


def _canonical_sha256(data: object) -> str:
    serialized = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def build_audit_cache_metadata(
    pages: list[dict],
    script: PaperScript,
) -> dict:
    """Fingerprint only inputs that can change ``script_audit.json``."""
    source_page_numbers = list(dict.fromkeys(
        page
        for chapter in script.plan.chapters
        for page in chapter.source_pages
    ))
    evidence_pages = select_source_page_content(pages, source_page_numbers)
    inputs = {
        "paper_script_sha256": _canonical_sha256(script.model_dump()),
        "source_pages_sha256": _canonical_sha256(evidence_pages),
        "audit_generation_sha256": _canonical_sha256(
            audit_generation_cache_material()
        ),
    }
    return {
        "version": AUDIT_CACHE_VERSION,
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


def load_cached_script_audit(
    audit_path: str | Path,
    cache_path: str | Path,
    expected_cache: dict,
) -> PaperScriptAudit | None:
    audit_path = Path(audit_path)
    cache_path = Path(cache_path)
    if not audit_path.is_file() or not cache_path.is_file():
        return None

    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if (
            cache.get("version") != AUDIT_CACHE_VERSION
            or cache.get("fingerprint") != expected_cache["fingerprint"]
        ):
            return None
        return PaperScriptAudit.model_validate_json(
            audit_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def save_script_audit(
    audit: PaperScriptAudit,
    output_path: str | Path,
    cache_path: str | Path | None = None,
    cache_metadata: dict | None = None,
) -> None:
    _write_json_atomic(Path(output_path), audit.model_dump())
    if cache_path is not None and cache_metadata is not None:
        _write_json_atomic(Path(cache_path), cache_metadata)
