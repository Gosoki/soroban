"""对外暴露「状态机规则」，供插件等外部进程取用。

**为什么要有这个端点**：爬虫插件是独立仓库、独立 venv，import 不到后端代码。
它回灌已导入订单时需要判断「能不能推进」，而这个规则的唯一真相在
`models/base.py`（rank + 终态）。若让插件自己抄一份，就又多一处「同一份东西写两处、
靠约定同步」——这正是本项目反复出问题的头号 bug 类（爬虫 STATUS_MAP 漏改导致整批
422 静默丢同步，就是这么来的）。让它来取，规则永远只有一处。

只读、无副作用；内容是纯规则，不含任何业务数据。
"""

from fastapi import APIRouter, Depends

from ..auth import get_current_user
from ..models import PURCHASE_STATUS_RANK, PURCHASE_TERMINAL_STATUSES, PurchaseStatus, ShipmentStatus

router = APIRouter(prefix="/api/meta", tags=["meta"], dependencies=[Depends(get_current_user)])


@router.get("/status-rules", openapi_extra={"x-scope": "meta:read"})
def status_rules():
    """各业务段的状态机：合法值、生命周期序、终态集合。**按段分键**。

    消费方（如爬虫）据此实现 can_advance_purchase：
      终态不可被推进态覆盖；推进态只前进不回退；未知值 rank 为 -1。
    与 `models/base.can_advance_purchase` 同源——那里是权威实现，这里只是把数据搬出来。
    """
    return {
        "purchase": {
            "values": [s.value for s in PurchaseStatus],
            "rank": dict(PURCHASE_STATUS_RANK),
            "terminal": sorted(PURCHASE_TERMINAL_STATUSES),
        },
        # 国际段目前没有「推进序」的概念（人工推进），只暴露合法值。
        # 按段分键而不是把它塞成 shipment_values：加卖出段时是新增一个键，不是再拍一个前缀。
        "shipment": {"values": [s.value for s in ShipmentStatus]},
    }
