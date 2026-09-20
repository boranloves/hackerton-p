# -*- coding: utf-8 -*-
"""
식약처 식품영양성분DB OpenAPI 연동 (FoodNtrCpntDbInfo03).

- 재료명으로 실측 영양성분(100g 기준)을 조회해 refdata.NUTRIENTS와 같은
  {kcal, protein, fat, carbs, sodium, calcium, iron, vitA, vitC, fiber} 구조로 돌려준다.
- 조회 결과는 메모리 + data/nutri_cache.json에 캐시한다(서버리스 읽기전용 FS에서는
  파일 저장을 건너뛰고 메모리만 사용).
- 매칭: FOOD_NM_KR LIKE 검색 → 정규화 이름 기준 정확/접두/포함 순 + 짧은 이름 우선으로
  최선 행 선택. 히트가 없으면 재료명에 포함된 핵심 재료 단어(refdata.NUTRIENTS 키)로 재검색.
- 응답 필드(100g당, 실측 검증): AMT_NUM1=에너지(kcal), 3=단백질, 4=지방, 6=탄수화물,
  8=식이섬유, 9=칼슘, 10=철분, 13=나트륨, 14=비타민A(RE), 21=비타민C.
  정제염(나트륨 36,025), 돼지간 요리(비A 4,643·철 4.19), 브로콜리 요리(비C 6~16)로 확정.
"""
import json
import os
import threading
import urllib.parse
import urllib.request

import refdata

ENDPOINT = ("https://apis.data.go.kr/1471000/FoodNtrCpntDbInfo03"
            "/getFoodNtrCpntDbInq03")
SERVICE_KEY = os.environ.get(
    "DATA_GO_KR_KEY",
    "4b90357f259413cf91cf3691abfde56d0d93434934002ca04f1b5d79689cba5a")
TIMEOUT = 6          # API 응답 대기(초) — 실패 시 폴백하고 앱이 막히지 않게 함
FETCH_ROWS = 12      # 최선 매칭을 고를 여유분

AMT_MAP = {1: "kcal", 3: "protein", 4: "fat", 6: "carbs", 8: "fiber",
           9: "calcium", 10: "iron", 13: "sodium", 14: "vitA", 21: "vitC"}

CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "data", "nutri_cache.json")

# 재료명에서 핵심 단어 찾기용 시드(긴 단어 우선): "삶은감자"→"감자", "미소된장"→"된장"
_SEEDS = sorted((k for k in refdata.NUTRIENTS), key=len, reverse=True)

_lock = threading.Lock()
_cache = None  # {정규화된 질의: 영양 dict | None(미발견)}


def _norm(name):
    return (name or "").replace(" ", "").strip()


def _load_cache():
    global _cache
    if _cache is not None:
        return
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            _cache = json.load(f)
    except Exception:
        _cache = {}


def _save_cache():
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(_cache, f, ensure_ascii=False)
    except OSError:
        pass  # 읽기전용 파일시스템(서버리스) — 메모리 캐시만 유지


def _to_num(raw):
    """API 숫자 필드("36,025.000"/""/None) → float | None."""
    if raw is None:
        return None
    s = str(raw).replace(",", "").strip()
    if not s:
        return None
    try:
        v = float(s)
        return v if v >= 0 else None
    except ValueError:
        return None


def _serving_scale(item):
    """SERVING_SIZE("100g", "30g"…) 기준 보정. 기본·파싱 실패는 100g로 가정."""
    size = _norm(str(item.get("SERVING_SIZE") or ""))
    if size.endswith("g"):
        try:
            grams = float(size[:-1])
            if grams > 0:
                return 100.0 / grams
        except ValueError:
            pass
    return 1.0


def _fetch_items(query):
    """FOOD_NM_KR LIKE 검색 → 최대 FETCH_ROWS건."""
    params = {"serviceKey": SERVICE_KEY, "pageNo": "1",
              "numOfRows": str(FETCH_ROWS), "type": "json", "FOOD_NM_KR": query}
    url = f"{ENDPOINT}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return []
    body = data.get("body") or {}
    items = body.get("items") or []
    if isinstance(items, dict):  # numOfRows=1일 때 단일 객체로 내려오는 케이스
        items = [items]
    return items


def _match_score(item_name, q):
    """(정확 0 < 접두 1 < 포함 2, 이름 길이) — 짧고 정확한 이름이 최선."""
    n = _norm(item_name)
    if n == q:
        return (0, len(n))
    if n.startswith(q):
        return (1, len(n))
    if q in n:
        return (2, len(n))
    return None


def _convert(item):
    """API 행 → 100g당 영양 dict(빈 값은 제외)."""
    scale = _serving_scale(item)
    out = {}
    for num, key in AMT_MAP.items():
        v = _to_num(item.get(f"AMT_NUM{num}"))
        if v is not None:
            out[key] = round(v * scale, 1)
    return out or None


def _search_convert(query):
    """질의 1회 검색 + 최선 행 변환. 없으면 None."""
    q = _norm(query)
    best, best_score = None, None
    for item in _fetch_items(q):
        score = _match_score(item.get("FOOD_NM_KR"), q)
        if score is None:
            continue
        if best_score is None or score < best_score:
            best, best_score = item, score
    return _convert(best) if best else None


def resolve(name):
    """재료명 → (영양 dict|None, 출처 'local'|'api'|'none').

    커버리지: 검증된 로컬 DB(76종) 우선 → 식약처 API(33만건) → 미발견.
    미발견도 캐시해 같은 이름으로 API를 반복 호출하지 않는다.
    """
    q = _norm(name)
    if not q:
        return None, "none"
    db = refdata.NUTRIENTS.get(q)
    if db:
        return db, "local"
    with _lock:
        _load_cache()
        if q in _cache:
            return _cache[q], ("api" if _cache[q] else "none")
    result = _search_convert(q)
    if result is None:  # 핵심 재료 단어로 재검색("삶은감자"→"감자")
        for seed in _SEEDS:
            if len(seed) >= 2 and seed in q and seed != q:
                result = _search_convert(seed)
                if result is not None:
                    break
    with _lock:
        _load_cache()
        _cache[q] = result
        _save_cache()
    return result, ("api" if result else "none")


def lookup(name):
    """디버그/표시용: resolve와 동일하되 원래 질의명을 함께 돌려준다."""
    nutrients, source = resolve(name)
    return nutrients, source
