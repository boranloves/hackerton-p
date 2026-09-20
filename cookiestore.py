# -*- coding: utf-8 -*-
"""
쿠키 기반 개인 데이터 저장소.

식단·프로필 등 개인 데이터는 서버 DB가 아니라 사용자 브라우저의 쿠키에 저장한다.
- 개별 쿠키 ~4KB 한계 → base64 인코딩 후 여러 쿠키(fnai_이름_N)로 분할 저장
- 위변조 방지: HMAC-SHA256 서명, 불일치 시 데이터 폐기(빈 값 처리)
- HttpOnly + SameSite=Lax, 유효기간 365일
"""
import base64
import hashlib
import hmac
import json
import os
import secrets

from flask import g, request

COOKIE_PREFIX = "fnai"
MAX_CHUNK = 3600   # 쿠키당 값 크기(바이트) — 헤더 한계 여유분 확보
MAX_CHUNKS = 16    # 최대 분할 수 → 데이터 상한 약 55KB
MAX_AGE = 365 * 24 * 3600

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
_KEY = None


_FALLBACK_KEY = "fnai-default-signing-key-change-me-in-production"


def _key():
    global _KEY
    if _KEY is None:
        # 1순위: 환경변수(서버리스/Vercel 배포 시 대시보드에서 SECRET_KEY 설정 권장)
        env_key = os.environ.get("SECRET_KEY", "").strip()
        if env_key:
            _KEY = env_key.encode("utf-8")
            return _KEY
        # 2순위: 로컬 파일(읽기/쓰기 가능한 파일시스템)
        path = os.path.join(_DATA_DIR, "secret.key")
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    key = f.read().strip()
                if key:
                    _KEY = key.encode("utf-8")
                    return _KEY
            os.makedirs(_DATA_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(secrets.token_hex(32))
            with open(path, "r", encoding="utf-8") as f:
                _KEY = f.read().strip().encode("utf-8")
            return _KEY
        except OSError:
            # 3순위: 읽기전용 파일시스템(서버리스) — 고정 키로 일관성 유지
            _KEY = _FALLBACK_KEY.encode("utf-8")
            return _KEY
    return _KEY


def _sign(payload: str) -> str:
    return hmac.new(_key(), payload.encode("ascii"), hashlib.sha256).hexdigest()[:32]


def encode(obj) -> str:
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    payload = base64.urlsafe_b64encode(raw).decode("ascii")
    return payload + "." + _sign(payload)


def decode(s: str):
    try:
        payload, sig = s.rsplit(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(_sign(payload), sig):
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return None


def read(name: str):
    """요청 쿠키에서 데이터 읽기. 없거나 손상 시 None."""
    val = request.cookies.get(f"{COOKIE_PREFIX}_{name}_0")
    if not val:
        return None
    parts = [val]
    for i in range(1, MAX_CHUNKS):
        nxt = request.cookies.get(f"{COOKIE_PREFIX}_{name}_{i}")
        if not nxt:
            break
        parts.append(nxt)
    return decode("".join(parts))


def stage(name: str, obj):
    """응답에 기록할 쿠키를 등록(after_request에서 실제 기록)."""
    encoded = encode(obj)
    chunks = [encoded[i:i + MAX_CHUNK] for i in range(0, len(encoded), MAX_CHUNK)]
    if len(chunks) > MAX_CHUNKS:
        raise ValueError("쿠키 데이터가 너무 큽니다.")
    if "_cookie_writes" not in g:
        g._cookie_writes = {}
    g._cookie_writes[name] = chunks


def apply_writes(resp):
    """after_request 훅: stage된 쿠키를 응답에 심고, 더 이상 필요 없는 잔여 청크 제거."""
    for name, chunks in (g.pop("_cookie_writes", None) or {}).items():
        for i, part in enumerate(chunks):
            resp.set_cookie(f"{COOKIE_PREFIX}_{name}_{i}", part,
                            max_age=MAX_AGE, httponly=True, samesite="Lax", path="/")
        for i in range(len(chunks), MAX_CHUNKS):
            if f"{COOKIE_PREFIX}_{name}_{i}" in request.cookies:
                resp.delete_cookie(f"{COOKIE_PREFIX}_{name}_{i}", path="/")
    return resp
