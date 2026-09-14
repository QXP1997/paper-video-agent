"""复用验证凭据派生有效知识、缺口和调查停滞；不以调用数/文件数度量进展。"""

from __future__ import annotations

import asyncio
import unicodedata

from qharness.exception import LoopExecutionError
from qharness.loop.models import CheckStatus, ProgressReport, Scope, StageKind
from qharness.loop.repository import digest


def source_fingerprint(spec):
    # 忽略模型可改写的标题、目标、ID、版本；换个名字不等于换了信息来源。
    return digest([spec.command, spec.cwd, spec.kind, spec.inputs])


def check_addresses(spec):
    return set(spec.addresses) | {c.question_id for c in spec.conclusions}


def fact_key(kind, finding):
    normalized = " ".join(unicodedata.normalize("NFKC", finding.finding).casefold().split())
    return kind, finding.ref, finding.fact_id or normalized


class ProgressTracker:
    def __init__(self, runner):
        self.runner, self.repository = runner, runner.repository

    async def view(self, state, todo_id, *, specs=None):
        todo = next(t.todo for t in state.todos if t.todo.id == todo_id)
        criteria = set(todo.acceptance_refs)
        questions = {q.id for q in state.questions if set(q.acceptance_refs) & criteria}
        attempts = [a for a in state.attempts if a.plan.todo_id == todo_id or set(a.plan.addresses) & (criteria | questions) or
                    any(set(v.remaining_gaps) & (criteria | questions) for v in a.verdicts)]
        refs = {ref for a in attempts for v in a.verdicts for ref in v.evidence_refs}
        refs.update(ref for link in state.satisfied_criteria for ref in link.evidence_refs)
        records, valid, stale = {}, set(), set()
        definitions = {s.id: s for s in specs} if specs is not None else None
        for ref in sorted(refs):
            try:
                evidence = await asyncio.to_thread(self.runner.read, ref)
            except (LoopExecutionError, ValueError) as error:
                if isinstance(error, LoopExecutionError) and error.code not in {"not_found", "invalid_evidence"}:
                    raise
                stale.add(ref)
                continue
            records[ref] = evidence
            definition = definitions.get(evidence.spec.id) if definitions is not None else evidence.spec
            if definition is not None and await self.runner.current(evidence, definition):
                valid.add(ref)
            else:
                stale.add(ref)

        # 同一检查后来明确失败时，旧 PASS 不能继续作为有效知识。ERROR 不否定业务事实。
        latest = {}
        for attempt in attempts:
            for verdict in attempt.verdicts:
                for ref in verdict.evidence_refs:
                    if ref in valid and records[ref].status in (CheckStatus.PASS, CheckStatus.FAIL):
                        latest[records[ref].spec.id] = records[ref]
        superseded = {ref for ref in valid if records[ref].status == CheckStatus.PASS and
                      records[ref].spec.id in latest and latest[records[ref].spec.id].status == CheckStatus.FAIL}
        valid -= superseded
        stale |= superseded

        def proven_finding(kind, finding):
            return bool(finding.evidence_refs) and all(
                ref in records and records[ref].status == CheckStatus.PASS and any(
                    c.question_id == finding.ref and c.finding == finding.finding and c.fact_id == finding.fact_id
                    and c.kind == kind for c in records[ref].spec.conclusions)
                for ref in finding.evidence_refs)

        seen, retained, retained_refs = set(), {}, set()
        seen_criteria, novel_refs = set(), ()
        stalled, sources = 0, set()
        for attempt in attempts:
            gained = set()
            for verdict in attempt.verdicts:
                for link in verdict.progress.regressed_criteria:
                    seen_criteria.discard(link.ref)
                for link in verdict.progress.satisfied_criteria:
                    if link.ref in criteria and all(ref in records and records[ref].status == CheckStatus.PASS
                        and records[ref].spec.scope == Scope.TODO and link.ref in records[ref].spec.targets
                        for ref in link.evidence_refs):
                        if link.ref not in seen_criteria:
                            gained.add(link.ref)
                        seen_criteria.add(link.ref)
                for kind, findings in (("resolved", verdict.progress.resolved_questions),
                                       ("narrowed", verdict.progress.narrowed_questions),
                                       ("eliminated", verdict.progress.eliminated_hypotheses)):
                    for finding in findings:
                        if finding.ref not in questions or not proven_finding(kind, finding):
                            continue
                        key = fact_key(kind, finding)
                        if key not in seen:
                            gained.add(finding.ref)
                            seen.add(key)
                        if set(finding.evidence_refs) <= valid:
                            retained[key] = finding
                            retained_refs.update(finding.evidence_refs)
            if not attempt.verdicts:
                continue  # 在途工具、分页和轮询尚未交回，不进入停滞统计。
            last = attempt.verdicts[-1]
            observable = [records[r] for r in last.evidence_refs if r in records and
                          records[r].spec.scope == Scope.STAGE and records[r].status == CheckStatus.PASS
                          and records[r].workspace_before == records[r].workspace_after
                          and records[r].environment_before == records[r].environment_after]
            if attempt.plan.kind != StageKind.INVESTIGATE or gained:
                stalled, sources = 0, set()
            elif last.stage_status == CheckStatus.PASS and last.todo_status != CheckStatus.PASS and observable:
                stalled += 1
                sources.update(source_fingerprint(e.spec) for e in observable)
            # ERROR/INCONCLUSIVE 不证明空转；不把检查故障或未完整获取结果当成调查失败。
            novel_refs = tuple(sorted(gained))
        satisfied = {link.ref for link in state.satisfied_criteria if link.ref in criteria and
                     set(link.evidence_refs) <= valid and all(records[r].status == CheckStatus.PASS for r in link.evidence_refs)}
        resolved = {key[1] for key in retained if key[0] == "resolved"}
        return ProgressReport(todo_id=todo_id, unresolved_criteria=tuple(r for r in todo.acceptance_refs if r not in satisfied),
            unresolved_questions=tuple(q.id for q in state.questions if q.id in questions - resolved),
            retained_findings=tuple(retained.values()), retained_evidence=tuple(sorted(retained_refs)),
            stale_evidence=tuple(sorted(stale)), novel_refs=novel_refs, stalled_investigations=stalled,
            repeated_sources=tuple(sorted(sources)))
