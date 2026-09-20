# -*- coding: utf-8 -*-
"""
냉장고 털이 AI 에이전트 — 운영 버전 Flask 서버 (waitress WSGI).

핵심 기능과 연동 구조:
  1) 냉장고 재료·유통기한 저장/분석     -> /fridge, /api/fridge*
  2) 평소 식단·칼로리 분석 및 예측      -> /diet, /api/meals*, /api/calorie/summary
  3) 남은 재료 × 평소 식단 레시피 추천  -> /recipes, /api/recipes/recommend
  4) 영양소 결핍 분석·식단 관리        -> /nutrition, /api/nutrition/summary

연동: 냉장고 유통기한 -> 레시피 임박재료 가점 /
식단 기록 -> 평소식단 통계·칼로리 예측·영양분석 / 영양 결핍 -> 레시피 보충 가점 /
레시피 '요리했어요' -> 재료 소진 + 식단 자동 기록 / 프로필 알레르기 -> 레시피 추천 제외.

개인 데이터(냉장고·식단·프로필)는 서버 DB가 아닌 사용자 브라우저 쿠키에 저장된다(cookiestore).

환경변수: HOST(기본 127.0.0.1), PORT(기본 5000), DEBUG(기본 0), SECRET_KEY(쿠키 서명)
"""
import json
import logging
import os
import sqlite3
import sys
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler

from flask import Flask, g, jsonify, render_template, request

import cookiestore
import nutrition_api as nutri
import refdata
import recipes_data as rd
# store.py(SQLite)는 과거 저장소 — 쿠키 미존재 시 1회 이관 원본으로만 참조됨

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "5000"))
DEBUG = os.environ.get("DEBUG", "0") == "1"

