"""Auth routes: login (OAuth2 password form) + current user."""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlmodel import Session

from ..auth import authenticate, create_access_token, get_current_user, hash_password, verify_password
from ..database import get_session
from ..models import User
from ..ratelimit import login_throttle
from ..schemas import ChangePassword, LoginResponse, UserRead

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    session: Session = Depends(get_session),
):
    # 失败退避：账号名长期固定、默认口令公开、令牌 90 天有效——不限速等于把字典爆破的门开着。
    # 按 (用户名, 来源 IP) 计数，前几次手滑不惩罚，之后指数退避（见 ratelimit.py）。
    key = login_throttle.key(form.username, request.client.host if request.client else None)
    wait = login_throttle.retry_after(key)
    if wait:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"登录失败次数过多，请 {wait} 秒后再试",
            headers={"Retry-After": str(wait)},
        )
    user = authenticate(session, form.username, form.password)
    if not user:
        login_throttle.record_failure(key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )
    login_throttle.record_success(key)
    return LoginResponse(access_token=create_access_token(user), user=UserRead.model_validate(user))


@router.get("/me", response_model=UserRead)
def me(current: User = Depends(get_current_user)):
    return current


@router.post("/change-password")
def change_password(
    payload: ChangePassword,
    current: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if not verify_password(payload.old_password, current.password_hash):
        raise HTTPException(status_code=400, detail="原密码不正确")
    if len(payload.new_password) < 6:
        raise HTTPException(status_code=422, detail="新密码至少 6 位")
    current.password_hash = hash_password(payload.new_password)
    session.add(current)
    session.commit()
    return {"ok": True}
