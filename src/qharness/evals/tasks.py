"""版本化小型代码任务。在线检查与独立终态检查由应用生成，不存入可写工作区。"""

import base64
from dataclasses import asdict, dataclass
import json

from qharness.loop.models import Criterion, TaskContract, Todo, TodoPlan
from qharness.loop.repository import digest
from qharness.verification import CheckCatalog, CheckSpec


SUITE_VERSION = "coding-micro-v1"
SPLITS = ("dev", "regression", "heldout")
CATEGORIES = ("explicit", "investigation", "cross_module", "environment", "steering", "long_running")
IMMUTABLE = "任务中的现有公开数据文件不可修改"
STEERING = "分页参数为零或负数时必须抛出 ValueError"


@dataclass(frozen=True)
class Task:
    id: str
    split: str
    category: str
    objective: str
    files: dict[str, str]
    requirements: tuple[str, ...]
    assertions: tuple[str, ...]
    terminal_assertions: tuple[str, ...]
    protected: tuple[str, ...] = ("public_data.json",)

    @property
    def fingerprint(self):
        return digest([SUITE_VERSION, asdict(self)])

    def contract(self, run_id):
        return TaskContract(task_id=run_id, objective=self.objective,
            criteria=tuple(Criterion(id=f"C{i+1}", description=r) for i, r in enumerate(self.requirements)),
            constraints=(IMMUTABLE,))

    def todos(self, *, single=False):
        if single:
            return TodoPlan(todos=(Todo(id="T1", objective=self.objective,
                acceptance_refs=tuple(f"C{i+1}" for i in range(len(self.requirements))),
                done_when=self.requirements),))
        return TodoPlan(todos=tuple(Todo(id=f"T{i+1}", objective=r, acceptance_refs=(f"C{i+1}",),
            done_when=(r,), dependencies=(f"T{i}",) if i else ()) for i, r in enumerate(self.requirements)))

    def oracle(self, *, index=None, terminal=False, steered=False, marker="QHARNESS_CHECK_OK"):
        assertions = self.terminal_assertions if terminal else self.assertions
        selected = assertions if index is None else (assertions[index],)
        # -I prevents cwd sitecustomize/PYTHONPATH startup; imports are then explicitly grounded to the workspace.
        lines = ["import sys, json, pathlib", "sys.path.insert(0, str(pathlib.Path.cwd()))"]
        for name in self.protected:
            lines.append(f"assert pathlib.Path({name!r}).read_text(encoding='utf-8') == {self.files[name]!r}, 'protected file changed'")
        lines.extend(selected)
        if steered:
            lines.extend(["from pagination import paginate", "for p, s in [(0,2),(-1,2),(1,0),(1,-1)]:",
                          "    try: paginate([1,2],p,s)", "    except ValueError: pass",
                          "    else: raise AssertionError('invalid pagination arguments accepted')"])
        lines.append(f"print({marker!r})")
        return "\n".join(lines) + "\n"

    def catalog(self, *, steered=False):
        specs = []
        for i, label in enumerate(self.requirements):
            command = command_for(self.oracle(index=i))
            specs.extend((CheckSpec(id=f"stage-{i+1}", scope="stage", targets=(label,), command=command,
                                   addresses=(f"C{i+1}",), failure_exit_codes=(1,)),
                          CheckSpec(id=f"todo-{i+1}", scope="todo", targets=(f"C{i+1}", label),
                                    command=command, failure_exit_codes=(1,))))
        specs.append(CheckSpec(id="task-all", scope="task",
            targets=tuple(f"C{i+1}" for i in range(len(self.requirements))) + (IMMUTABLE,) + ((STEERING,) if steered else ()),
            command=command_for(self.oracle(steered=steered)), failure_exit_codes=(1,)))
        return CheckCatalog(tuple(specs))


def command_for(source):
    encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
    return f'python -I -B -c "import base64;exec(compile(base64.b64decode(\'{encoded}\'),\'<qharness-check>\',\'exec\'))"'


def suite():
    """三个 split 使用不同输入实例；同源任务族，不宣称跨仓库泛化或私有基准。"""
    tasks = []
    for split_index, split in enumerate(SPLITS):
        values = list(range(10 + split_index * 3))
        public = json.dumps(values)
        for category in CATEGORIES:
            files = {"public_data.json": public}
            if category in ("explicit", "steering", "investigation"):
                files["pagination.py"] = ("def paginate(items, page, size):\n"
                    + ("    items = sorted(items)\n    start = (page - 1) * size\n" if category == "investigation"
                       else "    start = page * size\n") + "    return items[start:start + size]\n")
                requirement = "分页从第一页开始，保留输入顺序并正确返回尾页和空页"
                online = "from pagination import paginate\nassert paginate([7,3,9,1,8],1,2)==[7,3]\nassert paginate([7,3,9,1,8],3,2)==[8]"
                terminal = f"from pagination import paginate\nvalues={list(reversed(values))!r}\nfor size in (1,2,4,20):\n    for page in range(1,16):\n        assert paginate(values,page,size)==values[(page-1)*size:page*size]\nassert paginate([],1,3)==[]"
                objective = ("调查分页结果顺序与调用方预期不符的原因并修复。" if category == "investigation" else "修复分页起始偏移和边界。")
                task = Task(f"{split}-{category}", split, category, objective + "保持 paginate(items,page,size) 接口；阅读并保留 public_data.json。",
                            files, (requirement,), (online,), (terminal,))
            elif category == "environment":
                files.update({"settings.json": '{"data_file":"missing.json"}',
                              "loader.py": "import json\nfrom pathlib import Path\ndef load():\n    config=json.loads(Path('settings.json').read_text())\n    return json.loads(Path(config['data_file']).read_text())\n"})
                assertion = f"from loader import load\nassert load()=={values!r}"
                task = Task(f"{split}-{category}", split, category,
                    "修复配置中的数据路径使 loader.load() 返回 public_data.json；不可硬编码结果或改 loader.py。",
                    files, ("正确配置数据来源并恢复加载",), (assertion,),
                    (assertion + "\nassert json.loads(pathlib.Path('settings.json').read_text())['data_file']=='public_data.json'",),
                    ("public_data.json", "loader.py"))
            else:
                count = 3 if category == "cross_module" else 8
                requirements, assertions, terminal_assertions = [], [], []
                # Composition makes a local fix insufficient: each independent criterion checks an actual pipeline prefix.
                for i in range(count):
                    files[f"step{i}.py"] = f"def transform(value):\n    return value + {i}\n"
                    imports = "\n".join(f"from step{j} import transform as f{j}" for j in range(i + 1))
                    expr = "x"
                    for j in range(i + 1):
                        expr = f"f{j}({expr})"
                    expected = sum(range(1, i + 2))
                    requirements.append(f"管线前 {i+1} 层累计增加 {expected}，保持各层接口")
                    assertions.append(imports + f"\nx=2\nassert {expr}==x+{expected}")
                    terminal_assertions.append(imports + f"\nfor x in {[-5-split_index,0,7+split_index,100]!r}:\n    assert {expr}==x+{expected}\n    assert f{i}(x)==x+{i+1}")
                task = Task(f"{split}-{category}", split, category,
                    f"修复 {count} 层管线：step0.transform 加 1、step1 加 2，以此类推。每层必须支持任意整数，保持接口和公开数据。",
                    files, tuple(requirements), tuple(assertions), tuple(terminal_assertions))
            tasks.append(task)
    return tuple(tasks)
