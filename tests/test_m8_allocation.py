from decimal import Decimal as D
import pytest
from app.errors import DomainError
from app.modules.m8_allocation.rules import allocate, capacities
from app.pipeline import run
from app import store


def truck(id, line, bin, dry, net=None, standard=None, amount=None):
    return {"id": id, "_line": line, "bin": bin,
            "net_kg": D(str(net if net is not None else dry)),
            "dry_kg": D(str(dry)),
            "standard_kg": D(str(standard if standard is not None else dry)),
            "amount": D(str(amount if amount is not None else dry))}


def test_split_conserves_dry_matter():
    # 甲仓只剩 20，乙仓够；一车 90 必须拆成 20+70，干物质相加等于整车。
    plan = allocate([truck("A", 2, "甲", 90)], {"甲": D(100), "乙": D(100)}, {"甲": D(80)})
    assert [(e["bin"], e["dry_kg"]) for e in plan["entries"]] == [("甲", D(20)), ("乙", D(70))]
    assert sum(e["dry_kg"] for e in plan["entries"]) == D(90)


def test_split_second_part_uses_remainder():
    # 净重/折标/金额按比例拆，第二段用整车减第一段，逐车必须守恒。
    plan = allocate([truck("A", 2, "甲", D("1"), D("3"), D("7"), D("10"))],
                    {"甲": D("0.6"), "乙": D(10)})
    parts = plan["entries"]
    assert len(parts) == 2
    assert sum(p["net_kg"] for p in parts) == D(3)
    assert sum(p["standard_kg"] for p in parts) == D(7)
    assert sum(p["amount"] for p in parts) == D(10)


def test_split_respects_two_decimal_money():
    # 金额拆段保留到分，且两段相加等于原金额。
    plan = allocate([truck("A", 2, "甲", D("1"), D("1"), D("1"), D("100.01"))],
                    {"甲": D("0.3"), "乙": D(10)})
    amounts = [p["amount"] for p in plan["entries"]]
    assert sum(amounts) == D("100.01")
    assert all(a.as_tuple().exponent == -2 for a in amounts)


def test_no_bin_over_capacity():
    plan = allocate([truck(str(i), i, "甲", 30) for i in range(1, 6)],
                    {"甲": D(50), "乙": D(50)})
    for b in plan["bins"]:
        assert b["allocated_kg"] <= b["capacity"]
        assert b["remaining_kg"] >= 0


def test_deferred_truck_takes_no_capacity():
    # 甲剩 20，乙只够 30，不足整段 70：整车暂缓，两个仓都不为它留货。
    plan = allocate([truck("A", 2, "甲", 90)], {"甲": D(100), "乙": D(30)}, {"甲": D(80)})
    assert plan["entries"] == []
    assert [d["truck_id"] for d in plan["deferred"]] == ["A"]
    assert {b["bin"]: b["allocated_kg"] for b in plan["bins"]} == {"甲": D(0), "乙": D(0)}


def test_whole_truck_moves_when_declared_bin_full():
    # 申报仓余量为 0，另一仓能整放：整车换仓，不拆、不留 0 kg 首段。
    plan = allocate([truck("A", 2, "甲", 40)], {"甲": D(100), "乙": D(100)}, {"甲": D(100)})
    assert len(plan["entries"]) == 1
    entry = plan["entries"][0]
    assert (entry["bin"], entry["part_count"], entry["relocated"]) == ("乙", 1, True)


def test_order_is_inbound_line_order():
    a, b = truck("B", 9, "甲", 10), truck("A", 2, "甲", 10)
    plan = allocate([a, b], {"甲": D(10), "乙": D(10)})
    assert [e["truck_id"] for e in plan["entries"]] == ["A", "B"]
    assert plan["entries"][0]["bin"] == "甲"


def test_declared_bin_without_capacity_deferred():
    plan = allocate([truck("A", 2, "未知仓", 10)], {"甲": D(10)})
    assert plan["entries"] == [] and plan["deferred"][0]["code"] == "CAPACITY"


def test_capacity_param_forms():
    assert capacities('{"仓1":"10","仓2":"20"}') == {"仓1": D(10), "仓2": D(20)}
    assert capacities("仓1=10,仓2=20") == {"仓1": D(10), "仓2": D(20)}
    with pytest.raises(DomainError):
        capacities("仓1")


