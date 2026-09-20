# -*- coding: utf-8 -*-
"""
레시피 데이터 로더.

- data/recipe_main.csv (crawler.py 산출물, hye1ns/datanalysis_recipe 스키마)를
  읽어 앱 구조로 변환. CSV가 없으면 refdata.RECIPES 목업으로 폴백.
- 재료 매칭용 정규화: 공백 제거 + 흔한 동의어 통합(진간장→간장, 달걀→계란 등).
- "rich"(영양 보충 포인트)는 NUTRIENTS DB에서 재료별 우수 영양소를 집계해 자동 산출.
"""
import ast
import csv
import os
import re

import refdata

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CSV_PATH = os.path.join(DATA_DIR, "recipe_main.csv")

# 재료명 통합용 최소 동의어 맵 (매칭 정확도 목적)
INGREDIENT_ALIASES = {
    "진간장": "간장", "국간장": "간장", "양간장": "간장", "조선간장": "간장",
    "달걀": "계란", "계란물": "계란", "계란노른자": "계란", "계란흰자": "계란",
    "다진마늘": "마늘", "마늘종": "마늘", "통마늘": "마늘",
    "파": "대파", "다진파": "대파", "쪽파": "대파",
    "식용유": "참기름", "콩기름": "참기름",
    "돼지고기목심": "돼지고기", "돼지고기앞다리": "돼지고기", "돼지고기삼겹살": "돼지고기",
    "다진소고기": "소고기", "소고기안심": "소고기", "소고기등심": "소고기", "소고기다짐육": "소고기",
    "닭가슴살": "닭고기", "닭다리살": "닭고기", "닭": "닭고기",
    "고추가루": "고춧가루",
    "후춧가루": "후추", "통후추": "후추",
    "깨소금": "깨", "통깨": "깨", "참깨": "깨",
    "슬라이스치즈": "치즈", "모짜렐라치즈": "치즈", "모짜렐라": "치즈", "피자치즈": "치즈",
    "체다치즈": "치즈", "슈레드치즈": "치즈",
    "올리브오일": "올리브유", "그레인오일": "올리브유",
    "무염버터": "버터", "가염버터": "버터",
    "김가루": "김", "조미김": "김",
    "박력분": "밀가루", "중력분": "밀가루", "강력분": "밀가루", "전분가루": "밀가루",
    "연두부": "두부", "순두부": "두부",
    "캔옥수수": "옥수수", "옥수수캔": "옥수수",
    "케첩": "케찹", "토마토케찹": "케찹",
    "미림": "맛술", "맛미림": "맛술",
    "올리고당": "물엿", "액상과당": "물엿",
    "표고버섯": "버섯", "팽이버섯": "버섯", "느타리버섯": "버섯", "새송이버섯": "버섯",
    "양송이버섯": "버섯", "만가닥버섯": "버섯",
    "참치캔": "참치", "참치통조림": "참치",
    "파슬리가루": "파슬리", "이탈리안파슬리": "파슬리",
    "우동면": "면", "소면": "면", "국수": "면", "감자면": "면",
}

# 크롤링 노이즈: 재료 목록에 섞이는 UI 텍스트
NOISE_INGREDIENTS = {"구매", "쇼핑", "닫기", "주문", "더보기", "묶음", "바로가기"}

# 재료명 뒤에 붙는 크기/상태 수식어 ("감자큰것" → "감자")
_SIZE_SUFFIX_RE = re.compile(r"(큰것|작은것|중간것|중간크기|작은거)$")


def norm_ingredient(name):
    """재료명 정규화: 공백 제거 → 크기 수식어 제거 → 동의어 통합."""
    key = (name or "").replace(" ", "").strip()
    key = _SIZE_SUFFIX_RE.sub("", key)
    return INGREDIENT_ALIASES.get(key, key)


# 결핍 보충 가점 대상 영양소(여기에 없는 영양소는 rich 집계에서 제외)
RICH_KEYS = {"protein", "fiber", "calcium", "iron", "vitA", "vitC"}


