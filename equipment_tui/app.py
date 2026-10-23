#!/usr/bin/env python3
"""
ТУИ-приложение «Учёт оборудования компании».

Хранение — SQLite (файл equipment.db по умолчанию; путь можно задать
аргументом командной строки или переменной окружения EQUIPMENT_DB).

Роли:
  * суперадминистратор  — всё: пользователи, базы данных, оборудование, журнал;
  * администратор базы  — пользователи своей базы + полное управление оборудованием;
  * наблюдатель         — только просмотр оборудования, категорий и журнала;
  * пользователь        — просмотр + добавление/редактирование своего ввода.

Запуск:   python3 app.py [путь_к_бд]
Первый запуск создаёт базу «Основная» и учётную запись admin/admin.
"""

import curses
import locale
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import (Database, ROLES, ROLE_NAMES_RU, ROLE_SUPER, STATUSES, PERMISSIONS)


# ============================== низкоуровневые хелперы ======================

def safe_addstr(win, y, x, text, attr=curses.A_NORMAL):
    """addstr без падения на правой/нижней границе окна."""
    h, w = win.getmaxyx()
    if not (0 <= y < h and 0 <= x < w):
        return
    text = text[: max(0, w - x - 1)]
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


class Ctx:
    def __init__(self, db: Database):
        self.db = db
        self.user = None            # dict залогиненного пользователя
        self.current_db_id = None   # выбранная база оборудования
        self.current_db_name = ""

    def can(self, perm: str) -> bool:
        if not self.user:
            return False
        return perm in PERMISSIONS.get(self.user["role"], set())

    def my_db_ids(self):
        """Базы, к которым у текущего пользователя есть доступ."""
        if self.user["role"] == ROLE_SUPER:
            return [d["id"] for d in self.db.list_databases()]
        if self.user["db_id"]:
            return [self.user["db_id"]]
        return []


def prompt_line(scr, title, default="", secret=False, maxlen=200):
    """Однострочный ввод в рамке внизу экрана. Esc -> None."""
    curses.curs_set(0 if secret else 1)
    h, w = scr.getmaxyx()
    box_w = min(w, len(title) + maxlen + 6)
    win = curses.newwin(3, box_w, max(0, h - 4), 0)
    win.keypad(True)
    win.box()
    safe_addstr(win, 0, 3, " Ввод ", curses.A_BOLD)
    safe_addstr(win, 1, 2, title)
    text = list(default)
    start_x = 2 + len(title) + 1
    safe_addstr(win, 1, start_x, "".join(text)[: max(1, box_w - start_x - 2)])
    win.move(1, min(start_x + len(text), box_w - 2))
    win.refresh()
    while True:
        ch = win.getch()
        if ch == 27:                                   # Esc
            curses.curs_set(0)
            del win
            return None
        if ch in (curses.KEY_ENTER, 10, 13):
            curses.curs_set(0)
            del win
            return "".join(text)
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            if text:
                text.pop()
        elif 32 <= ch < 127 or ch > 127:               # печатные символы
            if len(text) < maxlen:
                text.append(chr(ch))
        shown = "•" * len(text) if secret else "".join(text)
        safe_addstr(win, 1, start_x, (shown + " " * 60)[: max(1, box_w - start_x - 2)])
        win.move(1, min(start_x + len(text), box_w - 2))
        win.refresh()


