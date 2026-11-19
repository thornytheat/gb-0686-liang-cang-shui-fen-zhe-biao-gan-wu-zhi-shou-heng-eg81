import os, sqlite3, json
from decimal import Decimal as D
from pathlib import Path
from app.errors import DomainError


def _path():
    return Path(os.environ.get("DATA_DIR", "data")) / "runs.sqlite"


def connection():
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY,payload TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,review TEXT NOT NULL DEFAULT '待复核')")
    db.execute("CREATE TABLE IF NOT EXISTS bins (name TEXT PRIMARY KEY,capacity TEXT NOT NULL,occupied TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS bin_entries (batch TEXT NOT NULL,run_id TEXT NOT NULL,truck_id TEXT NOT NULL,part_no INTEGER NOT NULL,bin TEXT NOT NULL,declared_bin TEXT,line TEXT,net_kg TEXT,dry_kg TEXT,standard_kg TEXT,amount TEXT,part_count INTEGER,PRIMARY KEY(batch,truck_id,part_no))")
    db.execute("CREATE TABLE IF NOT EXISTS alloc_commits (batch TEXT PRIMARY KEY,run_id TEXT NOT NULL,committed TEXT NOT NULL)")
    db.commit()
    return db


def snapshot():
    """已有仓账；没有库文件时返回空，避免只读预览创建数据目录。"""
    if not _path().exists():
        return {}
    with connection() as db:
        rows = db.execute("SELECT name,capacity,occupied FROM bins ORDER BY name").fetchall()
    return {row["name"]: {"capacity": D(row["capacity"]), "occupied": D(row["occupied"]),
                          "remaining": D(row["capacity"]) - D(row["occupied"])}
            for row in rows}


def allocated(run_id):
    with connection() as db:
        row = db.execute("SELECT 1 FROM alloc_commits WHERE run_id=?", (run_id,)).fetchone()
    return row is not None


def _batch_usage(db, batch):
    rows = db.execute("SELECT bin,dry_kg FROM bin_entries WHERE batch=?", (batch,)).fetchall()
    usage = {}
    for row in rows:
        usage[row["bin"]] = usage.get(row["bin"], D(0)) + D(row["dry_kg"])
    return usage


def opening(batch):
    """本批分配前的仓账。若该批次已入账，先扣回它自己的占用，
    保证同一批重跑看到与首次相同的余量，且不会二次占容。"""
    state = snapshot()
    if not state:
        return {}
    with connection() as db:
        committed = db.execute("SELECT 1 FROM alloc_commits WHERE batch=?", (batch,)).fetchone()
        mine = _batch_usage(db, batch) if committed else {}
    return {name: state[name]["occupied"] - mine.get(name, D(0)) for name in state}


