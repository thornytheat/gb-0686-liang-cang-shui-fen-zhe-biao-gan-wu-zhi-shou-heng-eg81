"""仓容分配口径（跨批次累计）。

口径：按到车顺序（原始行号）逐车落仓；本仓优先，只允许最多拆成一个第二仓。
- 本仓余量能全放下：整车入本仓，不拆。
- 本仓放不下：先占满本仓余量，剩余整段落进「另一个」仓，且选余量最小又能整段放下的仓
  （best-fit，少留碎片）。一段以上不再继续拆，保证一车最多两段、可追溯。
- 除本仓外没有单仓能整段容纳剩余部分：整车暂缓。暂缓车不占用任何仓容，不写入正式仓账，
  仍是已采信车次，只是本批不卸。
每段按干物质占比拆净重/折标重/金额，第二段用整车减第一段，拆分段相加恒等于整车。
"""
import json
from decimal import Decimal as D, ROUND_HALF_UP
from app.errors import DomainError
from app.normalize import number

CENT = D(".01")


def capacities(value):
    """仓容参数：接受 {仓号: kg}、"仓1=12000,仓2=11000" 或 JSON 对象字符串，单位 kg 干物质。"""
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            decoded = json.loads(value)
        except ValueError:
            raise DomainError("CAPACITY", "仓容 JSON 格式错误")
        return capacities(decoded)
    if isinstance(value, dict):
        pairs = [(str(k).strip(), v) for k, v in value.items()]
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise DomainError("CAPACITY", "未配置仓容")
        pairs = []
        for chunk in text.replace(";", ",").replace("；", ",").replace("，", ",").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "=" not in chunk:
                raise DomainError("CAPACITY", "仓容格式应为 仓号=kg")
            name, raw = chunk.split("=", 1)
            pairs.append((name.strip(), raw.strip()))
    else:
        raise DomainError("CAPACITY", "仓容参数格式错误")
    result = {}
    for name, raw in pairs:
        if not name:
            raise DomainError("CAPACITY", "仓号不能为空")
        if name in result:
            raise DomainError("CAPACITY", f"仓容重复配置: {name}")
        amount = number(raw)
        if amount < 0:
            raise DomainError("CAPACITY", f"仓容不能为负: {name}")
        result[name] = amount
    return result


def _split_value(total, part, whole):
    if whole == 0:
        return D(0), total
    first = total * part / whole
    return first, total - first


def _split_amount(total, part, whole):
    if whole == 0:
        return D(0), total
    first = (total * part / whole).quantize(CENT, rounding=ROUND_HALF_UP)
    if first < 0:
        first = D(0)
    if first > total:
        first = total
    return first, total - first


