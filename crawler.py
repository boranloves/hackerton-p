# -*- coding: utf-8 -*-
"""
만개의 레시피(10000recipe.com) 크롤러 — hye1ns/datanalysis_recipe의
p1_blog_analysis/crowling.py 방식을 따르되, 2026년 기준 HTML 구조에 맞게
셀렉터를 갱신하고 범위를 제한(카테고리별 상위 N개)한 버전.

출력: data/recipe_main.csv  (레포 스키마 16컬럼 + 재료양 1컬럼 추가)
실행: python crawler.py [--per-category 8] [--pages 1]
"""
import argparse
import ast
import csv
import os
import sys
import time

import requests
from bs4 import BeautifulSoup

BASE = "https://www.10000recipe.com"
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

# 종류별(cat4) — 레포 crowling.py의 매핑 사용
BY_TYPE = {
    "밑반찬": "63", "메인반찬": "56", "국/탕": "54", "찌개": "55", "디저트": "60",
    "면/만두": "53", "밥/죽/떡": "52", "퓨전": "61", "양념/잼/소스": "58", "양식": "65",
    "샐러드": "64", "스프": "68", "빵": "66", "과자": "69", "차/음료/술": "59",
}

COLUMNS = ["index", "종류별", "상황별", "재료별", "방법별", "제목", "url", "조회수",
           "셰프", "인분", "조리시간", "난이도", "재료", "인트로", "조리순서",
           "해시태그", "재료양"]

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
OUT_PATH = os.path.join(DATA_DIR, "recipe_main.csv")


def fetch(url, session, retries=2):
    for attempt in range(retries + 1):
        try:
            r = session.get(url, headers=UA, timeout=12)
            if r.status_code == 200:
                return r
        except requests.RequestException:
            pass
        time.sleep(1.5 * (attempt + 1))
    return None


def list_recipe_urls(session, cat4, pages):
    urls = []
    for page in range(1, pages + 1):
        list_url = (f"{BASE}/recipe/list.html?q=&query=&cat4={cat4}"
                    f"&order=reco&lastcate=cat4&dsearch=&copyshot=&scrap=&degree="
                    f"&portion=&time=&niresource=&page={page}")
        r = fetch(list_url, session)
        if r is None:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        found = 0
        for a in soup.select("a.common_sp_link"):
            href = a.get("href", "")
            if href.startswith("/recipe/"):
                urls.append(BASE + href)
                found += 1
        if found == 0:
            break
        time.sleep(1.2)
    return urls


def parse_detail(url, session):
    r = fetch(url, session)
    if r is None:
        return None
    soup = BeautifulSoup(r.text, "html.parser")

    title_el = soup.select_one("div.view2_summary h3")
    if title_el is None:
        return None
    title = title_el.get_text(strip=True)

    views_el = soup.select_one(".view2_pic .view_cate.st2 .hit")
    views = views_el.get_text(strip=True) if views_el else ""

    chef_el = soup.select_one(".user_info2_name")
    chef = chef_el.get_text(strip=True) if chef_el else ""

    info = soup.select(".view2_summary_info span")
    def info_text(i):
        try:
            return info[i].get_text(strip=True)
        except IndexError:
            return ""

    names, amounts = [], []
    for ul in soup.select("#divConfirmedMaterialArea ul"):
        for li in ul.find_all("li"):
            a = li.find("a")
            span = li.find("span")
            name = a.get_text(strip=True) if a else li.get_text(strip=True).split("\n")[0]
            if not name:
                continue
            names.append(name)
            amounts.append(span.get_text(strip=True) if span else "")

    intro_el = soup.select_one("#recipeIntro")
    intro = intro_el.get_text("\n", strip=True) if intro_el else ""

    steps = []
    i = 1
    while soup.select_one(f"#stepdescr{i}"):
        steps.append(soup.select_one(f"#stepdescr{i}").get_text("\n", strip=True))
        i += 1

    tags = [a.get_text(strip=True).lstrip("#") for a in soup.select(".view_tag a")]

    if not names or not steps:
        return None

    return {
        "제목": title, "url": url, "조회수": views, "셰프": chef,
        "인분": info_text(0), "조리시간": info_text(1), "난이도": info_text(2),
        "재료": names, "인트로": intro, "조리순서": steps, "해시태그": tags,
        "재료양": amounts,
    }


def crawl(per_category, pages):
    os.makedirs(DATA_DIR, exist_ok=True)
    session = requests.Session()
    rows, idx = [], 1
    for type_ko, cat4 in BY_TYPE.items():
        links = list_recipe_urls(session, cat4, pages)[:per_category]
        print(f"[{type_ko}] {len(links)}개 후보", flush=True)
        for link in links:
            d = parse_detail(link, session)
            if d is None:
                print(f"  SKIP {link}", flush=True)
                continue
            rows.append([idx, type_ko, "", "", "", d["제목"], d["url"], d["조회수"],
                         d["셰프"], d["인분"], d["조리시간"], d["난이도"],
                         str(d["재료"]), d["인트로"], str(d["조리순서"]),
                         str(d["해시태그"]), str(d["재료양"])])
            print(f"  #{idx} {d['제목'][:40]}  재료{len(d['재료'])}개", flush=True)
            idx += 1
            time.sleep(0.7)
    with open(OUT_PATH, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows([COLUMNS] + rows)
    print(f"완료: {len(rows)}개 → {OUT_PATH}", flush=True)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # 콘솔 cp949 한계 방지
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-category", type=int, default=8)
    ap.add_argument("--pages", type=int, default=1)
    args = ap.parse_args()
    crawl(args.per_category, args.pages)


if __name__ == "__main__":
    main()
