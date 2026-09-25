"""只聚合真实记录；环境阻塞、缺失用量和未审阅的路由不填成零。"""

from collections import Counter, defaultdict
import math
import statistics


def distribution(values):
    if not values:
        return {"n": 0, "min": None, "median": None, "p90": None, "p95": None, "max": None}
    values = sorted(values)
    return {"n": len(values), "min": values[0], "median": statistics.median(values),
            "p90": values[max(0, math.ceil(len(values)*.9)-1)],
            "p95": values[max(0, math.ceil(len(values)*.95)-1)], "max": values[-1]}


def passed(row):
    return row.get("oracle_status") == "pass" and row.get("budget_within_limits", True)


def consistency(rows, k):
    tasks = defaultdict(list)
    for row in rows:
        tasks[row["task"]].append(row)
    estimates = [math.comb(sum(passed(r) for r in group), k)/math.comb(len(group), k)
                 for group in tasks.values() if len(group) >= k]
    return {"k": k, "eligible_tasks": len(estimates), "pass_all_k": statistics.mean(estimates) if estimates else None}


def aggregate(records):
    groups = defaultdict(list)
    for row in records:
        groups[(row["split"], row["variant"])].append(row)
    output = []
    for (split, variant), rows in sorted(groups.items()):
        attempted = [r for r in rows if r["attempted"]]
        accepted = [r for r in attempted if passed(r)]
        claims = [r for r in attempted if r.get("phase") == "completed"]
        false_claims = [r for r in claims if r.get("oracle_status") == "fail"]
        unknown_claims = [r for r in claims if r.get("oracle_status") not in ("pass", "fail")]
        output.append({"split": split, "variant": variant, "scheduled": len(rows), "attempted": len(attempted),
            "blocked_before_attempt": len(rows)-len(attempted), "accepted": len(accepted),
            "acceptance_rate": len(accepted)/len(attempted) if attempted else None,
            "completion_claims": len(claims), "false_completion": len(false_claims),
            "false_completion_rate": len(false_claims)/len(claims) if claims else None,
            "false_completion_per_attempt": len(false_claims)/len(attempted) if attempted else None,
            "unverified_completion": len(unknown_claims),
            "pass_k": [consistency([r for r in attempted if "task" in r],k) for k in (2,3)],
            "seconds": distribution([r["seconds"] for r in attempted]),
            "accepted_seconds": distribution([r["seconds"] for r in accepted]),
            "other_seconds": distribution([r["seconds"] for r in attempted if not passed(r)]),
            "model_attempts": distribution([r["budget"]["model_attempts"] for r in attempted if r.get("budget")]),
            "tokens": distribution([r["budget"]["used_tokens"] for r in attempted if r.get("budget")]),
            "estimated_model_cost": distribution([r["model_cost"] for r in attempted if r.get("model_cost") is not None]),
            "routes": dict(sum((Counter(r.get("routes", {})) for r in attempted), Counter())),
            "route_quality": None, "note": "路由质量需人工标注，次数不等于正确率；微型任务不是生产收益证明。"})
    return output


def markdown(document):
    def rate(value):
        return "未测" if value is None else f"{value:.1%}"
    lines = ["# QHarness 评测记录", "", f"批次：`{document['batch_id']}`；数据集：`{document['manifest']['suite_version']}`。",
             "", f"状态：**{document['status']}**。", "",
             "| 集合 | 策略 | 计划/实际尝试 | 独立验收通过 | 通过率 | 误完成/完成声明 |",
             "|---|---|---:|---:|---:|---:|"]
    for row in aggregate(document["results"]):
        lines.append(f"| {row['split']} | {row['variant']} | {row['scheduled']}/{row['attempted']} | {row['accepted']} | {rate(row['acceptance_rate'])} | {row['false_completion']}/{row['completion_claims']} |")
    lines.extend(["", "环境阻塞不计作模型失败；未运行不显示为 0% 或 100%。误完成仅指运行声明 completed 而独立断言明确失败；未取得验收结论另记。",
                  "", "详细 JSON 包含每次运行、共享预算、角色调用、路由、返工、暂停触发、未知调用及独立验收原始输出。没有价格或完整用量时费用为 null。",
                  "", "本任务集为同源小型代码任务，保留集只冻结实例，不代表未知仓库泛化；路由质量和 Profile 选择需要真实运行及审阅。"])
    if document.get("preflight"):
        lines.extend(["", "预检：" + document["preflight"]["message"]])
    return "\n".join(lines) + "\n"
