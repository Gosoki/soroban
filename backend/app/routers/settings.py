"""运行期设置：读 / 改。

只暴露 `services/prefs.SPECS` 里注册过的键——注册表是唯一真相，路由不重复一份白名单，
也不重复一份取值范围校验（那样迟早两边对不上）。前端的表单也由 `describe()` 生成。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from ..auth import get_current_user
from ..database import get_session
from ..schemas import SettingsUpdate
from ..services import prefs

router = APIRouter(
    prefix="/api/settings", tags=["settings"], dependencies=[Depends(get_current_user)]
)


@router.get("")
def get_settings(session: Session = Depends(get_session)):
    """当前值 + 表单元信息（可选值、上下界、说明）。前端据此渲染，不自己写死一份。"""
    return {"values": prefs.load(session), "specs": prefs.describe()}


@router.put("")
def put_settings(payload: SettingsUpdate, session: Session = Depends(get_session)):
    """按传入的键更新。任一项不合规就整体拒绝——半套设置比旧设置更难排查。"""
    try:
        values = prefs.save(session, payload.values)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"values": values, "specs": prefs.describe()}
