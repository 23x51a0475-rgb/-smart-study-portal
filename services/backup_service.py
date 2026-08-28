import os
import shutil
import sqlite3
import json
import hashlib
import tempfile
import sys
from datetime import datetime
from services.database_service import DatabaseService
from services.storage_service import STORAGE_DIR, MATERIALS_DIR

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def get_backups_dir():
    if os.environ.get('VERCEL') == '1':
        return os.path.join(tempfile.gettempdir(), 'backups')
    
    local_dir = os.path.join(BASE_DIR, 'backups')
    try:
        os.makedirs(local_dir, exist_ok=True)
        test_file = os.path.join(local_dir, '.write_test')
        with open(test_file, 'w') as f:
            f.write('test')
        if os.path.exists(test_file):
            os.remove(test_file)
        return local_dir
    except Exception:
        return os.path.join(tempfile.gettempdir(), 'backups')

BACKUPS_DIR = get_backups_dir()
BACKUP_DB_DIR = os.path.join(BACKUPS_DIR, 'database')
BACKUP_MAT_DIR = os.path.join(BACKUPS_DIR, 'materials')
BACKUP_MAN_DIR = os.path.join(BACKUPS_DIR, 'manifests')

class BackupService:
    @staticmethod
    def _ensure_dirs():
        os.makedirs(BACKUP_DB_DIR, exist_ok=True)
        os.makedirs(BACKUP_MAT_DIR, exist_ok=True)
        os.makedirs(BACKUP_MAN_DIR, exist_ok=True)

    @staticmethod
    def _hash_file(filepath) -> str:
        if not os.path.exists(filepath):
            return None
        hasher = hashlib.sha256()
        with open(filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    @staticmethod
    def create_backup() -> dict:
        """
        Safely backs up the database and files, and creates a verified manifest.
        """
        try:
            BackupService._ensure_dirs()
            timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
            backup_id = f"backup_{timestamp}"
            
            # 1. Database Backup
            source_db_path = DatabaseService.get_db_path()
            dest_db_name = f"materials_{timestamp}.db"
            dest_db_path = os.path.join(BACKUP_DB_DIR, dest_db_name)
            
            if os.path.exists(source_db_path):
                source_conn = sqlite3.connect(source_db_path)
                dest_conn = sqlite3.connect(dest_db_path)
                with dest_conn:
                    source_conn.backup(dest_conn)
                dest_conn.close()
                source_conn.close()
            else:
                # If using external DB or DB file doesn't exist yet, export table json
                conn = DatabaseService.get_connection()
                rows = conn.execute("SELECT * FROM materials").fetchall()
                conn.close()
                dest_conn = sqlite3.connect(dest_db_path)
                dest_conn.execute("""
                    CREATE TABLE materials (
                        id INTEGER PRIMARY KEY, title TEXT, subject TEXT, description TEXT,
                        original_filename TEXT, stored_filename TEXT, relative_path TEXT,
                        file_size INTEGER, mime_type TEXT, sha256 TEXT, status TEXT,
                        version INTEGER, uploaded_by TEXT, uploaded_at TIMESTAMP, updated_at TIMESTAMP
                    )
                """)
                for r in rows:
                    dest_conn.execute("""
                        INSERT INTO materials VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (r.get('id'), r.get('title'), r.get('subject'), r.get('description'),
                          r.get('original_filename'), r.get('stored_filename'), r.get('relative_path'),
                          r.get('file_size'), r.get('mime_type'), r.get('sha256'), r.get('status'),
                          r.get('version'), r.get('uploaded_by'), str(r.get('uploaded_at')), str(r.get('updated_at'))))
                dest_conn.commit()
                dest_conn.close()
                
            # 2. File Backup
            def copy_dir_tree(src, dst):
                if not os.path.exists(dst):
                    os.makedirs(dst, exist_ok=True)
                for item in os.listdir(src):
                    s = os.path.join(src, item)
                    d = os.path.join(dst, item)
                    if os.path.isdir(s):
                        copy_dir_tree(s, d)
                    else:
                        shutil.copy2(s, d)

            if os.path.exists(MATERIALS_DIR):
                copy_dir_tree(MATERIALS_DIR, BACKUP_MAT_DIR)
                
            # 3. Create Manifest
            conn = sqlite3.connect(dest_db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM materials WHERE status != 'deleted'").fetchall()
            conn.close()
            
            manifest_files = []
            total_size = 0
            
            for row in rows:
                rel_path = row['relative_path']
                sub_path = rel_path[len('materials/'):] if rel_path.startswith('materials/') else rel_path
                backup_file_path = os.path.join(BACKUP_MAT_DIR, os.path.normpath(sub_path))
                
                file_hash = BackupService._hash_file(backup_file_path)
                file_sz = os.path.getsize(backup_file_path) if os.path.exists(backup_file_path) else 0
                
                manifest_files.append({
                    "material_id": row['id'],
                    "relative_path": rel_path,
                    "size": file_sz,
                    "sha256": file_hash
                })
                total_size += file_sz
                
            manifest_data = {
                "backup_id": backup_id,
                "database": dest_db_name,
                "created_at": datetime.now().isoformat(),
                "total_materials": len(rows),
                "total_files": len([f for f in manifest_files if f['sha256'] is not None]),
                "total_size_bytes": total_size,
                "files": manifest_files
            }
            
            manifest_path = os.path.join(BACKUP_MAN_DIR, f"{backup_id}.json")
            with open(manifest_path, 'w') as f:
                json.dump(manifest_data, f, indent=4)
                
            if not BackupService.verify_backup(backup_id):
                return {"success": False, "message": "Backup verification failed.", "backup_id": backup_id}
                
            return {"success": True, "message": "Backup completed successfully.", "backup_id": backup_id}
        except Exception as e:
            print(f"[BackupService.create_backup] Error: {e}", file=sys.stderr)
            return {"success": False, "message": f"Backup failed: {str(e)}"}

    @staticmethod
    def verify_backup(backup_id: str) -> bool:
        """Verifies the backup against its manifest."""
        manifest_path = os.path.join(BACKUP_MAN_DIR, f"{backup_id}.json")
        if not os.path.exists(manifest_path):
            return False
            
        with open(manifest_path, 'r') as f:
            manifest = json.load(f)
            
        db_path = os.path.join(BACKUP_DB_DIR, manifest['database'])
        if not os.path.exists(db_path):
            return False
            
        for file_meta in manifest['files']:
            rel_path = file_meta['relative_path']
            sub_path = rel_path[len('materials/'):] if rel_path.startswith('materials/') else rel_path
            backup_file_path = os.path.join(BACKUP_MAT_DIR, os.path.normpath(sub_path))
            
            if not os.path.exists(backup_file_path):
                return False
                
            if os.path.getsize(backup_file_path) != file_meta['size']:
                return False
                
            actual_hash = BackupService._hash_file(backup_file_path)
            if actual_hash != file_meta['sha256']:
                return False
                
        return True
