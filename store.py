# -*- coding: utf-8 -*-
"""
영속화 레이어 (SQLite).

- data/fridge_ai.db 단일 파일에 냉장고·식단·프로필을 저장 (sqlite3 표준 라이브러리).
- 기존 data/*.json이 있으면 최초 1회 자동 이관하고, 원본 JSON은 백업으로 남긴다.
- DB가 비어 있고 JSON도 없으면 데모 시드를 생성. 유통기한·식단 날짜는 실행 시점 기준
  상대 날짜로 시딩해 데모가 항상 '산' 상태로 보이게 한다.
- 저장소 인터페이스(FridgeStore/MealStore/ProfileStore)는 JSON 버전과 동일하므로
  app.py는 변경 없이 동작한다.
"""
import json
import os
import sqlite3
import uuid
from datetime import date, datetime, timedelta

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "fridge_ai.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fridge (
    id        TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    category  TEXT,
    amount    TEXT,
    expiry    TEXT,
    source    TEXT,
    added_at  TEXT
);
CREATE TABLE IF NOT EXISTS meals (
    id    TEXT PRIMARY KEY,
    date  TEXT NOT NULL,
    type  TEXT,
    memo  TEXT,
    items TEXT
);
CREATE TABLE IF NOT EXISTS profile (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    name              TEXT,
    daily_kcal_target REAL,
    notes             TEXT
);
CREATE INDEX IF NOT EXISTS idx_fridge_expiry ON fridge(expiry);
CREATE INDEX IF NOT EXISTS idx_meals_date ON meals(date);
"""

BACKUP_DIR = os.path.join(DATA_DIR, "backups")
BACKUP_KEEP = 7

_ensure_flag = False


def _today():
    return date.today()


def _read_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _backup_if_stale():
    """24시간 경과 시 DB 자동 백업, 최근 7개 유지."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    existing = sorted(f for f in os.listdir(BACKUP_DIR) if f.endswith(".db"))
    if existing:
        newest = os.path.getmtime(os.path.join(BACKUP_DIR, existing[-1]))
        if datetime.now().timestamp() - newest < 20 * 3600:
            return
    stamp = _today().isoformat().replace("-", "") + "_" + datetime.now().strftime("%H%M")
    dst = os.path.join(BACKUP_DIR, f"fridge_ai_{stamp}.db")
    try:
        conn = _connect()
        try:
            conn.execute("VACUUM INTO ?", (dst,))
        finally:
            conn.close()
        for old in existing[:-BACKUP_KEEP + 1]:
            os.remove(os.path.join(BACKUP_DIR, old))
    except sqlite3.Error:
        pass  # 백업 실패는 서비스 동작을 막지 않음


def _ensure():
    """DB 생성 + JSON 이관/시드. 멱등, 프로세스당 1회."""
    global _ensure_flag
    if _ensure_flag:
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = _connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        _migrate(conn)
        _seed_if_empty(conn)
    finally:
        conn.close()
    _backup_if_stale()
    _ensure_flag = True


def _migrate(conn):
    """기존 data/*.json → SQLite 1회 이관 (원본 JSON은 백업으로 유지)."""
    old_fridge = _read_json(os.path.join(DATA_DIR, "fridge.json"), [])
    if old_fridge and conn.execute("SELECT COUNT(*) FROM fridge").fetchone()[0] == 0:
        for r in old_fridge:
            conn.execute(
                "INSERT OR IGNORE INTO fridge VALUES (?,?,?,?,?,?,?)",
                (r.get("id"), r.get("name", ""), r.get("category", ""),
                 r.get("amount", ""), r.get("expiry", ""), r.get("source", "수동"),
                 r.get("added_at", "")),
            )
    old_meals = _read_json(os.path.join(DATA_DIR, "meals.json"), [])
    if old_meals and conn.execute("SELECT COUNT(*) FROM meals").fetchone()[0] == 0:
        for m in old_meals:
            conn.execute(
                "INSERT OR IGNORE INTO meals VALUES (?,?,?,?,?)",
                (m.get("id"), m.get("date", ""), m.get("type", ""), m.get("memo", ""),
                 json.dumps(m.get("items", []), ensure_ascii=False)),
            )
    old_profile = _read_json(os.path.join(DATA_DIR, "profile.json"), None)
    if old_profile and conn.execute("SELECT COUNT(*) FROM profile").fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO profile VALUES (1, ?, ?, ?)",
            (old_profile.get("name", "사용자"),
             old_profile.get("daily_kcal_target", 2100),
             old_profile.get("notes", "")),
        )
    conn.commit()