def save(result):
    """批次结果入库。带 allocation 的结果同时入正式仓账。

    幂等键是输入内容摘要（batch），不是 run_id：同一批磅单改了参数重跑，
    用差额替换本批旧占用，而不是再占一次。同一 run_id 重复执行直接复用旧结果。
    """
    run_id = result["manifest"]["run_id"]
    batch = result["manifest"]["sha256"]
    allocation = result.get("allocation")
    if allocation and allocated(run_id):
        return fetch(run_id)
    with connection() as db:
        if allocation:
            # 同一批的读仓账 + 写仓账必须在同一段串行事务里：
            # BEGIN IMMEDIATE 先取写锁，避免两个请求都按旧账计算后叠加。
            db.execute("BEGIN IMMEDIATE")
            committed = db.execute("SELECT run_id FROM alloc_commits WHERE batch=?", (batch,)).fetchone()
            if committed and committed["run_id"] == run_id:
                # 并发重复提交同一批：已由另一请求入账，直接复用，不再占容。
                db.rollback()
                return fetch(run_id)
            _commit_bins(db, batch, run_id, allocation)
            db.execute("INSERT INTO alloc_commits(batch,run_id,committed) VALUES(?,?,?) "
                       "ON CONFLICT(batch) DO UPDATE SET run_id=excluded.run_id,committed=excluded.committed",
                       (batch, run_id, result["manifest"].get("created", "")))
            db.execute("INSERT INTO runs(id,payload) VALUES(?,?) "
                       "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                       (run_id, json.dumps(result, ensure_ascii=False)))
        else:
            db.execute("INSERT OR IGNORE INTO runs(id,payload) VALUES(?,?)",
                       (run_id, json.dumps(result, ensure_ascii=False)))
        db.commit()
    return fetch(run_id)


def _commit_bins(db, batch, run_id, allocation):
    previous = _batch_usage(db, batch)
    additions = {}
    for entry in allocation["entries"]:
        additions[entry["bin"]] = additions.get(entry["bin"], D(0)) + D(str(entry["dry_kg"]))
    # 先校验再写，任一仓超容即整体回滚，保留入账前旧版库容。
    planned = {}
    for row in allocation["bins"]:
        name = row["bin"]
        capacity = D(str(row["capacity"]))
        state = db.execute("SELECT occupied FROM bins WHERE name=?", (name,)).fetchone()
        occupied = D(state["occupied"]) if state else D(0)
        expected = occupied - previous.get(name, D(0))
        if D(str(row.get("opening_kg", 0))) != expected:
            raise DomainError("STALE", f"仓 {name} 账面已变化，请重试", 409)
        updated = expected + additions.get(name, D(0))
        if updated > capacity or updated < 0:
            raise DomainError("CAPACITY", f"仓 {name} 超出仓容，本批未入账")
        planned[name] = (capacity, updated)
        # 本批此前占过、本次不再使用的仓要清零回退。
    for name in previous:
        if name not in planned:
            state = db.execute("SELECT capacity,occupied FROM bins WHERE name=?", (name,)).fetchone()
            if state:
                planned[name] = (D(state["capacity"]), D(state["occupied"]) - previous[name])
    for name, (capacity, updated) in planned.items():
        db.execute("INSERT INTO bins(name,capacity,occupied) VALUES(?,?,?) "
                   "ON CONFLICT(name) DO UPDATE SET capacity=excluded.capacity,occupied=excluded.occupied",
                   (name, str(capacity), str(updated)))
    db.execute("DELETE FROM bin_entries WHERE batch=?", (batch,))
    for entry in allocation["entries"]:
        db.execute("INSERT INTO bin_entries(batch,run_id,truck_id,part_no,bin,declared_bin,line,net_kg,dry_kg,standard_kg,amount,part_count) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (batch, run_id, str(entry["truck_id"]), int(entry["part_no"]), str(entry["bin"]),
                    str(entry.get("declared_bin", "")), str(entry.get("line", "")),
                    str(entry["net_kg"]), str(entry["dry_kg"]), str(entry["standard_kg"]),
                    str(entry.get("amount", "0")), int(entry.get("part_count", 1))))


def fetch(run_id):
    with connection() as db:
        row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise DomainError("NOT_FOUND", "批次不存在", 404)
    result = json.loads(row["payload"])
    result["version"] = row["version"]
    result["review"] = row["review"]
    return result


def bins_after(result):
    """页面展示用：本批结果保持本批口径（入账时点），另附当前仓账供对照。"""
    state = snapshot()
    plan = result.get("allocation")
    if plan and state:
        bins = []
        for row in plan["bins"]:
            current = state.get(row["bin"])
            if current:
                bins.append({**row, "current_occupied_kg": current["occupied"],
                             "current_remaining_kg": current["remaining"]})
            else:
                bins.append(row)
        result = {**result, "allocation": {**plan, "bins": bins}}
    return result


def history():
    with connection() as db:
        rows = db.execute("SELECT id,version,review FROM runs ORDER BY rowid DESC LIMIT 100").fetchall()
    return [dict(row) for row in rows]


def review(run_id, version, decision):
    if decision not in ("通过", "退回"):
        raise DomainError("REVIEW", "请选择通过或退回")
    if not isinstance(version, int):
        raise DomainError("STALE", "版本必须为整数")
    with connection() as db:
        cur = db.execute("UPDATE runs SET review=?,version=version+1 WHERE id=? AND version=?",
                         (decision, run_id, version))
        if cur.rowcount != 1:
            raise DomainError("STALE", "复核版本已更新", 409)
    return fetch(run_id)