def find_allergen_hits(ingredient_names):
    """재료명 목록에서 알레르기 유발 key 집합을 찾는다.

    refdata.ALLERGENS 키워드와 양방향 부분 일치: 키워드가 재료명에 포함되거나
    짧은 재료명("닭")이 키워드("닭고기")에 포함될 때도 hit로 본다.
    not_with 가드로 합성물 오탐("땅콩버터"≠유제품)을 제외한다.
    """
    names = [norm_ingredient(n) for n in ingredient_names if n]
    hits = set()
    for a in refdata.ALLERGENS:
        guards = a.get("not_with", {})
        for kw in a["keywords"]:
            blockers = guards.get(kw, [])
            if any((kw in n or n in kw) and not any(b in n for b in blockers)
                   for n in names):
                hits.add(a["key"])
                break
    return hits


def _scale_amount_text(raw_txt, scale):
    """'120g' -> '60g', '2모' -> '0.5모' 등 재료양 문자열을 1인분 비율로 환산."""
    s = str(raw_txt or "").strip()
    if not s or scale == 1.0:
        return s
    m = _AMOUNT_NUM_RE.search(s.replace(" ", ""))
    if not m:
        return s
    num_txt = m.group(1)
    try:
        if "/" in num_txt:
            a, b = num_txt.split("/", 1)
            v = float(a) / float(b)
        else:
            v = float(num_txt)
    except (ZeroDivisionError, ValueError):
        return s
    new_v = v * scale
    unit = s.replace(" ", "")[m.end():]
    # 읽기 좋은 포맷: 정수면 정수, 소수면 1~2자리
    v_str = f"{int(new_v)}" if abs(new_v - round(new_v)) < 0.01 else f"{new_v:.1f}".rstrip("0").rstrip(".")
    return f"{v_str}{unit}" if unit else v_str


def _normalize_to_single_serving(ingredients, servings):
    """레시피의 재료양을 1인분 기준으로 변환한다.

    - servings가 0 또는 1이면 원본 유지
    - servings >= 2 (예: 4인분)이면 각 재료양을 1/servings로 나눔
    - 원래 표기(amount_raw)와 1인분당 g(grams_1p)도 함께 돌려준다
    """
    scale = 1.0 / servings if (servings and servings >= 2) else 1.0
    out = []
    for ing in ingredients:
        raw_amt = ing.get("amount", "")
        # 전체 분량 기준 g 환산값 / servings = 1인분당 g
        total_g = amount_to_grams(raw_amt)
        per_g = max(1, round(total_g * scale))
        per_txt = _scale_amount_text(raw_amt, scale) if scale != 1.0 else raw_amt
        out.append({
            "name": ing["name"],
            "amount": per_txt,
            "amount_raw": raw_amt,
            "grams_1p": per_g,
        })
    return out

# 단위 startswith 판정이므로 길이·특수 단위를 앞세움 (kg→g, ml→l, 밥숟가락→…)
_AMOUNT_UNIT_GRAMS = (
    ("kg", 1000), ("킬로그램", 1000), ("리터", 1000), ("L", 1000),
    ("ml", 1), ("밀리리터", 1), ("g", 1), ("그램", 1),
    ("밥숟가락", 15), ("큰술", 15), ("숟가락", 15), ("숟갈", 15), ("T", 15),
    ("작은술", 5), ("티스푼", 5), ("t", 5),
    ("모", 400), ("공기", 210), ("컵", 200), ("종이컵", 200),
    ("인분", 300), ("묶음", 150), ("주먹", 150),
    ("장", 15), ("조각", 30), ("개", 100),
)
_AMOUNT_NUM_RE = re.compile(r"(\d+(?:\.\d+)?(?:/\d+)?)")