def _seed_if_empty(conn):
    if conn.execute("SELECT COUNT(*) FROM fridge").fetchone()[0] == 0:
        for r in _seed_fridge():
            conn.execute(
                "INSERT INTO fridge VALUES (?,?,?,?,?,?,?)",
                (r["id"], r["name"], r["category"], r["amount"],
                 r["expiry"], r["source"], r["added_at"]),
            )
    if conn.execute("SELECT COUNT(*) FROM meals").fetchone()[0] == 0:
        for m in _seed_meals():
            conn.execute(
                "INSERT INTO meals VALUES (?,?,?,?,?)",
                (m["id"], m["date"], m["type"], m["memo"],
                 json.dumps(m["items"], ensure_ascii=False)),
            )
    if conn.execute("SELECT COUNT(*) FROM profile").fetchone()[0] == 0:
        p = _seed_profile()
        conn.execute(
            "INSERT INTO profile VALUES (1, ?, ?, ?)",
            (p["name"], p["daily_kcal_target"], p["notes"]),
        )
    conn.commit()


def _seed_fridge():
    t = _today()
    # (이름, 분류, 양, D-day, 등록경로)
    rows = [
        ("두부", "채소류", "300g", 1, "수동"),
        ("우유", "유제품", "1L", 2, "수동"),
        ("돼지고기", "축산류", "400g", 2, "수동"),
        ("시금치", "채소류", "1단", 3, "수동"),
        ("어묵", "해물류", "2장", 4, "수동"),
        ("계란", "계란", "10개", 5, "수동"),
        ("애호박", "채소류", "1개", 7, "수동"),
        ("김치", "채소류", "800g", 10, "수동"),
        ("양파", "채소류", "3개", 14, "수동"),
        ("당근", "채소류", "2개", 15, "수동"),
        ("감자", "채소류", "5개", 20, "수동"),
        ("토마토", "채소류", "3개", 6, "수동"),
        ("오이", "채소류", "3개", 9, "수동"),
        ("바나나", "과일", "4송이", 4, "수동"),
        ("김", "기타", "1봉", 30, "수동"),
        ("밥", "곡류", "냉동 2공기", 21, "수동"),
        ("간장", "기타", "1병", 90, "수동"),
        ("참기름", "기타", "1병", 180, "수동"),
        ("마늘", "채소류", "1팩", 25, "수동"),
        ("대파", "채소류", "2대", 8, "수동"),
        ("고춧가루", "기타", "1봉", 150, "수동"),
        ("된장", "기타", "1통", 90, "수동"),
    ]
    return [
        {
            "id": uuid.uuid4().hex[:8],
            "name": name,
            "category": cat,
            "amount": amount,
            "expiry": (t + timedelta(days=dd)).isoformat(),
            "source": src,
            "added_at": (t - timedelta(days=1)).isoformat(),
        }
        for name, cat, amount, dd, src in rows
    ]


