"""把通用 QHarness 与工作台侧的 PPT Skill 组合起来。"""

from __future__ import annotations

from pathlib import Path

_SKILL_ROOT = Path(__file__).parent / "skills" / "create-research-deck"
PRESENTATION_SKILL_CODE = "create-research-deck"


def install_presentation_skill(skill_catalog):
    """将工作台自带的 PPT Skill 同步到 QHarness 托管目录。"""
    return skill_catalog.import_skill(_SKILL_ROOT, update=True)


def load_presentation_skill(skill_catalog):
    """按固定 code 读取 PPT Skill，不受通用对话 Agent 的启停状态影响。"""
    install_presentation_skill(skill_catalog)

    return skill_catalog.load_fixed(PRESENTATION_SKILL_CODE)


def build_presentation_contract(
    *,
    task_id: str,
    objective: str,
    skill_catalog,
    output_path: str = "output/deck_spec.json",
):
    """创建只启用 PPT Skill 的任务契约，其他已安装 Skill 默认关闭。"""
    from qharness.loop import Criterion, TaskContract
    from qharness.skills import activate_skills

    contract = TaskContract(
        task_id=task_id,
        objective=f"{objective}\n将最终 SlideDeckSpec 写入 {output_path}。",
        criteria=(
            Criterion(id="C_SPEC", description="SlideDeckSpec 通过确定性结构校验"),
            Criterion(id="C_EVIDENCE", description="全部素材和引文引用均可解析到已声明来源"),
        ),
    )
    return activate_skills(contract, (load_presentation_skill(skill_catalog),))


def build_presentation_check_catalog(output_path: str = "output/deck_spec.json"):
    """为 QHarness Verifier 构建可信的 Deck Spec 校验目录。"""
    if any(character in output_path for character in ('"', "\r", "\n")):
        raise ValueError("output_path 不能包含双引号或换行")

    from qharness.loop import Scope
    from qharness.verification import CheckCatalog, CheckKind, CheckSpec

    check = CheckSpec(
        id="validate-slide-deck-spec",
        scope=Scope.TASK,
        targets=("C_SPEC", "C_EVIDENCE"),
        command=f'python -m research_presentation_core validate "{output_path}"',
        kind=CheckKind.COMMAND,
        inputs=(output_path,),
        failure_exit_codes=(1,),
    )
    return CheckCatalog((check,))
