from datetime import datetime
import os
import re
import sqlite3
import threading
import time
from flask import Flask, jsonify, make_response, render_template, request
import requests

# --- НАСТРОЙКИ ---
SCRIPT_URL = os.environ.get("SCRIPT_URL", "")
DEVICE_KEY = os.environ.get("DEVICE_KEY", "default_key")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "budget.db")
FAMILY_MEMBERS = ["Катя", "Артем", "Маша", "Алиса"]

app = Flask(__name__)


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def clean_num(val):
    if isinstance(val, (int, float)):
        return float(val)
    cleaned = re.sub(r"[^\d.,-]", "", str(val)).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def init_and_load_from_google():
    # При запуске всегда создаем чистую БД и считываем актуальные данные из Google Таблицы
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = get_db()
    c = conn.cursor()
    c.execute("""CREATE TABLE bd (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date_str TEXT, category TEXT, name TEXT, amount REAL, note TEXT
    )""")
    c.execute("""CREATE TABLE categories (
        name TEXT PRIMARY KEY, description TEXT, cat_type TEXT
    )""")
    c.execute("""CREATE TABLE total_exp (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date_str TEXT, amount REAL
    )""")

    try:
        data = requests.get(SCRIPT_URL, timeout=20).json()
        for r in data.get("cats", [])[1:]:
            if r and str(r[0]).strip():
                desc = str(r[1]).strip() if len(r) > 1 else ""
                ctype = str(r[2]).strip() if len(r) > 2 else "Персональная"
                c.execute(
                    "INSERT OR REPLACE INTO categories VALUES (?, ?, ?)",
                    (str(r[0]).strip(), desc, ctype),
                )
        for r in data.get("bd", [])[1:]:
            if r and str(r[0]).strip():
                c.execute(
                    "INSERT INTO bd (date_str, category, name, amount, note) VALUES (?, ?, ?, ?, ?)",
                    (
                        str(r[0]).strip(),
                        str(r[1]).strip(),
                        str(r[2]).strip(),
                        clean_num(r[3]),
                        str(r[4]).strip() if len(r) > 4 else "",
                    ),
                )
        for r in data.get("total", [])[1:]:
            if r and str(r[0]).strip():
                c.execute(
                    "INSERT INTO total_exp (date_str, amount) VALUES (?, ?)",
                    (str(r[0]).strip(), clean_num(r[1])),
                )
    except Exception as e:
        print("Ошибка первичной загрузки из Google:", e)
    conn.commit()
    conn.close()
def sync_db_to_google():
    try:
        conn = get_db()
        bd_rows = [
            ["Дата", "Категория", "Имя", "Сумма", "Примечание"]
        ] + [
            [
                r["date_str"],
                r["category"],
                r["name"],
                r["amount"],
                r["note"],
            ]
            for r in conn.execute("SELECT * FROM bd ORDER BY id").fetchall()
        ]
        tot_rows = [["Дата", "Сумма"]] + [
            [r["date_str"], r["amount"]]
            for r in conn.execute(
                "SELECT * FROM total_exp ORDER BY id"
            ).fetchall()
        ]
        conn.close()
        requests.post(
            SCRIPT_URL,
            json={"action": "sync_all", "sheet": "БД", "rows": bd_rows},
            timeout=20,
        )
        requests.post(
            SCRIPT_URL,
            json={
                "action": "sync_all",
                "sheet": "Общий расход",
                "rows": tot_rows,
            },
            timeout=20,
        )
    except Exception as e:
        print("Ошибка синхронизации в Google:", e)


def auto_sync_worker():
    while True:
        time.sleep(6 * 3600)  # Автосинхронизация каждые 6 часов
        sync_db_to_google()


# Инициализация БД при старте и запуск фонового таймера на 6 часов
init_and_load_from_google()
threading.Thread(target=auto_sync_worker, daemon=True).start()


def is_authorized(req):
    if req.args.get("key") == DEVICE_KEY:
        return True
    if req.cookies.get("device_token") == DEVICE_KEY:
        return True
    if req.headers.get("X-Device-Key") == DEVICE_KEY:
        return True
    return False