def message(scr, text, is_error=False):
    """Модальное окно (закрыть любой клавишей)."""
    scr.erase()
    scr.refresh()
    h, w = scr.getmaxyx()
    msg = f" {text} "
    box_w = min(max(10, w - 2), len(msg) + 4)
    win = curses.newwin(3, box_w, max(0, h // 2 - 1), max(0, (w - box_w) // 2))
    win.box()
    safe_addstr(win, 1, 2, msg, curses.A_BOLD if is_error else curses.A_REVERSE)
    win.refresh()
    win.getch()
    del win


def confirm(scr, text) -> bool:
    scr.erase()
    scr.refresh()
    h, w = scr.getmaxyx()
    msg = f"{text}? [Enter=да / любой другой ключ=нет]"
    box_w = min(max(10, w - 2), len(msg) + 4)
    win = curses.newwin(3, box_w, max(0, h // 2 - 1), max(0, (w - box_w) // 2))
    win.box()
    safe_addstr(win, 1, 2, msg[: max(1, box_w - 4)], curses.A_BOLD)
    win.refresh()
    ch = win.getch()
    del win
    return ch in (10, 13, ord('y'), ord('Y'), ord('t'))  # t = русская «е»


def choice(scr, title, options):
    """Вертикальное меню. Возвращает индекс или None (Esc)."""
    sel, offset = 0, 0
    while True:
        scr.erase()
        h, w = scr.getmaxyx()
        safe_addstr(scr, 0, 1, f" {title} "[:w - 2], curses.A_BOLD | curses.A_REVERSE)
        body_h = h - 4
        if body_h < 1 or not options:
            return None
        sel = max(0, min(sel, len(options) - 1))
        if sel < offset:
            offset = sel
        elif sel >= offset + body_h:
            offset = sel - body_h + 1
        for i in range(min(len(options), body_h)):
            idx = offset + i
            prefix = "> " if idx == sel else "  "
            safe_addstr(scr, 2 + i, 2, f"{prefix}{options[idx]}"[:w - 3],
                        curses.A_REVERSE if idx == sel else curses.A_NORMAL)
        safe_addstr(scr, h - 1, 1, "↑/↓ выбор · Enter OK · Esc назад", curses.A_DIM)
        scr.refresh()
        ch = scr.getch()
        if ch == 27:
            return None
        if ch in (curses.KEY_UP, ord('k')):
            sel -= 1
        elif ch in (curses.KEY_DOWN, ord('j')):
            sel += 1
        elif ch == curses.KEY_NPAGE:
            sel += body_h
        elif ch == curses.KEY_PPAGE:
            sel -= body_h
        elif ch in (curses.KEY_ENTER, 10, 13):
            return sel


def table_screen(scr, title, headers, rows, extra_keys=()):
    """Прокручиваемая таблица.

    rows — список строк (каждая — список str). Возвращает пару:
      (None, None)  — Esc (назад);
      (idx, None)   — Enter по строке idx;
      (None, tag)   — нажата одна из `extra_keys` ((keys_tuple, tag)).
    """
    sel, offset, col_off = 0, 0, 0
    widths = [min(max([len(headers[i])] + [len(str(r[i])) for r in rows]), 36)
              for i in range(len(headers))] if headers else []

    while True:
        scr.erase()
        h, w = scr.getmaxyx()
        safe_addstr(scr, 0, 1, f" {title} "[:w - 2], curses.A_BOLD | curses.A_REVERSE)
        body_h = h - 3
        hdr = " │ ".join(f"{headers[i]:<{widths[i]}}" for i in range(len(headers)))
        safe_addstr(scr, 1, 1, hdr[col_off:col_off + w - 2],
                    curses.A_BOLD | curses.A_UNDERLINE)
        total = len(rows)
        if total == 0:
            safe_addstr(scr, 2, 2, "(нет данных)", curses.A_DIM)
        else:
            sel = max(0, min(sel, total - 1))
            if sel < offset:
                offset = sel
            elif sel >= offset + body_h:
                offset = sel - body_h + 1
            for ri in range(body_h):
                idx = offset + ri
                if idx >= total:
                    break
                cells = " │ ".join(f"{str(rows[idx][i]):<{widths[i]}}"
                                   for i in range(len(headers)))
                safe_addstr(scr, 2 + ri, 1, cells[col_off:col_off + w - 2],
                            curses.A_REVERSE if idx == sel else curses.A_NORMAL)
        keys_hint = " · ".join(tag for _, tag in extra_keys)
        safe_addstr(scr, h - 1, 1,
                    ("↑/↓ навигация · ←/→ прокрутка · Enter открыть · Esc назад"
                     + (f" · {keys_hint}" if keys_hint else "") +
                     (f" · всего {total}" if total else ""))[:w - 2], curses.A_DIM)
        scr.refresh()
        ch = scr.getch()
        if ch == 27:
            return None, None
        for keys_tuple, tag in extra_keys:
            if ch in keys_tuple:
                return None, tag
        if ch in (curses.KEY_UP, ord('k')):
            sel -= 1
        elif ch in (curses.KEY_DOWN, ord('j')):
            sel += 1
        elif ch == curses.KEY_NPAGE:
            sel += body_h
        elif ch == curses.KEY_PPAGE:
            sel -= body_h
        elif ch in (curses.KEY_LEFT, ord('h')):
            col_off = max(0, col_off - 5)
        elif ch in (curses.KEY_RIGHT, ord('l')):
            col_off += 5
        elif ch in (curses.KEY_ENTER, 10, 13):
            if total:
                return sel, None


# ================================ форма оборудования =========================

EQ_TEXT_FIELDS = [
    ("inv_number", "Инвентарный номер*"),
    ("name", "Наименование*"),
    ("model", "Модель"),
    ("serial_number", "Серийный номер"),
    ("location", "Расположение / кабинет"),
    ("responsible", "Ответственный"),
    ("purchase_date", "Дата покупки (ГГГГ-ММ-ДД)"),
    ("price", "Цена"),
    ("note", "Примечание"),
]


def equip_form(scr, ctx: Ctx, existing=None):
    """Пошаговая форма. Возвращает dict данных либо None (отмена)."""
    data = {"inv_number": "", "name": "", "model": "", "serial_number": "",
            "status": STATUSES[0], "location": "", "responsible": "",
            "purchase_date": "", "price": "0", "note": "", "category_id": None}
    cats = ctx.db.list_categories(ctx.current_db_id)
    cat_names = ["(без категории)"] + [c["name"] for c in cats]
    cat_sel = 0
    if existing:
        for k in list(data.keys()):
            if k == "category_id":
                continue
            v = existing.get(k)
            if v is not None:
                data[k] = str(v)
        cid = existing.get("category_id")
        for i, c in enumerate(cats):
            if c["id"] == cid:
                cat_sel = i + 1

    mode = "Изменение оборудования" if existing else "Новое оборудование"
    total_steps = len(EQ_TEXT_FIELDS) + 2   # + статус + категория
    step = 0
    while step < total_steps:
        if step == len(EQ_TEXT_FIELDS):
            idx = choice(scr, f"{mode}: шаг {step + 1}/{total_steps}. Статус", list(STATUSES))
            if idx is None:
                step = max(0, step - 1)
                continue
            data["status"] = STATUSES[idx]
            step += 1
            continue
        if step == len(EQ_TEXT_FIELDS) + 1:
            idx = choice(scr, f"{mode}: шаг {step + 1}/{total_steps}. Категория", cat_names)
            if idx is None:
                step -= 1
                continue
            cat_sel = idx
            step += 1
            continue
        key, label = EQ_TEXT_FIELDS[step]
        val = prompt_line(scr, f"[{step + 1}/{total_steps}] {label}:", data[key])
        if val is None:
            if step == 0:
                return None
            step -= 1
            continue
        data[key] = val
        step += 1

    if not data["inv_number"].strip() or not data["name"].strip():
        message(scr, "Инвентарный номер и наименование обязательны!", True)
        return None
    try:
        data["price"] = float(data["price"].replace(",", ".") or 0)
    except ValueError:
        message(scr, "Цена должна быть числом!", True)
        return None
    data["category_id"] = None if cat_sel == 0 else cats[cat_sel - 1]["id"]
    if not confirm(scr, f"Сохранить «{data['name']}» ({data['inv_number']})"):
        return None
    return data


def equip_card(scr, ctx: Ctx, item: dict):
    """Карточка объекта + последние операции по нему."""
    scr.erase()
    h, w = scr.getmaxyx()
    safe_addstr(scr, 0, 1,
                f" Карточка: {item['inv_number']} — {item['name']} "[:w - 2],
                curses.A_BOLD | curses.A_REVERSE)
    pairs = [
        ("Категория", item.get("category_name") or "—"),
        ("Модель", item.get("model") or "—"),
        ("Серийный №", item.get("serial_number") or "—"),
        ("Статус", item.get("status") or "—"),
        ("Расположение", item.get("location") or "—"),
        ("Ответственный", item.get("responsible") or "—"),
        ("Дата покупки", item.get("purchase_date") or "—"),
        ("Цена", f"{float(item.get('price', 0) or 0):,.2f}".replace(",", " ")),
        ("Примечание", item.get("note") or "—"),
        ("Создано / изменено", f"{item.get('created_at', '')} / {item.get('updated_at', '')}"),
    ]
    ri = 2
    for k, v in pairs:
        safe_addstr(scr, ri, 2, f"{k:<19}: {v}"[:w - 4])
        ri += 1
    hist = [x for x in ctx.db.list_history(ctx.current_db_id, 500)
            if x["equip_id"] == item["id"]][:12]
    ri += 1
    if ri < h - 2:
        safe_addstr(scr, ri, 2, "История по объекту:", curses.A_BOLD)
        for hh in hist:
            ri += 1
            if ri >= h - 2:
                break
            safe_addstr(scr, ri, 4,
                        f"{hh['created_at']}  {hh['username']}: {hh['action']} {hh['details']}"[:w - 6])
    safe_addstr(scr, h - 1, 1, "Любая клавиша — назад", curses.A_DIM)
    scr.refresh()
    scr.getch()


def equip_actions_menu(scr, ctx: Ctx, item: dict):
    """Действия после просмотра карточки (с учётом прав)."""
    opts, acts = [], []
    if ctx.can("edit_equip"):
        opts.append("Изменить данные"); acts.append("edit")
    if ctx.can("delete_equip"):
        opts.append("Удалить / списать"); acts.append("del")
    if not opts:
        return
    opts.append("Ничего"); acts.append(None)
    pick = choice(scr, f"Действие: {item['inv_number']} {item['name']}", opts)
    if pick is None:
        return
    act = acts[pick]
    if act == "edit":
        data = equip_form(scr, ctx, item)
        if data:
            ctx.db.update_equipment(item["id"], data, ctx.user["login"])
            message(scr, "Изменения сохранены.")
    elif act == "del":
        if confirm(scr, f"Удалить {item['inv_number']} — {item['name']}"):
            ctx.db.delete_equipment(item["id"], ctx.user["login"])
            message(scr, "Объект удалён (запись в журнале сохранена).")


# =============================== экран: оборудование ========================

def screen_equipment(scr, ctx: Ctx):
    search, status_filter = "", ""
    headers = ["Инв.№", "Наименование", "Категория", "Статус", "Где", "Ответств."]
    while True:
        items = ctx.db.list_equipment(ctx.current_db_id, search, status_filter)
        rows = [[e["inv_number"], e["name"], e.get("category_name") or "",
                 e["status"], e["location"], e["responsible"]] for e in items]
        title = f"Оборудование · {ctx.current_db_name}"
        if search:
            title += f" · поиск: «{search}»"
        if status_filter:
            title += f" · статус: {status_filter}"
        extra = []
        if ctx.can("add_equip"):
            extra.append(((ord('a'), ord('A'), ord('ф')), "A — добавить"))
        extra.append(((ord('/'), ord(',')), "/ — поиск"))
        extra.append(((ord('s'), ord('S'), ord('ы')), "S — фильтр статуса"))
        idx, tag = table_screen(scr, title, headers, rows, extra)
        if tag == "A — добавить":
            data = equip_form(scr, ctx)
            if data:
                try:
                    ctx.db.add_equipment(ctx.current_db_id, data, ctx.user["login"])
                    message(scr, f"Добавлено: {data['name']} ({data['inv_number']})")
                except Exception as e:
                    message(scr, f"Ошибка (возможно, инв.№ уже есть): {e}", True)
            continue
        if tag == "/ — поиск":
            q = prompt_line(scr, "Поиск (номер/название/серийник/место/ответств.):", search)
            if q is not None:
                search = q.strip()
            continue
        if tag == "S — фильтр статуса":
            pick = choice(scr, "Фильтр по статусу", ["(все статусы)"] + list(STATUSES))
            if pick is not None:
                status_filter = "" if pick == 0 else STATUSES[pick]
            continue
        if idx is None:
            return
        item = items[idx]
        equip_card(scr, ctx, item)
        equip_actions_menu(scr, ctx, item)


# ================================ экран: пользователи =======================

def create_user_flow(scr, ctx: Ctx):
    login = prompt_line(scr, "Логин нового пользователя:")
    if not login or not login.strip():
        return
    pwd = prompt_line(scr, "Пароль:", secret=True)
    if not pwd:
        return
    rep = prompt_line(scr, "Повторите пароль:", secret=True)
    if pwd != rep:
        message(scr, "Пароли не совпадают!", True)
        return
    fio = prompt_line(scr, "ФИО (необязательно):") or ""
    allowed_roles = list(ROLES)
    if ctx.user["role"] == ROLE_ADMIN:          # админ базы не создаёт суперадминов
        allowed_roles = [r for r in ROLES if r != ROLE_SUPER]
    ridx = choice(scr, "Роль", [ROLE_NAMES_RU[r] for r in allowed_roles])
    if ridx is None:
        return
    role = allowed_roles[ridx]
    db_id = ctx.current_db_id
    if ctx.user["role"] == ROLE_SUPER and role != ROLE_SUPER:
        dbs = ctx.db.list_databases()
        didx = choice(scr, "База для пользователя", [d["name"] for d in dbs])
        if didx is None:
            return
        db_id = dbs[didx]["id"]
    try:
        ctx.db.create_user(login.strip(), pwd, role, db_id, fio.strip())
        message(scr, f"Создан пользователь «{login.strip()}» ({ROLE_NAMES_RU[role]}).")
    except Exception as e:
        message(scr, f"Ошибка создания: {e}", True)


def screen_users(scr, ctx: Ctx):
    include_all = ctx.user["role"] == ROLE_SUPER
    while True:
        users = ctx.db.list_users(ctx.current_db_id, include_all)
        rows = [[u["login"], u["full_name"] or "—", ROLE_NAMES_RU[u["role"]],
                 u["db_name"] or "—", "активен" if u["active"] else "отключён"]
                for u in users]
        extra = [((ord('n'), ord('N'), ord('т')), "N — создать")]
        idx, tag = table_screen(scr, "Пользователи",
                                ["Логин", "ФИО", "Роль", "База", "Состояние"], rows, extra)
        if tag == "N — создать":
            create_user_flow(scr, ctx)
            continue
        if idx is None:
            return
        u = users[idx]
        # ограничения: админ базы не трогает суперадмина и других админов
        privileged_target = u["role"] in (ROLE_SUPER,) or \
            (ctx.user["role"] == ROLE_ADMIN and u["role"] == ROLE_ADMIN and u["id"] != ctx.user["id"])
        opts, acts = [], []
        if not privileged_target:
            opts.append("Сбросить пароль"); acts.append("pwd")
            opts.append("Сменить роль / базу"); acts.append("role")
            opts.append("Вкл./выкл. доступ"); acts.append("toggle")
        else:
            message(scr, "Нет прав управлять этим пользователем.", True)
            continue
        opts.append("Ничего"); acts.append(None)
        pick = choice(scr, f"Пользователь: {u['login']} ({ROLE_NAMES_RU[u['role']]})", opts)
        if pick is None:
            continue
        act = acts[pick]
        if act == "pwd":
            np = prompt_line(scr, "Новый пароль:", secret=True)
            if np:
                np2 = prompt_line(scr, "Повторите пароль:", secret=True)
                if np == np2:
                    ctx.db.set_user_password(u["id"], np)
                    message(scr, "Пароль обновлён.")
                else:
                    message(scr, "Пароли не совпадают!", True)
        elif act == "role":
            ridx = choice(scr, "Новая роль", [ROLE_NAMES_RU[r] for r in ROLES])
            if ridx is None:
                continue
            dbs = ctx.db.list_databases()
            names = [d["name"] for d in dbs] + ["(без базы)"]
            didx = choice(scr, "База для пользователя", names)
            if didx is None:
                continue
            db_id = None if didx == len(dbs) else dbs[didx]["id"]
            ctx.db.set_user_role(u["id"], ROLES[ridx], db_id)
            message(scr, f"Установлена роль: {ROLE_NAMES_RU[ROLES[ridx]]}")
        elif act == "toggle":
            ctx.db.set_user_active(u["id"], not u["active"])
            message(scr, "Доступ включён." if not u["active"] else "Доступ отключён.")


# ================================= экран: базы данных ========================

def screen_databases(scr, ctx: Ctx):
    while True:
        dbs = ctx.db.list_databases()
        rows = [[str(d["id"]), d["name"], str(d["equip_count"]), d["created_at"]] for d in dbs]
        extra = [((ord('b'), ord('B'), ord('и')), "B — создать базу")]
        idx, tag = table_screen(scr, "Базы данных оборудования (суперадмин)",
                                ["ID", "Название", "Ед. оборуд.", "Создана (UTC)"],
                                rows, extra)
        if tag == "B — создать базу":
            nm = prompt_line(scr, "Название новой базы:")
            if nm and nm.strip():
                try:
                    new_id = ctx.db.create_database(nm.strip())
                    ctx.current_db_id, ctx.current_db_name = new_id, nm.strip()
                    message(scr, f"Создана и выбрана база «{nm.strip()}».")
                except Exception as e:
                    message(scr, f"Ошибка: {e}", True)
            continue
        if idx is None:
            return
        d = dbs[idx]
        opts = ["Сделать рабочей", "Переименовать", "Удалить", "Ничего"]
        pick = choice(scr, f"База: {d['name']}", opts)
        if pick is None or pick == 3:
            continue
        if pick == 0:
            ctx.current_db_id, ctx.current_db_name = d["id"], d["name"]
            message(scr, f"Рабочая база: {d['name']}")
        elif pick == 1:
            nm = prompt_line(scr, "Новое название:", d["name"])
            if nm and nm.strip():
                try:
                    ctx.db.rename_database(d["id"], nm.strip())
                    if ctx.current_db_id == d["id"]:
                        ctx.current_db_name = nm.strip()
                except Exception as e:
                    message(scr, f"Ошибка: {e}", True)
        elif pick == 2:
            if confirm(scr, f"Удалить базу «{d['name']}» со ВСЕМ оборудованием и журналом"):
                ctx.db.delete_database(d["id"])
                if ctx.current_db_id == d["id"]:
                    rest = ctx.db.list_databases()
                    if rest:
                        ctx.current_db_id, ctx.current_db_name = rest[0]["id"], rest[0]["name"]
                    else:
                        ctx.current_db_id, ctx.current_db_name = None, "—"


def switch_db_flow(scr, ctx: Ctx):
    dbs = [d for d in ctx.db.list_databases() if d["id"] in ctx.my_db_ids()]
    if len(dbs) <= 1:
        message(scr, "У вас доступна только одна база.")
        return
    idx = choice(scr, "Выбор рабочей базы",
                 [f"{d['name']} ({d['equip_count']} ед.)" for d in dbs])
    if idx is not None:
        ctx.current_db_id, ctx.current_db_name = dbs[idx]["id"], dbs[idx]["name"]


# ================================== экран: журнал ============================

def screen_history(scr, ctx: Ctx):
    while True:
        hist = ctx.db.list_history(ctx.current_db_id, 500)
        rows = [[x["created_at"], x["username"], x["action"],
                 ((x["inv_number"] or "?") + " " + (x["equip_name"] or "")).strip(),
                 x["details"]] for x in hist]
        idx, _ = table_screen(scr, f"Журнал операций · {ctx.current_db_name}",
                              ["Дата (UTC)", "Кто", "Действие", "Объект", "Подробно"], rows)
        if idx is None:
            return


# ================================= categories ===============================

def screen_categories(scr, ctx: Ctx):
    editable = ctx.can("edit_equip")
    while True:
        cats = ctx.db.list_categories(ctx.current_db_id)
        rows = [[str(c["id"]), c["name"]] for c in cats]
        extra = []
        if editable:
            extra.append(((ord('n'), ord('N'), ord('т')), "N — новая категория"))
        idx, tag = table_screen(scr, f"Категории · {ctx.current_db_name}",
                                ["ID", "Название"], rows, extra)
        if tag == "N — новая категория":
            nm = prompt_line(scr, "Название категории:")
            if nm and nm.strip():
                try:
                    ctx.db.create_category(ctx.current_db_id, nm.strip())
                except Exception as e:
                    message(scr, f"Ошибка: {e}", True)
            continue
        if idx is None:
            return


# ================================== главное меню ============================

def change_own_password(scr, ctx: Ctx):
    oldp = prompt_line(scr, "Текущий пароль:", secret=True)
    if oldp is None:
        return
    if ctx.db.authenticate(ctx.user["login"], oldp) is None:
        message(scr, "Неверный текущий пароль!", True)
        return
    np = prompt_line(scr, "Новый пароль:", secret=True)
    if not np:
        return
    np2 = prompt_line(scr, "Повторите новый пароль:", secret=True)
    if np == np2:
        ctx.db.set_user_password(ctx.user["id"], np)
        message(scr, "Пароль изменён.")
    else:
        message(scr, "Пароли не совпадают!", True)


def main_menu(scr, ctx: Ctx):
    """Возвращает True, если нужно выйти из приложения целиком."""
    actions = []  # (keys:set, label, handler)

    def add(keys, label, handler, need_perm=None, cond=None):
        if need_perm and not ctx.can(need_perm):
            return
        if cond is not None and not cond():
            return
        actions.append((set(keys), label, handler))

    add(set("1"), "1. Оборудование — список, поиск, карточки", screen_equipment, "view")
    add(set("2"), "2. Добавить оборудование", _quick_add, "add_equip")
    add(set("3"), "3. Категории оборудования", screen_categories, "view")
    add(set("4"), "4. Пользователи", screen_users, "manage_users")
    add(set("5"), "5. Создать пользователя", create_user_flow, "manage_users")
    add(set("6"), "6. Базы данных (создание/удаление)", screen_databases, "manage_databases")
    add(set("7"), "7. Сменить рабочую базу", switch_db_flow,
        cond=lambda: len(ctx.my_db_ids()) > 1)
    add(set("8"), "8. Журнал операций", screen_history, "audit")
    add(set("9"), "9. Сменить свой пароль", change_own_password)
    add({ord('q'), ord('Q'), ord('й')}, "Q. Выход", None)

    sel = 0
    while True:
        scr.erase()
        h, w = scr.getmaxyx()
        role_ru = ROLE_NAMES_RU[ctx.user["role"]]
        head = (f" УЧЁТ ОБОРУДОВАНИЯ · {ctx.user['login']} ({role_ru}) · "
                f"база: {ctx.current_db_name or '—'} ")
        safe_addstr(scr, 0, 1, head[:w - 2], curses.A_BOLD | curses.A_REVERSE)
        for i, (_, label, _) in enumerate(actions):
            mark = ">" if i == sel else " "
            attr = curses.A_REVERSE if i == sel else curses.A_NORMAL
            safe_addstr(scr, 2 + i, 2, f"{mark} {label}"[:w - 3], attr)
        footer = f"Файл БД: {ctx.db.path}"
        safe_addstr(scr, h - 2, 1, footer[:w - 2], curses.A_DIM)
        safe_addstr(scr, h - 1, 1, "Цифры или ↑/↓+Enter — выбор · Q — выход", curses.A_DIM)
        scr.refresh()
        sel = max(0, min(sel, len(actions) - 1))
        ch = scr.getch()
        target = None
        if ch in (curses.KEY_UP, ord('k')):
            sel = (sel - 1) % len(actions)
            continue
        if ch in (curses.KEY_DOWN, ord('j')):
            sel = (sel + 1) % len(actions)
            continue
        for i, (keys, label, _) in enumerate(actions):
            if ch in keys:
                target = i
                break
        if ch in (curses.KEY_ENTER, 10, 13):
            target = sel
        if target is None:
            continue
        sel = target
        keys, label, handler = actions[target]
        if handler is None:                      # выход
            return confirm(scr, "Выйти из приложения")
        if not ctx.current_db_id and handler not in (screen_databases, change_own_password):
            if not pick_start_db(scr, ctx):
                return True                      # нет баз — на перезаход
        handler(scr, ctx)


def _quick_add(scr, ctx):
    data = equip_form(scr, ctx)
    if data:
        try:
            ctx.db.add_equipment(ctx.current_db_id, data, ctx.user["login"])
            message(scr, f"Добавлено: {data['name']} ({data['inv_number']})")
        except Exception as e:
            message(scr, f"Ошибка: {e}", True)


# ==================================== вход ===================================

def login_screen(scr, db: Database):
    error = ""
    while True:
        scr.erase()
        h, w = scr.getmaxyx()
        bw, bh = min(60, max(20, w - 2)), 8
        bx, by = max(0, (w - bw) // 2), max(0, (h - bh) // 2 - 2)
        win = curses.newwin(bh, bw, by, bx)
        win.box()
        safe_addstr(win, 0, 4, " АВТОРИЗАЦИЯ ", curses.A_BOLD)
        safe_addstr(win, 2, 4, "Учёт оборудования компании (SQLite)")
        safe_addstr(win, 3, 4, "Роли: суперадмин · админ базы · наблюдатель · пользователь")
        safe_addstr(win, 4, 4, "Esc в поле ввода — выход из программы")
        if error:
            safe_addstr(win, 6, 4, error, curses.A_BOLD)
        win.refresh()
        del win
        login = prompt_line(scr, "Логин:")
        if login is None:
            return None
        if not login.strip():
            return None
        pwd = prompt_line(scr, "Пароль:", secret=True)
        if pwd is None:
            return None
        user = db.authenticate(login.strip(), pwd)
        if user:
            return user
        error = "Неверный логин или пароль!"


def pick_start_db(scr, ctx: Ctx) -> bool:
    ids = ctx.my_db_ids()
    if not ids:
        message(scr, "Нет доступной базы — обратитесь к администратору.", True)
        return False
    dbs = [d for d in ctx.db.list_databases() if d["id"] in ids]
    if not dbs:
        return False
    if len(dbs) == 1:
        ctx.current_db_id, ctx.current_db_name = dbs[0]["id"], dbs[0]["name"]
        return True
    idx = choice(scr, "Выберите базу", [f"{d['name']} ({d['equip_count']} ед.)" for d in dbs])
    if idx is None:
        return False
    ctx.current_db_id, ctx.current_db_name = dbs[idx]["id"], dbs[idx]["name"]
    return True


def main(scr, db: Database):
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    curses.curs_set(0)
    scr.keypad(True)
    if db.init_first_run():
        message(scr, "Первый запуск: создан суперадмин admin / admin. Смените пароль!")
    while True:
        user = login_screen(scr, db)
        if not user:
            return
        ctx = Ctx(db)
        ctx.user = user
        if not pick_start_db(scr, ctx):
            continue
        quit_app = main_menu(scr, ctx)
        if quit_app:
            return


def run(db_path=None):
    db = Database(db_path)
    try:
        curses.wrapper(main, db)
    finally:
        db.close()


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None)