def test_real_batch_split_and_defer(raw, isolate):
    result = run(raw)
    plan = result["allocation"]
    assert plan["metrics"]["暂缓车次"] == 1
    assert plan["metrics"]["拆仓车次"] == 2
    # 每个拆仓车次各段干物质相加等于整车，且暂缓车不在分配表里。
    allocated = {}
    for entry in plan["entries"]:
        allocated[entry["truck_id"]] = allocated.get(entry["truck_id"], D(0)) + D(entry["dry_kg"])
    records = {r["id"]: D(r["dry_kg"]) for r in result["records"]}
    assert allocated == {k: v for k, v in records.items() if k in allocated}
    deferred = {d["truck_id"] for d in plan["deferred"]}
    assert not deferred & set(allocated)
    assert sum(allocated.values()) + sum(D(d["dry_kg"]) for d in plan["deferred"]) == sum(records.values())
    for b in plan["bins"]:
        assert D(b["allocated_kg"]) <= D(b["capacity"])


def test_real_batch_idempotent_bin_ledger(raw, isolate, client):
    first = client.post("/api/runs", json={}).json
    before = {n: b["occupied"] for n, b in store.snapshot().items()}
    again = client.post("/api/runs", json={}).json
    after = {n: b["occupied"] for n, b in store.snapshot().items()}
    assert again["manifest"]["run_id"] == first["manifest"]["run_id"]
    assert after == before


def test_same_input_new_params_replaces_not_adds(isolate, client):
    body = {"text": "id,bin,gross,tare,unit,moisture,impurity,version\n"
                    "00001,仓1,1200,200,kg,14,0,1\n"
                    "00002,仓2,1100,100,kg,14,0,1\n"}
    first = client.post("/api/runs", json={**body, "params": {"bin_capacity": "仓1=50,仓2=50"}}).json
    assert first["allocation"]["metrics"]["暂缓车次"] >= 1
    # 同一批磅单改大仓容重跑：本批旧占用被替换，不是叠加。
    second = client.post("/api/runs", json={**body, "params": {"bin_capacity": "仓1=5000,仓2=5000"}}).json
    after = {n: b["occupied"] for n, b in store.snapshot().items()}
    assert second["manifest"]["run_id"] != first["manifest"]["run_id"]
    assert second["allocation"]["metrics"]["暂缓车次"] == 0
    # 干物质 = 净重 ×(1-14%)：1000 + 1000 → 860 + 860，只留本次，不含上一版。
    assert sum(after.values()) == D(1720)


def test_commit_failure_keeps_previous_ledger(isolate):
    # 先入账一批，拿到旧账与旧发布结果。
    first = run(persist=True)
    before = {n: b["occupied"] for n, b in store.snapshot().items()}
    # 仓1 的 opening 与账面一致（会先写），仓2 故意过期（随后报错），
    # 验证整批事务回滚，而不是留下写了一半的仓账。
    forged = {"manifest": {"run_id": "forged-run", "sha256": "forged-batch"},
              "allocation": {"bins": [
                  {"bin": "仓1", "capacity": "99999", "opening_kg": str(before["仓1"])},
                  {"bin": "仓2", "capacity": "99999", "opening_kg": "0"}],
                  "entries": [{"truck_id": "X", "part_no": 1, "bin": "仓1", "declared_bin": "仓1",
                               "line": "1", "net_kg": "1", "dry_kg": "1", "standard_kg": "1",
                               "amount": "0", "part_count": 1}]}}
    with pytest.raises(DomainError):
        store.save(forged)
    after = {n: b["occupied"] for n, b in store.snapshot().items()}
    assert after == before
    assert store.fetch(first["manifest"]["run_id"])["manifest"]["run_id"] == first["manifest"]["run_id"]
    with pytest.raises(DomainError):
        store.fetch("forged-run")


def test_over_capacity_commit_rolls_back(isolate):
    first = run(persist=True)
    before = {n: b["occupied"] for n, b in store.snapshot().items()}
    bins = [{"bin": name, "capacity": str(state["capacity"]),
             "opening_kg": str(state["occupied"])} for name, state in store.snapshot().items()]
    # 给仓1 再塞 2 kg 但只留 1 kg 仓容：整批拒绝，旧账不变。
    bins[0]["capacity"] = str(D(bins[0]["opening_kg"]) + 1)
    forged = {"manifest": {"run_id": "over-cap", "sha256": "over-cap-batch"},
              "allocation": {"bins": bins,
                             "entries": [{"truck_id": "X", "part_no": 1, "bin": bins[0]["bin"],
                                          "declared_bin": bins[0]["bin"], "line": "1",
                                          "net_kg": "1", "dry_kg": "2", "standard_kg": "1",
                                          "amount": "0", "part_count": 1}]}}
    with pytest.raises(DomainError):
        store.save(forged)
    assert {n: b["occupied"] for n, b in store.snapshot().items()} == before
    assert store.fetch(first["manifest"]["run_id"])["allocation"]["metrics"]["已分配车次"] > 0