@app.route("/")
def index():
    if not is_authorized(request):
        return (
            "<h3>Доступ запрещен. Откройте приложение по персональной ссылке привязки.</h3>",
            403,
        )
    resp = make_response(
        render_template("index.html", device_key=DEVICE_KEY)
    )
    # Привязываем браузер устройства на 10 лет через HttpOnly Cookie
    resp.set_cookie(
        "device_token", DEVICE_KEY, max_age=10 * 365 * 24 * 3600, samesite="Lax"
    )
    return resp


@app.route("/api/init")
def api_init():
    if not is_authorized(request):
        return jsonify({"error": "403"}), 403
    conn = get_db()
    cats = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM categories ORDER BY LOWER(name)"
        ).fetchall()
    ]
    bd = [
        dict(r)
        for r in conn.execute("SELECT * FROM bd ORDER BY id DESC").fetchall()
    ]
    tot = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM total_exp ORDER BY id DESC"
        ).fetchall()
    ]
    conn.close()
    return jsonify({
        "categories": cats,
        "bd": bd,
        "total": tot,
        "members": FAMILY_MEMBERS,
    })
@app.route("/api/add", methods=["POST"])
def api_add():
    if not is_authorized(request):
        return jsonify({"error": "403"}), 403
    data = request.json
    rows = data.get("rows", [])
    conn = get_db()
    for r in rows:
        conn.execute(
            "INSERT INTO bd (date_str, category, name, amount, note) VALUES (?, ?, ?, ?, ?)",
            (r[0], r[1], r[2], float(r[3]), r[4]),
        )
    conn.commit()
    conn.close()
    # В фоне сразу дописываем свежие строки в Google для надежности
    threading.Thread(
        target=lambda: requests.post(
            SCRIPT_URL,
            json={"action": "append", "sheet": "БД", "rows": rows},
            timeout=15,
        ),
        daemon=True,
    ).start()
    return jsonify({"status": "ok"})


@app.route("/api/update", methods=["POST"])
def api_update():
    if not is_authorized(request):
        return jsonify({"error": "403"}), 403
    d = request.json
    conn = get_db()
    conn.execute(
        "UPDATE bd SET date_str=?, category=?, name=?, amount=?, note=? WHERE id=?",
        (
            d["date_str"],
            d["category"],
            d["name"],
            float(d["amount"]),
            d["note"],
            int(d["id"]),
        ),
    )
    conn.commit()
    conn.close()
    threading.Thread(target=sync_db_to_google, daemon=True).start()
    return jsonify({"status": "ok"})


@app.route("/api/delete", methods=["POST"])
def api_delete():
    if not is_authorized(request):
        return jsonify({"error": "403"}), 403
    row_id = int(request.json["id"])
    conn = get_db()
    conn.execute("DELETE FROM bd WHERE id=?", (row_id,))
    conn.commit()
    conn.close()
    threading.Thread(target=sync_db_to_google, daemon=True).start()
    return jsonify({"status": "ok"})


@app.route("/api/total", methods=["POST"])
def api_total():
    if not is_authorized(request):
        return jsonify({"error": "403"}), 403
    d = request.json
    date_str, amount = d["date_str"], float(d["amount"])
    mm_yyyy = date_str[3:]
    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM total_exp WHERE substr(date_str, 4) = ?", (mm_yyyy,)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE total_exp SET date_str=?, amount=? WHERE id=?",
            (date_str, amount, existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO total_exp (date_str, amount) VALUES (?, ?)",
            (date_str, amount),
        )
    conn.commit()
    conn.close()
    threading.Thread(target=sync_db_to_google, daemon=True).start()
    return jsonify({"status": "ok"})


@app.route("/api/sync_now", methods=["POST"])
def api_sync_now():
    if not is_authorized(request):
        return jsonify({"error": "403"}), 403
    sync_db_to_google()
    return jsonify({"status": "ok"})