def amount_to_grams(raw, default=100):
    """재료양 문자열("120g", "1/2모", "2큰술", "1.5T" …)을 g 근사치로 환산.

    숫자나 단위를 알 수 없으면 default(100g)로 둔다.
    """
    s = str(raw or "").replace(" ", "")
    m = _AMOUNT_NUM_RE.search(s)
    if not m:
        return default
    num_txt = m.group(1)
    try:
        if "/" in num_txt:
            a, b = num_txt.split("/", 1)
            value = float(a) / float(b)
        else:
            value = float(num_txt)
    except (ZeroDivisionError, ValueError):
        return default
    unit = s[m.end():]
    for u, grams in _AMOUNT_UNIT_GRAMS:
        if unit.startswith(u):
            return max(1, min(int(value * grams), 10000))
    return default


def _parse_list(s):
    """CSV 셀의 "['a', 'b']" 문자열 → 리스트."""
    if not s:
        return []
    s = s.strip()
    if s.startswith("["):
        try:
            return list(ast.literal_eval(s))
        except (ValueError, SyntaxError):
            return []
    return [s] if s else []


def _parse_int(s, default=0):
    digits = "".join(ch for ch in (s or "") if ch.isdigit())
    return int(digits) if digits else default


def _compute_rich(ingredient_names):
    """레시피 재료가 NUTRIENTS DB에서 두드러지는 영양소 key 집합(최대 3개)."""
    score = {}
    for name in ingredient_names:
        db = refdata.NUTRIENTS.get(norm_ingredient(name))
        if not db:
            continue
        for k in RICH_KEYS:
            rda = refdata.REFERENCE_INTAKE[k]
            score[k] = score.get(k, 0) + db.get(k, 0) / rda
    top = sorted(score.items(), key=lambda kv: -kv[1])[:3]
    return [k for k, v in top if v > 0]


def load_recipes():
    """recipe_main.csv를 앱 구조로 변환. 실패 시 목업 폴백."""
    if not os.path.exists(CSV_PATH):
        return [dict(r, url=_mock_search_url(r["title"]),
                     allergen_hits=sorted(find_allergen_hits(
                         [i["name"] for i in r["ingredients"]])),
                     ingredients=_normalize_to_single_serving(
                         r["ingredients"], r.get("servings")))
                for r in refdata.RECIPES]

    recipes = []
    with open(CSV_PATH, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            names = [n for n in _parse_list(row.get("재료")) if n]
            names = [n for n in names if n.replace(" ", "").strip() not in NOISE_INGREDIENTS]
            if not names:
                continue
            amounts = [a for a in _parse_list(row.get("재료양", ""))]
            while len(amounts) < len(names):
                amounts.append("")
            servings = _parse_int(row.get("인분"), 0)
            ingredients = _normalize_to_single_serving(
                [{"name": n, "amount": a} for n, a in zip(names, amounts)], servings)
            steps = _parse_list(row.get("조리순서"))
            recipes.append({
                "index": _parse_int(row.get("index"), len(recipes) + 1),
                "title": (row.get("제목") or "").strip() or "제목 없음",
                "type": (row.get("종류별") or "").strip(),
                "situation": (row.get("상황별") or "").strip(),
                "ingredient_tag": (row.get("재료별") or "").strip(),
                "method": (row.get("방법별") or "").strip(),
                "url": (row.get("url") or "").strip(),
                "views": _parse_int(row.get("조회수")),
                "chef": (row.get("셰프") or "").strip(),
                "servings": servings,
                "cook_time": (row.get("조리시간") or "").strip(),
                "difficulty": (row.get("난이도") or "").strip(),
                "ingredients": ingredients,
                "intro": (row.get("인트로") or "").strip(),
                "steps": steps,
                "tags": [t for t in _parse_list(row.get("해시태그")) if t],
                "rich": _compute_rich(names),
                "allergen_hits": sorted(find_allergen_hits(names)),
            })
    recipes.sort(key=lambda r: -r["views"])
    for i, r in enumerate(recipes, 1):
        r["index"] = i
    return recipes


def _mock_search_url(title):
    import urllib.parse
    return "https://www.10000recipe.com/recipe/list?q=" + urllib.parse.quote(title)
