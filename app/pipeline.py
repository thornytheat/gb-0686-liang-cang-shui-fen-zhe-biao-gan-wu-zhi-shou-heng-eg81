from importlib import import_module
from time import perf_counter
from app.config import STAGES, DEFAULTS, SAMPLES
from app.manifest import make
from app.normalize import serial
from app.errors import DomainError
from app import store


def run(raw=None, params=None, stop=7, persist=True):
    if raw is None: raw=(SAMPLES/"dirty.csv").read_bytes()
    if isinstance(raw,str): raw=raw.encode("utf-8")
    if not isinstance(params,(dict,type(None))): raise DomainError("PARAMS","参数必须是对象")
    actual={**DEFAULTS,**(params or {})}
    manifest=make(raw,actual)
    # 同批已入账：直接返回旧结果，不重算、不二次占容。
    if persist and stop==len(STAGES) and store.allocated(manifest["run_id"]):
        return store.fetch(manifest["run_id"])
    t=perf_counter();ctx={"source":raw,"params":actual,"stages":[],"quarantine":[],"run_id":manifest["run_id"]}
    for stage in range(1,min(stop,len(STAGES))+1):
        mod=import_module(f"app.modules.m{stage}_{STAGES[stage-1]}.service")
        ctx=mod.execute(ctx)
    ctx.pop("source",None);ctx.pop("raw",None)
    # 仓容分配是本批的派生账，不改变前 7 步的车次与金额口径。
    if stop>=len(STAGES):
        ctx["opening"]=store.opening(manifest["sha256"])
        ctx=import_module("app.modules.m8_allocation.service").execute(ctx)
    result=serial(ctx);result["manifest"]=manifest;result["elapsed_ms"]=round((perf_counter()-t)*1000,3)
    result["version"]=1;result["review"]="待复核"
    if persist and stop==len(STAGES): return store.save(result)
    return result