LOG_PATH = os.path.join(BASE_DIR, "data", "app.log")
logger = logging.getLogger("fridge_ai")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
try:  # Windows 콘솔(cp949)에서 em-dash 등 유니코드 로그 실패 방지
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass
_handlers = [logging.StreamHandler(sys.stdout)]
try:  # 읽기전용 파일시스템(서버리스)에서는 파일 로그 건너뜀
    _handlers.append(RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
except OSError:
    pass
for h in _handlers:
    h.setFormatter(_fmt)
    logger.addHandler(h)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024  # JSON 본문 상한


# ---------------------------------------------------------------------------
# 개인 데이터 쿠키 저장소(식단·프로필): 브라우저 쿠키가 원본.
# 기존 SQLite에 있던 데이터는 쿠키가 비어 있을 때 한 번 이관해 사용한다.
_DB_PATH = os.path.join(BASE_DIR, "data", "fridge_ai.db")
_MEAL_PRUNE_DAYS = 60
_MEAL_PRUNE_MAX = 200


def _meals_from_db_backup():
    try:
        conn = sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, date, type, memo, items FROM meals").fetchall()
        conn.close()
        meals = []
        for r in rows:
            try:
                items = json.loads(r["items"])
            except Exception:
                continue
            meals.append({"id": r["id"], "date": r["date"], "type": r["type"],
                          "items": items, "memo": r["memo"] or ""})
        return meals
    except Exception:
        return []


def _fridge_from_db_backup():
    try:
        conn = sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, name, category, amount, expiry, source FROM fridge").fetchall()
        conn.close()
        return [{"id": r["id"], "name": r["name"], "category": r["category"] or "기타",
                 "amount": r["amount"] or "", "expiry": r["expiry"], "source": r["source"] or "수동"}
                for r in rows]
    except Exception:
        return []


def _profile_from_db_backup():
    try:
        conn = sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT name, daily_kcal_target, notes FROM profile WHERE id=1").fetchone()
        conn.close()
        if row:
            return {"name": row["name"] or "사용자",
                    "daily_kcal_target": row["daily_kcal_target"] or 2100,
                    "notes": row["notes"] or ""}
    except Exception:
        pass
    return None


def _prune_meals(meals):
    """쿠키 용량 보호: 최근 60일 + 최대 200건만 보관."""
    cutoff = (date.today() - timedelta(days=_MEAL_PRUNE_DAYS)).isoformat()
    kept = [m for m in meals if m.get("date", "") >= cutoff]
    kept.sort(key=lambda m: (m.get("date", ""), m.get("id", "")))
    return kept[-_MEAL_PRUNE_MAX:]


class CookieMealStore:
    """기존 MealStore와 동일 인터페이스. 원본은 쿠키, 요청 내 g 캐시로 일관성 유지."""

    def _state(self):
        if "_meals" not in g:
            data = cookiestore.read("meals")
            if data is None:
                data = _meals_from_db_backup()
                if data:
                    cookiestore.stage("meals", _prune_meals(data))
                    logger.info("기존 DB 식단 %d건을 쿠키로 이관", len(data))
            g._meals = data or []
        return g._meals

    def all(self):
        return list(self._state())

    def recent_days(self, days):
        start = (date.today() - timedelta(days=days)).isoformat()
        return [m for m in self._state() if m.get("date", "") >= start]

    def add(self, meal_date, mtype, items, memo=None):
        meal = {"id": uuid.uuid4().hex[:8], "date": meal_date, "type": mtype,
                "items": items, "memo": memo or ""}
        st = self._state()
        st.append(meal)
        cookiestore.stage("meals", _prune_meals(st))
        return dict(meal)

    def remove(self, meal_id):
        st = self._state()
        before = len(st)
        st[:] = [m for m in st if m["id"] != meal_id]
        if len(st) != before:
            cookiestore.stage("meals", _prune_meals(st))
            return True
        return False


class CookieProfileStore:
    def _state(self):
        if "_profile" not in g:
            data = cookiestore.read("profile")
            if data is None:
                data = _profile_from_db_backup() or {"name": "사용자", "daily_kcal_target": 2100, "notes": ""}
                cookiestore.stage("profile", data)
            data.setdefault("allergies", [])  # 구 쿠키에 없던 항목 기본값
            g._profile = data
        return g._profile

    def get(self):
        return dict(self._state())

    def update(self, patch):
        st = self._state()
        st.update(patch)
        cookiestore.stage("profile", st)
        return dict(st)


_FRIDGE_MAX = 150  # 쿠키 용량 보호 상한


class CookieFridgeStore:
    """냉장고 재료도 쿠키 저장(서버리스 파일시스템은 읽기전용이므로)."""

    def _state(self):
        if "_fridge" not in g:
            data = cookiestore.read("fridge")
            if data is None:
                data = _fridge_from_db_backup()
                if data:
                    cookiestore.stage("fridge", data)
                    logger.info("기존 DB 냉장고 %d건을 쿠키로 이관", len(data))
            g._fridge = data or []
        return g._fridge

    def all(self):
        return sorted(self._state(), key=lambda i: i.get("expiry", ""))

    def add(self, name, category, amount, expiry, source="수동"):
        st = self._state()
        if len(st) >= _FRIDGE_MAX:
            raise ApiError(f"냉장고 재료가 너무 많습니다 (최대 {_FRIDGE_MAX}개). 사용하지 않는 재료를 정리해 주세요.")
        item = {"id": uuid.uuid4().hex[:8], "name": name, "category": category,
                "amount": amount or "", "expiry": expiry, "source": source}
        st.append(item)
        cookiestore.stage("fridge", st)
        return dict(item)

    def remove(self, item_id):
        st = self._state()
        before = len(st)
        st[:] = [i for i in st if i["id"] != item_id]
        if len(st) != before:
            cookiestore.stage("fridge", st)
            return True
        return False


meal_store = CookieMealStore()
profile_store = CookieProfileStore()
fridge_store = CookieFridgeStore()

# 만개의 레시피 실데이터(data/recipe_main.csv) 로드. 없으면 목업 폴백.
RECIPES = rd.load_recipes()

MICROS = ["protein", "sodium", "calcium", "iron", "vitA", "vitC", "fiber"]

MEAL_TYPES = ("아침", "점심", "저녁", "간식")
CATEGORIES = ("채소류", "축산류", "해물류", "계란", "유제품", "곡류", "과일", "기타")


class ApiError(Exception):
    """클라이언트 입력 오류 → 400 JSON 응답."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def user_allergies():
    """프로필에 저장된 알레르기 key 집합(카탈로그에 없는 값은 무시)."""
    valid = {a["key"] for a in refdata.ALLERGENS}
    return {a for a in profile_store.get().get("allergies", []) if a in valid}


def infer_meal_type():
    """현재 시각 기준 끼니 추정 — '요리했어요' 식단 자동 기록용."""
    h = datetime.now().hour
    if 6 <= h < 11:
        return "아침"
    if 11 <= h < 15:
        return "점심"
    if 15 <= h < 21:
        return "저녁"
    return "간식"


@app.post("/api/fridge/consume")
def api_fridge_consume():
    """레시피 '요리했어요' — 레시피에 쓰인 모든 재료를 식단 내역에 기록하고,
    냉장고에 있던 재료는 제거한다."""
    body = json_body()
    try:
        idx = int(body.get("recipe_index"))
    except (TypeError, ValueError):
        raise ApiError("recipe_index가 필요합니다.")
    recipe = next((r for r in RECIPES if r["index"] == idx), None)
    if not recipe:
        raise ApiError("해당 레시피를 찾을 수 없습니다.")
    fridge_names = {rd.norm_ingredient(f["name"]): f["id"] for f in fridge_store.all()}
    removed, missing, seen, used_items = [], [], set(), []
    for ing in recipe["ingredients"]:
        key = rd.norm_ingredient(ing["name"])
        if key in seen:
            continue
        seen.add(key)
        used_items.append({"name": ing["name"][:40],
                           "amount": ing.get("grams_1p")
                           or rd.amount_to_grams(ing.get("amount"))})
        if key in fridge_names:
            removed.append(ing["name"])
            fridge_store.remove(fridge_names[key])
        else:
            missing.append(ing["name"])
    if not removed:
        raise ApiError("냉장고에서 소진할 재료가 없습니다. 재료를 먼저 등록해 주세요.")
    meal = meal_store.add(date.today().isoformat(), infer_meal_type(), used_items,
                          ("레시피: " + recipe["title"])[:200])
    meal["kcal"] = meal_kcal(meal)
    logger.info("요리 소진: %s → 재료 %d개 제거, 식단 기록 %s %dkcal",
                recipe["title"], len(removed), meal["type"], meal["kcal"])
    return jsonify({"ok": True, "removed": removed, "missing": missing, "meal": meal})


@app.errorhandler(ApiError)
def handle_api_error(e):
    return jsonify({"ok": False, "error": e.message}), 400


@app.errorhandler(404)
def handle_404(e):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "존재하지 않는 API 경로입니다."}), 404
    return render_template("404.html", page=""), 404


@app.errorhandler(500)
def handle_500(e):
    logger.exception("서버 오류: %s", request.path)
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "서버 내부 오류가 발생했습니다."}), 500
    return render_template("500.html", page=""), 500


@app.errorhandler(413)
def handle_too_large(e):
    return jsonify({"ok": False, "error": "요청 본문이 너무 큽니다."}), 413


@app.after_request
def add_security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return cookiestore.apply_writes(resp)


def json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError("JSON 본문이 필요합니다.")
    return data


def valid_date(s, field="날짜", limit_days=400):
    try:
        d = datetime.strptime(str(s), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        raise ApiError(f"{field} 형식은 YYYY-MM-DD 입니다.")
    if limit_days and abs((d - date.today()).days) > limit_days:
        raise ApiError(f"{field}는 오늘 기준 ±{limit_days}일 이내여야 합니다.")
    return d


def clean_text(v, field, min_len=1, max_len=40):
    s = str(v or "").strip()
    if not (min_len <= len(s) <= max_len):
        raise ApiError(f"{field}는 {min_len}~{max_len}자 사이여야 합니다.")
    return s


def parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def days_left(expiry_str):
    return (parse_date(expiry_str) - date.today()).days


def norm(name):
    """재료명 정규화: 공백 제거 + 동의어 통합(진간장→간장, 달걀→계란 등)."""
    return rd.norm_ingredient(name)


def resolve_nutrients(name):
    """재료 영양 DB 조회: 로컬 검증 DB(76종) → 식약처 실측 API → None."""
    return nutri.resolve(norm(name))[0]


# ---------------------------------------------------------------- 레시피 추천
def usual_diet_stats(meals):
    """평소 식단 통계: 자주 쓴 재료 상위 + 그 재료들이 자주 등장하는 레시피 종류."""
    item_freq = Counter()
    for m in meals:
        for it in m["items"]:
            item_freq[norm(it["name"])] += 1
    type_scores = Counter()
    for ing, cnt in item_freq.items():
        for r in RECIPES:
            if ing in {norm(i["name"]) for i in r["ingredients"]}:
                type_scores[r["type"]] += cnt
    usual_types = [t for t, _ in type_scores.most_common(2)]
    top_items = [{"name": n, "count": c} for n, c in item_freq.most_common(6)]
    return {"top_items": top_items, "usual_types": usual_types}


def recommend_recipes(limit=6):
    """(추천 결과, 알레르기로 제외된 레시피 수) 튜플 반환."""
    fridge = fridge_store.all()
    meals = meal_store.recent_days(21)
    nutrition = nutrition_summary(meal_store.recent_days(7))
    deficits = {d["key"] for d in nutrition["deficits"][:3]}
    usual_types = usual_diet_stats(meals)["usual_types"]
    allergies = user_allergies()

    fridge_set = {norm(f["name"]) for f in fridge}
    urgent_set = {norm(f["name"]) for f in fridge if days_left(f["expiry"]) <= 3}
    fridge_names = {f["name"] for f in fridge}

    results = []
    excluded = 0
    for r in RECIPES:
        if allergies and allergies.intersection(r["allergen_hits"]):
            excluded += 1  # 알레르기 재료가 든 레시피는 추천에서 제외
            continue
        ings = [i["name"] for i in r["ingredients"]]
        ings_norm = [norm(i) for i in ings]
        matched = [n for n in fridge_names if norm(n) in ings_norm]
        missing = [i for i, ni in zip(ings, ings_norm) if ni not in fridge_set]
        urgent_matched = [m for m in matched if norm(m) in urgent_set]
        boost = sorted(set(r["rich"]) & deficits)

        score = (len(matched) / max(len(ings), 1)) * 60          # 재료 커버리지
        score += min(len(urgent_matched) * 4, 20)                 # 임박 재료 소진 가점
        if r["type"] in usual_types:                              # 평소 식단 부합
            score += 12
        score += min(len(boost) * 8, 16)                          # 결핍 영양소 보충 가점
        score += min(r["views"], 1000) / 1000 * 5                 # 인기도

        results.append({
            **r,
            "score": round(score, 1),
            "matched": matched,
            "missing": missing,
            "urgent_matched": urgent_matched,
            "boost": [refdata.NUTRIENT_LABEL[b] for b in boost],
        })

    results.sort(key=lambda x: -x["score"])
    return results[:limit], excluded


# ---------------------------------------------------------------- 칼로리
def meal_kcal(meal):
    """재료·양 기준으로 kcal 계산(저장값 대신 항상 계산해 일관성 유지)."""
    kcal = 0.0
    for it in meal["items"]:
        db = resolve_nutrients(it["name"])
        if db:
            kcal += db.get("kcal", 0) * it["amount"] / 100
    return round(kcal)


def calorie_summary():
    target = profile_store.get().get("daily_kcal_target", 2100)
    meals = meal_store.recent_days(7)
    by_date = defaultdict(float)
    for m in meals:
        by_date[m["date"]] += meal_kcal(m)

    today = date.today()
    daily = []
    recorded = []
    for i in range(6, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        kcal = round(by_date.get(d, 0))
        daily.append({"date": d, "kcal": kcal})
        if kcal > 0:
            recorded.append(kcal)

    avg7 = round(sum(recorded) / len(recorded)) if recorded else 0
    # 간단 예측: 최근 기록일 3일 이동평균을 내일 섭취 예상치로 사용
    forecast = round(sum(recorded[-3:]) / min(3, len(recorded))) if recorded else 0
    today_kcal = round(by_date.get(today.isoformat(), 0))

    by_type = defaultdict(list)
    for m in meals:
        by_type[m["type"]].append(meal_kcal(m))
    type_avg = {t: round(sum(v) / len(v)) for t, v in by_type.items()}

    return {
        "target": target,
        "today": today_kcal,
        "today_pct": round(today_kcal / target * 100) if target else 0,
        "avg7": avg7,
        "forecast": forecast,
        "daily": daily,
        "type_avg": type_avg,
    }


# ---------------------------------------------------------------- 영양 분석
def nutrition_summary(meals, window=7):
    tot = {k: 0.0 for k in refdata.REFERENCE_INTAKE}
    unknown = Counter()
    for m in meals:
        for it in m["items"]:
            db = resolve_nutrients(it["name"])
            if not db:
                unknown[it["name"]] += 1
                continue
            scale = it["amount"] / 100
            for k in tot:
                tot[k] += db.get(k, 0) * scale

    avg = {k: round(v / window, 1) for k, v in tot.items()}
    rda = refdata.REFERENCE_INTAKE
    pct = {k: round(avg[k] / rda[k] * 100) for k in rda}

    rows = []
    for k in rda:
        p = pct[k]
        status = "부족" if p < 70 else ("과다" if p > 110 else "적정")
        rows.append({
            "key": k, "label": refdata.NUTRIENT_LABEL[k], "unit": refdata.NUTRIENT_UNIT[k],
            "avg": avg[k], "rda": rda[k], "pct": p, "status": status,
        })
    rows.sort(key=lambda x: x["pct"])

    score = round(sum(min(p, 110) for p in pct.values()) / len(pct) / 110 * 100)
    # 나트륨은 과잉이 문제인 영양소이므로 결핍 목록에서 제외(현황표에는 표시)
    deficits = [{
        "key": r["key"], "label": r["label"], "unit": r["unit"],
        "avg": r["avg"], "rda": r["rda"], "pct": r["pct"],
        "foods": refdata.DEFICIENCY_FOODS.get(r["key"], []),
    } for r in rows if r["status"] == "부족" and r["key"] not in ("kcal", "sodium")]

    return {
        "window_days": window,
        "score": score,
        "rows": rows,
        "deficits": deficits,
        "excess": [r for r in rows if r["status"] == "과다"],
        "unknown_items": dict(unknown),
    }


# ---------------------------------------------------------------- 프런트 페이지
@app.route("/")
def page_dashboard():
    return render_template("index.html", page="dashboard")


@app.route("/fridge")
def page_fridge():
    return render_template("fridge.html", page="fridge")


@app.route("/diet")
def page_diet():
    return render_template("diet.html", page="diet")


@app.route("/recipes")
def page_recipes():
    return render_template("recipes.html", page="recipes",
                           allergens=refdata.ALLERGENS,
                           saved_allergies=profile_store.get().get("allergies", []))


@app.route("/nutrition")
def page_nutrition():
    return render_template("nutrition.html", page="nutrition")


# ---------------------------------------------------------------- API: 냉장고
@app.get("/api/fridge")
def api_fridge_list():
    items = fridge_store.all()
    for it in items:
        it["days_left"] = days_left(it["expiry"])
    items.sort(key=lambda x: x["days_left"])
    return jsonify({
        "items": items,
        "summary": {
            "total": len(items),
            "urgent": sum(1 for i in items if i["days_left"] <= 2),
            "caution": sum(1 for i in items if 2 < i["days_left"] <= 7),
        },
    })


@app.post("/api/fridge")
def api_fridge_add():
    data = json_body()
    name = clean_text(data.get("name"), "재료명", 1, 40)
    category = data.get("category") or "기타"
    if category not in CATEGORIES:
        raise ApiError("분류 값이 올바르지 않습니다.")
    amount = str(data.get("amount") or "").strip()[:20]
    # 유통기한은 기간 제한 없음(과거 날짜 = 기한 지남 표시), 형식만 검증
    expiry = valid_date(data.get("expiry"), "유통기한", limit_days=None).isoformat()
    item = fridge_store.add(name, category, amount, expiry,
                            source=str(data.get("source") or "수동")[:20])
    item["days_left"] = days_left(item["expiry"])
    logger.info("냉장고 등록: %s (%s, D-%d)", name, category, item["days_left"])
    return jsonify({"ok": True, "item": item}), 201


@app.delete("/api/fridge/<item_id>")
def api_fridge_delete(item_id):
    ok = fridge_store.remove(item_id)
    logger.info("냉장고 삭제: %s (%s)", item_id, "성공" if ok else "대상 없음")
    return jsonify({"ok": ok})


@app.get("/api/meals")
def api_meals_list():
    days = min(max(request.args.get("days", 7, type=int), 1), 60)
    meals = sorted(meal_store.recent_days(days), key=lambda m: m["date"], reverse=True)
    for m in meals:
        m["kcal"] = meal_kcal(m)
    return jsonify({"meals": meals})


@app.post("/api/meals")
def api_meals_add():
    data = json_body()
    meal_date = valid_date(data.get("date"), "식사 날짜").isoformat()
    mtype = data.get("type") or "간식"
    if mtype not in MEAL_TYPES:
        raise ApiError("식사 구분은 아침/점심/저녁/간식 중 하나여야 합니다.")
    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not (1 <= len(raw_items) <= 20):
        raise ApiError("먹은 재료를 1~20개 입력하세요.")
    items = []
    for it in raw_items:
        if not isinstance(it, dict):
            raise ApiError("재료 항목 형식이 올바르지 않습니다.")
        try:
            amount = float(it.get("amount", 0))
        except (TypeError, ValueError):
            raise ApiError("재료 양은 숫자(g)로 입력하세요.")
        if not (1 <= amount <= 10000):
            raise ApiError("재료 양은 1~10000g 사이여야 합니다.")
        items.append({"name": clean_text(it.get("name"), "재료명", 1, 40),
                      "amount": amount})
    memo = str(data.get("memo") or "").strip()[:200]
    meal = meal_store.add(meal_date, mtype, items, memo)
    meal["kcal"] = meal_kcal(meal)
    logger.info("식단 기록: %s %s %dkcal (%d개 항목)", meal_date, mtype, meal["kcal"], len(items))
    return jsonify({"ok": True, "meal": meal, "calorie": calorie_summary()}), 201


@app.delete("/api/meals/<meal_id>")
def api_meals_delete(meal_id):
    return jsonify({"ok": meal_store.remove(meal_id)})


@app.get("/api/calorie/summary")
def api_calorie_summary():
    return jsonify(calorie_summary())


@app.get("/api/profile")
def api_profile_get():
    return jsonify({"ok": True, "profile": profile_store.get()})


@app.patch("/api/profile")
def api_profile_update():
    data = json_body()
    patch = {}
    if "name" in data:
        patch["name"] = clean_text(data.get("name"), "이름", 1, 20)
    if "daily_kcal_target" in data:
        try:
            target = int(data.get("daily_kcal_target"))
        except (TypeError, ValueError):
            raise ApiError("목표 칼로리는 숫자로 입력하세요.")
        if not (800 <= target <= 5000):
            raise ApiError("목표 칼로리는 800~5000kcal 사이로 입력하세요.")
        patch["daily_kcal_target"] = target
    if "notes" in data:
        patch["notes"] = str(data.get("notes") or "").strip()[:300]
    if "allergies" in data:
        raw = data.get("allergies")
        if not isinstance(raw, list):
            raise ApiError("allergies는 알레르기 key 목록이어야 합니다.")
        valid = {a["key"] for a in refdata.ALLERGENS}
        patch["allergies"] = sorted({str(k).strip() for k in raw if str(k).strip() in valid})
    if not patch:
        raise ApiError("수정할 항목이 없습니다. name/daily_kcal_target/notes/allergies 중 하나를 보내세요.")
    profile = profile_store.update(patch)
    logger.info("프로필 수정: %s", ", ".join(patch.keys()))
    return jsonify({"ok": True, "profile": profile})


# ---------------------------------------------------------------- API: 레시피
@app.get("/api/recipes/recommend")
def api_recipes_recommend():
    limit = min(max(request.args.get("limit", 6, type=int), 1), 30)
    recs, excluded = recommend_recipes(limit)
    allergies = user_allergies()
    return jsonify({"recipes": recs,
                    "usual": usual_diet_stats(meal_store.recent_days(21)),
                    "allergies": sorted(allergies),
                    "allergy_excluded": excluded})


# ---------------------------------------------------------------- API: 영양
@app.get("/api/nutrition/summary")
def api_nutrition_summary():
    days = min(max(request.args.get("days", 7, type=int), 1), 60)
    return jsonify(nutrition_summary(meal_store.recent_days(days), window=days))


@app.get("/api/nutrition/lookup")
def api_nutrition_lookup():
    """재료 1건의 영양 성분 조회 — 로컬 검증 DB → 식약처 실측 API 순으로 확인."""
    name = clean_text(request.args.get("name"), "재료명", 1, 40)
    nutrients, source = nutri.resolve(norm(name))
    return jsonify({"ok": True, "name": name, "source": source, "nutrients": nutrients})


# ---------------------------------------------------------------- API: 대시보드 통합
@app.get("/api/dashboard")
def api_dashboard():
    fridge = fridge_store.all()
    for it in fridge:
        it["days_left"] = days_left(it["expiry"])
    urgent = sorted([i for i in fridge if i["days_left"] <= 3], key=lambda x: x["days_left"])

    cal = calorie_summary()
    nut = nutrition_summary(meal_store.recent_days(7))
    recs, _ = recommend_recipes(3)
    stats = usual_diet_stats(meal_store.recent_days(21))

    return jsonify({
        "fridge": {
            "total": len(fridge),
            "urgent": urgent[:5],
            "urgent_count": len(urgent),
            "caution_count": sum(1 for i in fridge if 3 < i["days_left"] <= 7),
        },
        "calorie": cal,
        "deficits": nut["deficits"][:3],
        "nutrition_score": nut["score"],
        "recipes": recs,
        "usual": stats,
    })


def main():
    logger.info("냉장고 털이 AI 시작 — http://%s:%s (레시피 %d건)", HOST, PORT, len(RECIPES))
    try:
        from waitress import serve
        serve(app, host=HOST, port=PORT, threads=8, clear_untrusted_proxy_headers=True)
    except ImportError:
        logger.warning("waitress 미설치 — Flask 개발 서버로 대체 실행")
        app.run(host=HOST, port=PORT, debug=DEBUG)


if __name__ == "__main__":
    main()
