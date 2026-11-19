from .rules import allocate, capacities
from app.errors import DomainError


def execute(ctx):
    # 仓容口径必须显式给出，缺失不静默放行。
    try:
        caps = capacities(ctx['params'].get('bin_capacity'))
    except DomainError as exc:
        raise DomainError('CAPACITY', exc.message)
    # 暂缓车不写入正式仓账，因此不进入 quarantine（它仍是已采信车次，
    # 只是本批不卸粮）；它单独出现在 allocation.deferred 供页面与清单核对。
    ctx['allocation'] = allocate(ctx['records'], caps, ctx.get('opening'))
    return ctx
