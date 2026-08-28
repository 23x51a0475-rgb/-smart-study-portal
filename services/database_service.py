import sqlite3
import os
import tempfile
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DB_PATH = os.path.join(BASE_DIR, 'data', 'materials.db')

class DictRow(dict):
    """Row object supporting both dictionary key access and integer positional indexing."""
    def __init__(self, values, keys):
        super().__init__(zip(keys, values))
        self._values = list(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)

def get_sqlite_db_path():
    """Returns a safe, writable SQLite database path."""
    db_env_path = os.environ.get('SQLITE_DB_PATH')
    if db_env_path:
        os.makedirs(os.path.dirname(db_env_path), exist_ok=True)
        return db_env_path

    # On Vercel runtime or if local directory is read-only, use /tmp
    if os.environ.get('VERCEL') == '1':
        tmp_dir = os.path.join(tempfile.gettempdir(), 'data')
        os.makedirs(tmp_dir, exist_ok=True)
        return os.path.join(tmp_dir, 'materials.db')

    try:
        os.makedirs(os.path.dirname(LOCAL_DB_PATH), exist_ok=True)
        # Test write permission
        test_file = os.path.join(os.path.dirname(LOCAL_DB_PATH), '.write_test')
        with open(test_file, 'w') as f:
            f.write('test')
        if os.path.exists(test_file):
            os.remove(test_file)
        return LOCAL_DB_PATH
    except Exception:
        tmp_dir = os.path.join(tempfile.gettempdir(), 'data')
        os.makedirs(tmp_dir, exist_ok=True)
        return os.path.join(tmp_dir, 'materials.db')

class UnifiedDBConnection:
    def __init__(self, conn, db_type='sqlite'):
        self.conn = conn
        self.db_type = db_type

    def execute(self, sql, params=()):
        if self.db_type == 'postgres':
            pg_sql = sql.replace('?', '%s')
            cursor = self.conn.cursor()
            cursor.execute(pg_sql, params)
            return UnifiedDBCursor(cursor, self.db_type)
        else:
            cursor = self.conn.execute(sql, params)
            return UnifiedDBCursor(cursor, self.db_type)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        try:
            self.conn.rollback()
        except Exception:
            pass

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

class UnifiedDBCursor:
    def __init__(self, cursor, db_type):
        self.cursor = cursor
        self.db_type = db_type

    def fetchone(self):
        if self.db_type == 'postgres':
            if not self.cursor.description:
                return None
            row = self.cursor.fetchone()
            if row is None:
                return None
            keys = [col[0] for col in self.cursor.description]
            return DictRow(row, keys)
        else:
            row = self.cursor.fetchone()
            if row is None:
                return None
            if isinstance(row, sqlite3.Row):
                keys = row.keys()
                return DictRow([row[k] for k in keys], list(keys))
            keys = [col[0] for col in self.cursor.description] if self.cursor.description else []
            return DictRow(row, keys)

    def fetchall(self):
        if self.db_type == 'postgres':
            if not self.cursor.description:
                return []
            rows = self.cursor.fetchall()
            keys = [col[0] for col in self.cursor.description]
            return [DictRow(r, keys) for r in rows]
        else:
            rows = self.cursor.fetchall()
            if not rows:
                return []
            if isinstance(rows[0], sqlite3.Row):
                keys = list(rows[0].keys())
                return [DictRow([r[k] for k in keys], keys) for r in rows]
            keys = [col[0] for col in self.cursor.description] if self.cursor.description else []
            return [DictRow(r, keys) for r in rows]

class DatabaseService:
    @staticmethod
    def get_connection():
        db_url = os.environ.get('DATABASE_URL') or os.environ.get('POSTGRES_URL')
        if db_url:
            if db_url.startswith('postgres://'):
                db_url = db_url.replace('postgres://', 'postgresql://', 1)
            try:
                import psycopg2
                conn = psycopg2.connect(db_url)
                return UnifiedDBConnection(conn, db_type='postgres')
            except Exception as e:
                print(f"[DatabaseService] PostgreSQL connection failed ({e}), falling back to SQLite.", file=sys.stderr)
        
        db_path = get_sqlite_db_path()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
        except Exception:
            pass
        try:
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
        except Exception:
            pass
            
        return UnifiedDBConnection(conn, db_type='sqlite')

    @staticmethod
    def init_db():
        try:
            conn = DatabaseService.get_connection()
            if conn.db_type == 'postgres':
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS materials (
                        id SERIAL PRIMARY KEY,
                        title TEXT NOT NULL,
                        subject TEXT NOT NULL,
                        description TEXT,
                        original_filename TEXT NOT NULL,
                        stored_filename TEXT NOT NULL UNIQUE,
                        relative_path TEXT NOT NULL UNIQUE,
                        file_size BIGINT NOT NULL,
                        mime_type TEXT NOT NULL DEFAULT 'application/pdf',
                        sha256 TEXT NOT NULL UNIQUE,
                        status TEXT NOT NULL DEFAULT 'active',
                        version INTEGER NOT NULL DEFAULT 1,
                        uploaded_by TEXT,
                        uploaded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                """)
            else:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS materials (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title TEXT NOT NULL,
                        subject TEXT NOT NULL,
                        description TEXT,
                        original_filename TEXT NOT NULL,
                        stored_filename TEXT NOT NULL UNIQUE,
                        relative_path TEXT NOT NULL UNIQUE,
                        file_size INTEGER NOT NULL,
                        mime_type TEXT NOT NULL DEFAULT 'application/pdf',
                        sha256 TEXT NOT NULL UNIQUE,
                        status TEXT NOT NULL DEFAULT 'active',
                        version INTEGER NOT NULL DEFAULT 1,
                        uploaded_by TEXT,
                        uploaded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                """)

            conn.execute("CREATE INDEX IF NOT EXISTS idx_materials_subject ON materials(subject);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_materials_status ON materials(status);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_materials_uploaded_at ON materials(uploaded_at);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_materials_title ON materials(title);")
            
            if conn.db_type == 'sqlite':
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_materials_sha256 ON materials(sha256);")

            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[DatabaseService.init_db] Warning: {e}", file=sys.stderr)

    @staticmethod
    def get_db_path():
        return get_sqlite_db_path()
