"""Слой работы с базой данных SQLite для системы учёта оборудования."""

import hashlib
import hmac
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("EQUIPMENT_DB", "equipment.db")

ROLE_SUPER = "superadmin"
ROLE_ADMIN = "admin"
ROLE_VIEWER = "viewer"
ROLE_USER = "user"

ROLES = (ROLE_SUPER, ROLE_ADMIN, ROLE_VIEWER, ROLE_USER)
ROLE_NAMES_RU = {
    ROLE_SUPER: "Суперадминистратор",
    ROLE_ADMIN: "Администратор базы",
    ROLE_VIEWER: "Наблюдатель",
    ROLE_USER: "Пользователь",
}

STATUSES = ("в эксплуатации", "на складе", "в ремонте", "списано")

# Права доступа по ролям
PERMISSIONS = {
    ROLE_SUPER: {"view", "manage_users", "manage_databases", "audit",
                 "add_equip", "edit_equip", "delete_equip", "assign_equip"},
    ROLE_ADMIN: {"view", "manage_users", "audit",
                 "add_equip", "edit_equip", "delete_equip", "assign_equip"},
    ROLE_VIEWER: {"view", "audit"},
    ROLE_USER: {"view", "add_equip", "assign_equip"},
}


def hash_password(password: str, salt: bytes | None = None):
    if salt is None:
        salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=2 ** 14, r=8, p=1, dklen=32)
    return salt, digest


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Database:
    def __init__(self, path: str | None = None):
        self.path = path or DB_PATH
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def close(self):
        self.conn.close()

    # ------------------------------------------------------------------ schema
    def _create_schema(self):
        cur = self.conn.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS databases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                login TEXT NOT NULL UNIQUE,
                full_name TEXT NOT NULL DEFAULT '',
                salt BLOB NOT NULL,
                pwd_hash BLOB NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('superadmin','admin','viewer','user')),
                db_id INTEGER REFERENCES databases(id) ON DELETE SET NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                db_id INTEGER NOT NULL REFERENCES databases(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                UNIQUE (db_id, name)
            );

            CREATE TABLE IF NOT EXISTS equipment (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                db_id INTEGER NOT NULL REFERENCES databases(id) ON DELETE CASCADE,
                inv_number TEXT NOT NULL,
                name TEXT NOT NULL,
                category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
                model TEXT NOT NULL DEFAULT '',
                serial_number TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'на складе',
                location TEXT NOT NULL DEFAULT '',
                responsible TEXT NOT NULL DEFAULT '',
                purchase_date TEXT NOT NULL DEFAULT '',
                price REAL NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (db_id, inv_number)
            );

            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                db_id INTEGER NOT NULL REFERENCES databases(id) ON DELETE CASCADE,
                equip_id INTEGER,
                username TEXT NOT NULL,
                action TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
        """)
        self.conn.commit()

    # ------------------------------------------------------------- bootstrap
    def init_first_run(self):
        """Создаёт базу «Основная» и суперадмина admin/admin, если пользователей нет."""
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) c FROM databases")
        if cur.fetchone()["c"] == 0:
            cur.execute("INSERT INTO databases(name, created_at) VALUES (?, ?)",
                        ("Основная", _now()))
        cur.execute("SELECT COUNT(*) c FROM users")
        if cur.fetchone()["c"] == 0:
            self.create_user("admin", "admin", ROLE_SUPER, full_name="Суперадминистратор")
            return True
        return False

    # ------------------------------------------------------------------- auth
    def authenticate(self, login: str, password: str):
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM users WHERE login = ? AND active = 1", (login,))
        row = cur.fetchone()
        if not row:
            return None
        _, digest = hash_password(password, row["salt"])
        if hmac.compare_digest(digest, bytes(row["pwd_hash"])):
            return dict(row)
        return None

    # --------------------------------------------------------------- databases
    def list_databases(self):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT d.id, d.name, d.created_at,
                   (SELECT COUNT(*) FROM equipment e WHERE e.db_id = d.id) AS equip_count
            FROM databases d ORDER BY d.name
        """)
        return [dict(r) for r in cur.fetchall()]

    def create_database(self, name: str):
        cur = self.conn.cursor()
        cur.execute("INSERT INTO databases(name, created_at) VALUES (?, ?)", (name, _now()))
        self.conn.commit()
        return cur.lastrowid

    def rename_database(self, db_id: int, name: str):
        self.conn.execute("UPDATE databases SET name = ? WHERE id = ?", (name, db_id))
        self.conn.commit()

    def delete_database(self, db_id: int):
        self.conn.execute("DELETE FROM databases WHERE id = ?", (db_id,))
        self.conn.commit()

    # ------------------------------------------------------------------- users
    def create_user(self, login: str, password: str, role: str,
                    db_id=None, full_name: str = ""):
        salt, digest = hash_password(password)
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO users(login, full_name, salt, pwd_hash, role, db_id, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)
        """, (login, full_name, salt, digest, role, db_id, _now()))
        self.conn.commit()
        return cur.lastrowid

    def list_users(self, db_id: int | None = None, include_all: bool = False):
        cur = self.conn.cursor()
        if include_all:
            cur.execute("""
                SELECT u.id, u.login, u.full_name, u.role, u.active, u.db_id,
                       d.name AS db_name
                FROM users u LEFT JOIN databases d ON d.id = u.db_id
                ORDER BY u.login
            """)
        else:
            cur.execute("""
                SELECT u.id, u.login, u.full_name, u.role, u.active, u.db_id,
                       d.name AS db_name
                FROM users u LEFT JOIN databases d ON d.id = u.db_id
                WHERE u.db_id = ? OR u.role = 'superadmin'
                ORDER BY u.login
            """, (db_id,))
        return [dict(r) for r in cur.fetchall()]

    def set_user_active(self, user_id: int, active: bool):
        self.conn.execute("UPDATE users SET active = ? WHERE id = ?", (int(active), user_id))
        self.conn.commit()

    def set_user_role(self, user_id: int, role: str, db_id=None):
        self.conn.execute("UPDATE users SET role = ?, db_id = ? WHERE id = ?",
                          (role, db_id, user_id))
        self.conn.commit()

    def set_user_password(self, user_id: int, password: str):
        salt, digest = hash_password(password)
        self.conn.execute("UPDATE users SET salt = ?, pwd_hash = ? WHERE id = ?",
                          (salt, digest, user_id))
        self.conn.commit()

    # -------------------------------------------------------------- categories
    def list_categories(self, db_id: int):
        cur = self.conn.cursor()
        cur.execute("SELECT id, name FROM categories WHERE db_id = ? ORDER BY name", (db_id,))
        return [dict(r) for r in cur.fetchall()]

    def create_category(self, db_id: int, name: str):
        cur = self.conn.cursor()
        cur.execute("INSERT INTO categories(db_id, name) VALUES (?, ?)", (db_id, name))
        self.conn.commit()
        return cur.lastrowid

    # --------------------------------------------------------------- equipment
    def list_equipment(self, db_id: int, search: str = "", status: str = ""):
        q = """
            SELECT e.*, c.name AS category_name
            FROM equipment e LEFT JOIN categories c ON c.id = e.category_id
            WHERE e.db_id = ?
        """
        params = [db_id]
        if search:
            like = f"%{search}%"
            q += (" AND (e.name LIKE ? OR e.inv_number LIKE ? OR e.serial_number LIKE ?"
                  " OR e.model LIKE ? OR e.location LIKE ? OR e.responsible LIKE ?)")
            params += [like] * 6
        if status:
            q += " AND e.status = ?"
            params.append(status)
        q += " ORDER BY e.inv_number"
        cur = self.conn.cursor()
        cur.execute(q, params)
        return [dict(r) for r in cur.fetchall()]

    def get_equipment(self, equip_id: int):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT e.*, c.name AS category_name
            FROM equipment e LEFT JOIN categories c ON c.id = e.category_id
            WHERE e.id = ?
        """, (equip_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def add_equipment(self, db_id: int, data: dict, username: str):
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO equipment(db_id, inv_number, name, category_id, model, serial_number,
                                  status, location, responsible, purchase_date, price, note,
                                  created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (db_id, data["inv_number"], data["name"], data.get("category_id"),
              data.get("model", ""), data.get("serial_number", ""),
              data.get("status", "на складе"), data.get("location", ""),
              data.get("responsible", ""), data.get("purchase_date", ""),
              float(data.get("price", 0) or 0), data.get("note", ""), _now(), _now()))
        eid = cur.lastrowid
        self.log_history(db_id, eid, username, "приход", data["name"])
        self.conn.commit()
        return eid

    def update_equipment(self, equip_id: int, data: dict, username: str):
        cur = self.conn.cursor()
        cur.execute("SELECT db_id, name FROM equipment WHERE id = ?", (equip_id,))
        row = cur.fetchone()
        if not row:
            return False
        cur.execute("""
            UPDATE equipment SET inv_number=?, name=?, category_id=?, model=?, serial_number=?,
                   status=?, location=?, responsible=?, purchase_date=?, price=?, note=?,
                   updated_at=?
            WHERE id=?
        """, (data["inv_number"], data["name"], data.get("category_id"),
              data.get("model", ""), data.get("serial_number", ""),
              data.get("status", "на складе"), data.get("location", ""),
              data.get("responsible", ""), data.get("purchase_date", ""),
              float(data.get("price", 0) or 0), data.get("note", ""),
              _now(), equip_id))
        self.log_history(row["db_id"], equip_id, username, "изменение", data["name"])
        self.conn.commit()
        return True

    def delete_equipment(self, equip_id: int, username: str):
        cur = self.conn.cursor()
        cur.execute("SELECT db_id, name FROM equipment WHERE id = ?", (equip_id,))
        row = cur.fetchone()
        if not row:
            return False
        cur.execute("DELETE FROM equipment WHERE id = ?", (equip_id,))
        self.log_history(row["db_id"], equip_id, username, "списание/удаление", row["name"])
        self.conn.commit()
        return True

    # ---------------------------------------------------------------- history
    def log_history(self, db_id: int, equip_id, username: str, action: str, details: str = ""):
        self.conn.execute("""
            INSERT INTO history(db_id, equip_id, username, action, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (db_id, equip_id, username, action, details, _now()))
        self.conn.commit()

    def list_history(self, db_id: int, limit: int = 200):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT h.*, e.inv_number, e.name AS equip_name
            FROM history h LEFT JOIN equipment e ON e.id = h.equip_id
            WHERE h.db_id = ? ORDER BY h.id DESC LIMIT ?
        """, (db_id, limit))
        return [dict(r) for r in cur.fetchall()]
