"""把通用 QHarness 与工作台侧的 PPT Skill 组合起来。"""

from __future__ import annotations

from pathlib import Path

_SKILL_ROOT = Path(__file__).parent / "skills" / "create-presentation"
PRESENTATION_SKILL_CODE = "create-presentation"
# 保留导出名，兼容之前调用过 load_pptx_skill 的业务代码；实际只加载一个 Skill。
PPTX_SKILL_CODE = PRESENTATION_SKILL_CODE


def install_presentation_skills(skill_catalog):
    """将统一的演示文稿 Skill 同步到 QHarness 托管目录。"""
    return (skill_catalog.import_skill(_SKILL_ROOT, update=True),)


def load_presentation_skills(skill_catalog):
    """按固定 code 读取统一演示文稿 Skill，不受通用对话 Agent 的启停状态影响。"""
    install_presentation_skills(skill_catalog)
    return (skill_catalog.load_fixed(PRESENTATION_SKILL_CODE),)


def install_presentation_skill(skill_catalog):
    """同步统一演示文稿 Skill。"""
    return skill_catalog.import_skill(_SKILL_ROOT, update=True)


def load_presentation_skill(skill_catalog):
    """按固定 code 读取统一演示文稿 Skill。"""
    install_presentation_skill(skill_catalog)

    return skill_catalog.load_fixed(PRESENTATION_SKILL_CODE)


def load_pptx_skill(skill_catalog):
    """兼容旧接口：返回包含 Spec 设计和 PPTX 渲染两阶段的统一 Skill。"""
    install_presentation_skill(skill_catalog)
    return skill_catalog.load_fixed(PRESENTATION_SKILL_CODE)


def build_presentation_contract(
    *,
    task_id: str,
    objective: str,
    skill_catalog,
    include_pptx_skill: bool = False,
    output_path: str = "output/deck_spec.json",
    pptx_output_path: str | None = None,
):
    """创建演示文稿任务契约；可选择继续执行 PPTX 生成阶段。"""
    from qharness.loop import Criterion, TaskContract
    from qharness.skills import activate_skills

    objective_text = f"{objective}\n将最终 SlideDeckSpec 写入 {output_path}。"
    if include_pptx_skill:
        target = pptx_output_path or str(Path(output_path).with_suffix(".pptx"))
        objective_text += (
            f"\n先校验 SlideDeckSpec，再使用 PptxGenJS 将同一份规格渲染为可编辑 PPTX，"
            f"写入 {target}。"
        )
    criteria = [
        Criterion(id="C_SPEC", description="SlideDeckSpec 通过确定性结构校验"),
        Criterion(id="C_EVIDENCE", description="全部素材和引文引用均可解析到已声明来源"),
    ]
    if include_pptx_skill:
        criteria.append(
            Criterion(id="C_PPTX", description="生成的 PPTX 是包含至少一页幻灯片的 Open XML 文件")
        )
    contract = TaskContract(
        task_id=task_id,
        objective=objective_text,
        criteria=tuple(criteria),
    )
    skills = (
        load_presentation_skills(skill_catalog)
        if include_pptx_skill
        else (load_presentation_skill(skill_catalog),)
    )
    return activate_skills(contract, skills)


def build_presentation_check_catalog(
    output_path: str = "output/deck_spec.json",
    *,
    pptx_output_path: str | None = None,
    skill_root: str = ".qharness/skills/create-presentation",
):
    """为 QHarness Verifier 构建 Spec 及可选 PPTX 的可信校验目录。"""
    for label, value in (
        ("output_path", output_path),
        ("pptx_output_path", pptx_output_path),
        ("skill_root", skill_root),
    ):
        if value is not None and any(character in value for character in ('"', "\r", "\n")):
            raise ValueError(f"{label} 不能包含双引号或换行")

    from qharness.loop import Scope
    from qharness.verification import CheckCatalog, CheckKind, CheckSpec

    spec_command = f'python "{skill_root}/scripts/validate_spec.py" "{output_path}"'
    checks = [
        CheckSpec(
            id="validate-slide-deck-spec-todo",
            scope=Scope.TODO,
            targets=("C_SPEC", "C_EVIDENCE"),
            command=spec_command,
            kind=CheckKind.COMMAND,
            inputs=(output_path,),
            failure_exit_codes=(1, 2),
        ),
        CheckSpec(
            id="validate-slide-deck-spec",
            scope=Scope.TASK,
            targets=("C_SPEC", "C_EVIDENCE"),
            command=spec_command,
            kind=CheckKind.COMMAND,
            inputs=(output_path,),
            failure_exit_codes=(1, 2),
        ),
    ]
    if pptx_output_path is not None:
        pptx_command = f'python "{skill_root}/scripts/inspect_pptx.py" "{pptx_output_path}"'
        checks.extend(
            (
                CheckSpec(
                    id="inspect-generated-pptx-todo",
                    scope=Scope.TODO,
                    targets=("C_PPTX",),
                    command=pptx_command,
                    kind=CheckKind.COMMAND,
                    inputs=(pptx_output_path,),
                    failure_exit_codes=(1, 2),
                ),
                CheckSpec(
                    id="inspect-generated-pptx",
                    scope=Scope.TASK,
                    targets=("C_PPTX",),
                    command=pptx_command,
                    kind=CheckKind.COMMAND,
                    inputs=(pptx_output_path,),
                    failure_exit_codes=(1, 2),
                ),
            )
        )
    return CheckCatalog(tuple(checks))
