# -*- coding: utf-8 -*-
"""
엔드투엔드 스모크 테스트 (표준 라이브러리만 사용).

사전 조건: 서버가 실행 중이어야 함  →  python app.py  (기본 http://127.0.0.1:5000)
실행: python smoke_test.py [base_url]
"""
import http.cookiejar
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5000"
TODAY = date.today()
_results = []

# 쿠키 저장소(식단·프로필) 플로우까지 검증하기 위해 쿠키를 유지하는 opener 사용
_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))


def call(method, path, body=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method=method,
    )
    try:
        with _opener.open(req, timeout=15) as res:
            raw = res.read()
            data = json.loads(raw) if raw.startswith(b"{") else None
            return res.status, data
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, None


def check(name, ok, detail=""):
    _results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")


def main():
    pages = ["/", "/fridge", "/diet", "/recipes", "/nutrition"]
    for p in pages:
        st, _ = call("GET", p)
        check(f"페이지 {p}", st == 200, f"({st})")

    st, _ = call("GET", "/no-such-page")
    check("404 처리", st == 404, f"({st})")

    st, d = call("GET", "/api/dashboard")
    check("대시보드 API", st == 200 and "fridge" in d and "calorie" in d)

    st, d = call("GET", "/api/fridge")
    check("냉장고 API", st == 200 and isinstance(d.get("items"), list))
    fridge_count = len(d["items"])

    st, d = call("GET", "/api/recipes/recommend?limit=5")
    check("레시피 추천 API", st == 200 and len(d.get("recipes", [])) == 5)
    if d.get("recipes"):
        r0 = d["recipes"][0]
        check("레시피 URL 존재", r0.get("url", "").startswith("http"))
    st, d = call("GET", "/api/recipes/recommend?limit=10")
    ings_all = [i for r in d.get("recipes", []) for i in r.get("ingredients", [])]
    check("1인분 기준 재료량(grams_1p) 포함",
          bool(ings_all) and all(i.get("grams_1p", 0) >= 1 for i in ings_all),
          f"({len(ings_all)}개 재료)")

    st, d = call("GET", "/api/nutrition/summary?days=7")
    check("영양 분석 API", st == 200 and "rows" in d and "deficits" in d)

    st, d = call("GET", "/api/calorie/summary")
    check("칼로리 API", st == 200 and "daily" in d and "forecast" in d)

    # 냉장고 등록 → 확인 → 삭제
    expiry = (TODAY + timedelta(days=5)).isoformat()
    st, d = call("POST", "/api/fridge", {"name": "스모크테스트재료", "category": "기타",
                                        "amount": "1개", "expiry": expiry, "source": "스모크"})
    ok_add = st == 201 and d.get("item", {}).get("name") == "스모크테스트재료"
    check("냉장고 등록", ok_add, f"({st})")
    item_id = d.get("item", {}).get("id")
    st, _ = call("DELETE", f"/api/fridge/{item_id}")
    check("냉장고 삭제", st in (200, 204), f"({st})")
    st, d = call("GET", "/api/fridge")
    check("냉장고 개수 복원", len(d["items"]) == fridge_count)

    # 유통기한 기간 제한 없음(오늘 +800일 등록 성공)
    far_future = (TODAY + timedelta(days=800)).isoformat()
    st, d = call("POST", "/api/fridge", {"name": "장기보관재료", "category": "기타",
                                        "amount": "1개", "expiry": far_future, "source": "스모크"})
    ok_far = st == 201 and d.get("item", {}).get("expiry") == far_future
    check("유통기한 기간 제한 없음(+800일)", ok_far, f"({st})")
    if ok_far:
        call("DELETE", f"/api/fridge/{d['item']['id']}")

    # 식단 등록 → 삭제 + 잘못된 입력 400 검증
    st, d = call("POST", "/api/meals", {"date": TODAY.isoformat(), "type": "점심",
                                        "items": [{"name": "계란", "amount": 60}]})
    check("식단 등록", st == 201 and d.get("meal", {}).get("kcal", 0) > 0, f"({st})")
    meal_id = d.get("meal", {}).get("id")
    st, d = call("GET", "/api/meals?days=7")
    check("쿠키 식단 유지(쿠키 왕복)", any(m.get("id") == meal_id for m in d.get("meals", [])))
    st, _ = call("DELETE", f"/api/meals/{meal_id}")
    check("식단 삭제", st in (200, 204), f"({st})")

    st, _ = call("POST", "/api/meals", {"date": "not-a-date", "type": "점심",
                                        "items": [{"name": "밥", "amount": 100}]})
    check("식단 유효성 검증(400)", st == 400, f"({st})")
    st, _ = call("POST", "/api/fridge", {"name": "", "expiry": "2026-99-99"})
    check("냉장고 유효성 검증(400)", st == 400, f"({st})")

    # 요리했어요 → 레시피 전체 재료 식단 기록 + 냉장고 소진
    st, d = call("POST", "/api/fridge/consume", {"recipe_index": 999999})
    check("요리했어요 존재하지 않는 레시피(400)", st == 400, f"({st})")
    st, d = call("GET", "/api/recipes/recommend?limit=10")
    target = next((r for r in d.get("recipes", [])
                   if r.get("missing") and r["missing"][0] and len(r["missing"][0]) <= 40
                   and len(r.get("ingredients", [])) >= 3), None)
    if target:
        ing = target["missing"][0]
        st, d2 = call("POST", "/api/fridge", {"name": ing, "category": "기타",
                                             "amount": "1개", "expiry": expiry, "source": "스모크"})
        check("요리 테스트용 재료 등록", st == 201, f"({st})")
        st, d2 = call("POST", "/api/fridge/consume", {"recipe_index": target["index"]})
        meal = d2.get("meal") or {}
        raw_names = [i["name"] for i in target.get("ingredients", [])]
        item_names = [i["name"] for i in meal.get("items", [])]
        check("요리했어요 → 냉장고 재료 소진",
              st == 200 and ing in d2.get("removed", []) and ing in item_names, f"({st})")
        check("레시피 전체 재료가 식단에 기록됨",
              set(item_names) <= set(raw_names)
              and len(item_names) >= max(1, len(set(raw_names)) - 1),
              f"({len(item_names)}항목 / 레시피 {len(set(raw_names))}종)")
        meal_id = meal.get("id")
        st, d3 = call("GET", "/api/meals?days=7")
        recorded = next((m for m in d3.get("meals", []) if m.get("id") == meal_id), None)
        check("기록된 식단이 식단 내역에 유지됨",
              bool(recorded) and recorded.get("memo", "").startswith("레시피:")
              and recorded.get("kcal", -1) >= 0, f"({st})")
        if meal_id:
            call("DELETE", f"/api/meals/{meal_id}")  # 사용자 데이터 오염 방지
    else:
        check("요리했어요 → 냉장고 재료 소진", False, "(대상 레시피 없음)")
        check("레시피 전체 재료가 식단에 기록됨", False, "(대상 없음)")

    # 식약처 영양 DB 조회(로컬 76종 → API 33만건 폴백)
    st, d = call("GET", "/api/nutrition/lookup?name=" + urllib.parse.quote("계란"))
    nut = d.get("nutrients") or {}
    check("영양 조회 API(로컬 DB)",
          st == 200 and d.get("source") in ("local", "api")
          and (not nut or "kcal" in nut), f"(source={d.get('source')})")
    st, d = call("GET", "/api/nutrition/lookup?name=")
    check("영양 조회 유효성 검증(400)", st == 400, f"({st})")

    # 프로필 검증
    st, before = call("GET", "/api/profile")
    orig_target = ((before or {}).get("profile") or {}).get("daily_kcal_target", 2100)
    st, d = call("PATCH", "/api/profile", {"daily_kcal_target": 2000})
    check("프로필 수정", st == 200 and d.get("profile", {}).get("daily_kcal_target") == 2000, f"({st})")
    st, _ = call("PATCH", "/api/profile", {"daily_kcal_target": -5})
    check("프로필 유효성 검증(400)", st == 400, f"({st})")
    # 테스트가 사용자 설정을 바꾸지 않도록 원래 값으로 복원
    call("PATCH", "/api/profile", {"daily_kcal_target": orig_target})
    st, d = call("GET", "/api/profile")
    check("프로필 원복", (d.get("profile") or {}).get("daily_kcal_target") == orig_target)

    # 알레르기 설정 → 레시피 추천 제외
    st, d = call("PATCH", "/api/profile", {"allergies": ["egg", "bogus-key"]})
    check("알레르기 설정 저장(무효 key 제거)",
          st == 200 and d.get("profile", {}).get("allergies") == ["egg"], f"({st})")
    st, d = call("GET", "/api/recipes/recommend?limit=30")
    has_egg = [r for r in d.get("recipes", [])
               if any(("계란" in i["name"] or "달걀" in i["name"]) for i in r["ingredients"])]
    check("계란 알레르기 레시피 추천 제외",
          st == 200 and not has_egg and d.get("allergy_excluded", 0) > 0
          and d.get("allergies") == ["egg"],
          f"(제외 {d.get('allergy_excluded')}건)")
    st, _ = call("PATCH", "/api/profile", {"allergies": "egg"})
    check("알레르기 유효성 검증(400)", st == 400, f"({st})")
    st, d = call("PATCH", "/api/profile", {"allergies": []})
    check("알레르기 설정 초기화", st == 200 and d.get("profile", {}).get("allergies") == [], f"({st})")

    fails = [r for r in _results if not r[1]]
    print(f"\n=== 스모크 테스트: {len(_results) - len(fails)}/{len(_results)} 통과 ===")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