def _seed_meals():
    """최근 7일 식단 시드 (오늘은 아침만 기록된 상태)."""
    t = _today()
    # day_offset: 6(일주일 전) ~ 0(오늘)
    menus = {
        6: {
            "아침": [("계란", 60), ("밀가루", 90), ("우유", 200)],
            "점심": [("밥", 300), ("김치", 100), ("계란", 50), ("참기름", 5)],
            "저녁": [("돼지고기", 200), ("김치", 100), ("밥", 200), ("상추", 30), ("마늘", 10)],
        },
        5: {
            "아침": [("바나나", 100), ("우유", 250)],
            "점심": [("밥", 250), ("김", 5), ("당근", 30), ("오이", 30), ("계란", 50), ("단무지", 10)],
            "저녁": [("두부", 100), ("애호박", 80), ("감자", 80), ("양파", 50), ("된장", 15), ("밥", 200)],
        },
        4: {
            "아침": [("미역", 30), ("소고기", 50), ("밥", 200)],
            "점심": [("밥", 300), ("감자", 80), ("당근", 50), ("양파", 50), ("소고기", 60), ("카레가루", 20)],
            "저녁": [("어묵", 100), ("대파", 20), ("밥", 200)],
        },
        3: {
            "아침": [("계란", 100), ("밥", 200), ("김", 3)],
            "점심": [("토마토", 150), ("계란", 100), ("밥", 150)],
            "저녁": [("돼지고기", 250), ("상추", 50), ("마늘", 15), ("밥", 100)],
        },
        2: {
            "아침": [("우유", 250), ("바나나", 100)],
            "점심": [("김치", 200), ("돼지고기", 100), ("두부", 100), ("양파", 50), ("밥", 200)],
            "저녁": [("감자", 200), ("밥", 150)],
        },
        1: {
            "아침": [("밥", 250), ("김", 5), ("당근", 30), ("오이", 30), ("계란", 50), ("단무지", 10)],
            "점심": [("밀가루", 200), ("고춧가루", 10), ("오이", 50)],
            "저녁": [("고등어", 150), ("밥", 150)],
        },
        0: {
            "아침": [("계란", 60), ("밥", 150), ("김치", 50)],
        },
    }
    out = []
    for off, day_meals in menus.items():
        d = (t - timedelta(days=off)).isoformat()
        for mtype, items in day_meals.items():
            out.append({
                "id": uuid.uuid4().hex[:8],
                "date": d,
                "type": mtype,
                "items": [{"name": n, "amount": a} for n, a in items],
                "memo": "",
            })
    return out


def _seed_profile():
    return {
        "name": "홍길동",
        "daily_kcal_target": 2100,
        "notes": "평소 국·찌개와 볶음밥을 자주 먹습니다.",
    }


class FridgeStore:
    def __init__(self):
        _ensure()

    def all(self):
        conn = _connect()
        try:
            rows = conn.execute("SELECT * FROM fridge ORDER BY expiry, name").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def add(self, name, category, amount, expiry, source="수동"):
        item = {
            "id": uuid.uuid4().hex[:8],
            "name": name.strip(),
            "category": category,
            "amount": amount.strip(),
            "expiry": expiry,
            "source": source,
            "added_at": _today().isoformat(),
        }
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO fridge VALUES (?,?,?,?,?,?,?)",
                (item["id"], item["name"], item["category"], item["amount"],
                 item["expiry"], item["source"], item["added_at"]),
            )
            conn.commit()
        finally:
            conn.close()
        return item

    def remove(self, item_id):
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM fridge WHERE id = ?", (item_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def find(self, item_id):
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM fridge WHERE id = ?", (item_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


class MealStore:
    def __init__(self):
        _ensure()

    @staticmethod
    def _row_to_meal(row):
        m = dict(row)
        m["items"] = json.loads(m.get("items") or "[]")
        return m

    def all(self):
        conn = _connect()
        try:
            rows = conn.execute("SELECT * FROM meals ORDER BY date, type").fetchall()
            return [self._row_to_meal(r) for r in rows]
        finally:
            conn.close()

    def add(self, date_str, mtype, items, memo=""):
        meal = {
            "id": uuid.uuid4().hex[:8],
            "date": date_str,
            "type": mtype,
            "items": [{"name": it["name"].strip(), "amount": float(it["amount"])} for it in items],
            "memo": memo,
        }
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO meals VALUES (?,?,?,?,?)",
                (meal["id"], meal["date"], meal["type"], meal["memo"],
                 json.dumps(meal["items"], ensure_ascii=False)),
            )
            conn.commit()
        finally:
            conn.close()
        return meal

    def remove(self, meal_id):
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM meals WHERE id = ?", (meal_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def recent_days(self, n=7):
        start = (_today() - timedelta(days=n - 1)).isoformat()
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM meals WHERE date >= ? ORDER BY date, type", (start,)
            ).fetchall()
            return [self._row_to_meal(r) for r in rows]
        finally:
            conn.close()


class ProfileStore:
    def __init__(self):
        _ensure()

    def get(self):
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()
            if row:
                return dict(row)
        finally:
            conn.close()
        return _seed_profile()

    def update(self, patch):
        cur = self.get()
        cur.update(patch)
        conn = _connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO profile VALUES (1, ?, ?, ?)",
                (cur.get("name", "사용자"), cur.get("daily_kcal_target", 2100),
                 cur.get("notes", "")),
            )
            conn.commit()
        finally:
            conn.close()
        return cur