def allocate(rows, capacities_map, opening=None):
    """rows: 已裁决并结算的车次；capacities_map: {仓号: 总容量 kg}；
    opening: {仓号: 本批之前已占 kg}（缺省为 0）。返回本批分配账与暂缓车。"""
    opening = opening or {}
    caps = {str(name): D(str(value)) for name, value in capacities_map.items()}
    before = {name: D(str(opening.get(name, 0))) for name in caps}
    for name, used in before.items():
        if used < 0:
            raise DomainError("CAPACITY", f"仓 {name} 已占为负")
        if used > caps[name]:
            raise DomainError("CAPACITY", f"仓 {name} 账面已超容")
    remaining = {name: caps[name] - before[name] for name in caps}
    entries, deferred = [], []
    completed = set()
    order = sorted(rows, key=lambda r: (r.get("_line", 0), str(r.get("id", ""))))

    for row in order:
        truck = str(row.get("id", ""))
        declared = str(row.get("bin", ""))
        dry = D(str(row["dry_kg"]))
        if dry < 0:
            raise DomainError("BALANCE", f"车次 {truck} 干物质为负")
        if declared not in remaining:
            deferred.append({
                "truck_id": truck, "line": row.get("_line", ""), "declared_bin": declared,
                "net_kg": row["net_kg"], "dry_kg": dry, "standard_kg": row["standard_kg"],
                "code": "CAPACITY", "reason": "申报仓未配置仓容",
            })
            continue

        first = min(dry, max(D(0), remaining[declared]))
        left = dry - first
        target = None
        if left > 0:
            candidates = [name for name, free in remaining.items()
                          if name != declared and free >= left]
            if candidates:
                # best-fit：余量最小且放得下；同余量按仓号稳定排序。
                target = min(candidates, key=lambda name: (remaining[name], name))
        if left > 0 and target is None:
            deferred.append({
                "truck_id": truck, "line": row.get("_line", ""), "declared_bin": declared,
                "net_kg": row["net_kg"], "dry_kg": dry, "standard_kg": row["standard_kg"],
                "code": "CAPACITY", "reason": "本仓余量不足，且无其他单仓可整段容纳",
            })
            continue

        plan = []
        if first > 0:
            plan.append((declared, first))
        if left > 0:
            plan.append((target, left))
        if not plan:
            # 零干物质车次：仍记在申报仓，不占容量。
            plan.append((declared, D(0)))
        parts = len(plan)
        # 申报仓余量为 0、整车放进另一仓：仍是单段，只是换了仓。
        relocated = plan[0][0] != declared and parts == 1
        if parts == 1:
            # 未拆段：原值直接落账，不做任何比例运算，保证与结算表逐位一致。
            pairs = {"net_kg": (D(str(row["net_kg"])),),
                     "standard_kg": (D(str(row["standard_kg"])),),
                     "amount": (D(str(row.get("amount", 0))),)}
        else:
            pairs = {
                "net_kg": _split_value(D(str(row["net_kg"])), first, dry),
                "standard_kg": _split_value(D(str(row["standard_kg"])), first, dry),
                "amount": _split_amount(D(str(row.get("amount", 0))), first, dry),
            }
        for index, (name, part) in enumerate(plan, 1):
            slot = index - 1
            entries.append({
                "truck_id": truck, "line": row.get("_line", ""), "declared_bin": declared,
                "part_no": index, "part_count": parts, "bin": name,
                "net_kg": pairs["net_kg"][slot],
                "dry_kg": part,
                "standard_kg": pairs["standard_kg"][slot],
                "amount": pairs["amount"][slot],
                "split": parts > 1, "relocated": relocated,
            })
            remaining[name] -= part
        completed.add(truck)

    bins = []
    for name in sorted(caps):
        allocated = caps[name] - before[name] - remaining[name]
        if allocated < 0 or remaining[name] < 0:
            raise DomainError("CAPACITY", f"仓 {name} 分配越界")
        bins.append({
            "bin": name, "capacity": caps[name],
            "opening_kg": before[name], "allocated_kg": allocated,
            "remaining_kg": remaining[name],
            "trucks": len({e["truck_id"] for e in entries if e["bin"] == name}),
            "split_in": sum(1 for e in entries if e["bin"] == name and e["part_no"] > 1),
        })

    total_dry = sum((D(str(r["dry_kg"])) for r in rows), D(0))
    allocated_dry = sum((e["dry_kg"] for e in entries), D(0))
    deferred_dry = sum((D(str(d["dry_kg"])) for d in deferred), D(0))
    if allocated_dry + deferred_dry != total_dry:
        raise DomainError("BALANCE", "分配与暂缓干物质之和不等于本批干物质")
    if any(e["dry_kg"] < 0 for e in entries):
        raise DomainError("BALANCE", "分配出现负干物质")
    if any(b["remaining_kg"] < 0 for b in bins):
        raise DomainError("CAPACITY", "仓容超限")

    # 逐车校验：已分配车的各段干物质之和必须等于整车干物质。
    for row in rows:
        truck = str(row.get("id", ""))
        if truck not in completed:
            continue
        got = sum((e["dry_kg"] for e in entries if e["truck_id"] == truck), D(0))
        want = D(str(row["dry_kg"]))
        if got != want:
            raise DomainError("BALANCE", f"车次 {truck} 拆分段干物质不守恒")

    metrics = {
        "采信车次": len(rows),
        "已分配车次": len(completed),
        "拆仓车次": len({e["truck_id"] for e in entries if e["part_count"] > 1}),
        "暂缓车次": len(deferred),
        "本批可分配干物质 kg": str(total_dry),
        "已占干物质 kg": str(allocated_dry),
        "暂缓干物质 kg": str(deferred_dry),
        "剩余仓容 kg": str(sum((b["remaining_kg"] for b in bins), D(0))),
    }
    return {"entries": entries, "bins": bins, "deferred": deferred, "metrics": metrics}
