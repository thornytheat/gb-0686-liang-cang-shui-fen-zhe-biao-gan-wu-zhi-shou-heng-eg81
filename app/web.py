from flask import render_template,request
from app import store
from app.pipeline import run
from app.config import TITLE,MODULES,STAGES,COLUMNS,SAMPLES,DEFAULTS
def view(stage):
    if not 1<=stage<=len(MODULES): return "页面不存在",404
    run_id=request.args.get("run")
    result=store.fetch(run_id) if run_id else run(persist=False)
    sample=(SAMPLES/"dirty.csv").read_text(encoding="utf-8-sig")
    result=store.bins_after(result)
    data=(result.get("allocation") or {"entries":[],"bins":[],"deferred":[],"metrics":{}}) if stage>len(STAGES) else result["stages"][stage-1]
    return render_template(f"m{stage}.html",title=TITLE,modules=MODULES,stage=stage,result=result,data=data,
                           sample=sample,defaults=DEFAULTS,columns=COLUMNS,history=store.history())
