import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
import bcrypt
import jwt


class AuthManager:
    def __init__(
        self,
        db_path: str = "database/oficina.db",
        schema_path: str = "database/schema.sql",
        jwt_secret: str | None = None,
        token_hours: int = 8,
        max_attempts: int = 3,
        lock_minutes: int = 5,
    ):
        self.db_path = db_path
        self.schema_path = schema_path
        self.jwt_secret = jwt_secret or os.getenv("JWT_SECRET", "cambia-esta-clave-en-produccion")
        self.token_hours = token_hours
        self.max_attempts = max_attempts
        self.lock_minutes = lock_minutes
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with open(self.schema_path, "r", encoding="utf-8") as f:
            schema_sql = f.read()
        with self._get_conn() as conn:
            conn.executescript(schema_sql)

    def _log_audit(
        self,
        conn: sqlite3.Connection,
        user_id: int | None,
        action: str,
        status: str,
        details: str | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO audit_logs (user_id, agent_name, action, status, details)
            VALUES (?, 'AuthManager', ?, ?, ?)
            """,
            (user_id, action, status, details),
        )

    def create_user(self, username: str, password: str, role: str = "admin") -> int:
        password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        with self._get_conn() as conn:
            cursor = conn.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                (username.strip(), password_hash, role),
            )
            user_id = cursor.lastrowid
            self._log_audit(conn, user_id, "CREATE_USER", "SUCCESS", f"Usuario '{username}' creado")
            return int(user_id)

    def login(self, username: str, password: str) -> dict:
        now = datetime.now(timezone.utc)
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ? AND is_active = 1",
                (username.strip(),),
            ).fetchone()

            if not row:
                self._log_audit(conn, None, "LOGIN", "DENIED", f"Usuario inexistente: {username}")
                return {"authenticated": False, "error": "Credenciales inválidas."}

            user_id = row["id"]

            if row["locked_until"]:
                locked_dt = datetime.fromisoformat(row["locked_until"])
                if now < locked_dt:
                    self._log_audit(conn, user_id, "LOGIN", "LOCKED", "Cuenta bloqueada temporalmente")
                    return {
                        "authenticated": False,
                        "error": f"Cuenta bloqueada hasta {locked_dt.isoformat()}.",
                    }
                conn.execute(
                    "UPDATE users SET failed_attempts = 0, locked_until = NULL WHERE id = ?",
                    (user_id,),
                )

            valid_pw = bcrypt.checkpw(password.encode("utf-8"), row["password_hash"].encode("utf-8"))
            if not valid_pw:
                attempts = row["failed_attempts"] + 1
                lock_str = None
                if attempts >= self.max_attempts:
                    lock_str = (now + timedelta(minutes=self.lock_minutes)).isoformat()
                    attempts = 0
                conn.execute(
                    "UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?",
                    (attempts, lock_str, user_id),
                )
                self._log_audit(
                    conn,
                    user_id,
                    "LOGIN",
                    "FAILED",
                    f"Contraseña incorrecta. Bloqueo: {lock_str or 'no'}",
                )
                return {"authenticated": False, "error": "Credenciales inválidas."}

            conn.execute(
                "UPDATE users SET failed_attempts = 0, locked_until = NULL WHERE id = ?",
                (user_id,),
            )

            jti = str(uuid.uuid4())
            expires_at = now + timedelta(hours=self.token_hours)
            payload = {
                "sub": str(user_id),
                "username": row["username"],
                "role": row["role"],
                "jti": jti,
                "exp": int(expires_at.timestamp()),
            }
            token = jwt.encode(payload, self.jwt_secret, algorithm="HS256")

            conn.execute(
                "INSERT INTO sessions (user_id, token_jti, expires_at) VALUES (?, ?, ?)",
                (user_id, jti, expires_at.isoformat()),
            )
            self._log_audit(conn, user_id, "LOGIN", "SUCCESS", "Sesión iniciada correctamente")

            return {
                "authenticated": True,
                "token": token,
                "expires_at": expires_at.isoformat(),
                "user": {"id": user_id, "username": row["username"], "role": row["role"]},
            }

    def verify_token(self, token: str) -> dict | None:
        try:
            payload = jwt.decode(token, self.jwt_secret, algorithms=["HS256"])
        except jwt.PyJWTError:
            return None

        jti = payload.get("jti")
        with self._get_conn() as conn:
            session = conn.execute(
                "SELECT revoked FROM sessions WHERE token_jti = ?",
                (jti,),
            ).fetchone()
            if not session or session["revoked"] == 1:
                return None

        return payload
