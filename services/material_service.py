import os
import mimetypes
from services.database_service import DatabaseService
from services.storage_service import StorageService, MATERIALS_DIR

class MaterialService:
    @staticmethod
    def upload_material(file_obj, title: str, subject: str, description: str, uploaded_by: str) -> dict:
        """
        Failure-safe upload sequence:
        1. Validate & process into temp file
        2. Check for duplicate SHA-256
        3. Generate final paths
        4. Move file
        5. Insert DB record
        """
        original_name = getattr(file_obj, 'filename', 'material.pdf')
        if not original_name or original_name.strip() in ['', 'unknown', 'file']:
            original_name = f"{title.strip()}.pdf"
            
        # 1. Process into Temp File (calculates SHA256, Size)
        try:
            temp_data = StorageService.process_upload(file_obj, original_name)
        except ValueError as e:
            return {"success": False, "message": str(e)}
            
        temp_path = temp_data['temp_path']
        sha256 = temp_data['sha256']
        file_size = temp_data['file_size']
        mime_type = mimetypes.guess_type(original_name)[0] or 'application/octet-stream'
        
        conn = DatabaseService.get_connection()
        try:
            # 2. Check for duplicate
            existing = conn.execute("SELECT id FROM materials WHERE sha256 = ? AND status != 'deleted'", (sha256,)).fetchone()
            if existing:
                os.remove(temp_path)
                return {"success": False, "message": "This file already exists."}
                
            # 3. Generate Paths
            abs_final_path, relative_path, stored_filename = StorageService.generate_final_path(sha256, original_name)
            
            # 4. Move file
            StorageService.move_to_final(temp_path, abs_final_path)
            
            # 5. Insert DB record
            conn.execute(
                """INSERT INTO materials 
                   (title, subject, description, original_filename, stored_filename, relative_path, file_size, mime_type, sha256, uploaded_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (title, subject, description, original_name, stored_filename, relative_path, file_size, mime_type, sha256, uploaded_by)
            )
            conn.commit()
            return {"success": True, "message": "Material uploaded successfully."}
            
        except Exception as e:
            conn.rollback()
            # Cleanup temp if it's still there
            if os.path.exists(temp_path):
                os.remove(temp_path)
            # Cleanup final if move succeeded but DB failed
            if 'abs_final_path' in locals() and os.path.exists(abs_final_path):
                os.remove(abs_final_path)
            return {"success": False, "message": f"Upload failed: {e}"}
        finally:
            conn.close()

    @staticmethod
    def delete_material(material_id: int) -> dict:
        """Soft deletes a material by moving it to trash and updating status."""
        conn = DatabaseService.get_connection()
        try:
            row = conn.execute("SELECT * FROM materials WHERE id = ? AND status != 'deleted'", (material_id,)).fetchone()
            if not row:
                return {"success": False, "message": "Material not found."}
                
            # Move file to trash (safely ignore if file missing on disk)
            try:
                StorageService.move_to_trash(row['relative_path'])
            except Exception as file_err:
                print(f"[delete_material] Warning moving file to trash: {file_err}")
            
            # Update DB
            conn.execute("UPDATE materials SET status = 'deleted', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (material_id,))
            conn.commit()
            
            return {"success": True, "message": "Material deleted successfully."}
        except Exception as e:
            conn.rollback()
            return {"success": False, "message": f"Deletion failed: {e}"}
        finally:
            conn.close()

    @staticmethod
    def get_materials(page: int = 1, limit: int = 20, search: str = None, subject: str = None) -> dict:
        """Fetch materials with pagination and filtering."""
        offset = (page - 1) * limit
        conn = DatabaseService.get_connection()
        
        query = "SELECT * FROM materials WHERE status != 'deleted'"
        count_query = "SELECT COUNT(*) FROM materials WHERE status != 'deleted'"
        params = []
        
        if search:
            query += " AND (title LIKE ? OR description LIKE ?)"
            count_query += " AND (title LIKE ? OR description LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])
            
        if subject:
            query += " AND subject = ?"
            count_query += " AND subject = ?"
            params.append(subject)
            
        query += " ORDER BY uploaded_at DESC LIMIT ? OFFSET ?"
        
        total = conn.execute(count_query, params).fetchone()[0]
        
        params.extend([limit, offset])
        rows = conn.execute(query, params).fetchall()
        conn.close()
        
        materials = [dict(row) for row in rows]
        
        import math
        total_pages = math.ceil(total / limit) if total > 0 else 1
        
        return {
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": total_pages,
            "materials": materials
        }

    @staticmethod
    def get_material_by_id(material_id: int):
        conn = DatabaseService.get_connection()
        row = conn.execute("SELECT * FROM materials WHERE id = ? AND status != 'deleted'", (material_id,)).fetchone()
        conn.close()
        return dict(row) if row else None
        
    @staticmethod
    def get_material_by_filename(filename: str):
        conn = DatabaseService.get_connection()
        # Ensure we only fetch active files
        row = conn.execute("SELECT * FROM materials WHERE stored_filename = ? AND status != 'deleted'", (filename,)).fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def check_integrity() -> dict:
        """
        Checks all active DB records against physical files.
        Detects missing, corrupted, and orphan files.
        """
        conn = DatabaseService.get_connection()
        rows = conn.execute("SELECT * FROM materials WHERE status != 'deleted'").fetchall()
        conn.close()
        
        total = len(rows)
        healthy = 0
        missing = 0
        corrupted = 0
        
        db_valid_paths = set()
        
        import hashlib
        
        for row in rows:
            abs_path = StorageService.get_absolute_path(row['relative_path'])
            db_valid_paths.add(abs_path)
            
            if not os.path.exists(abs_path):
                missing += 1
                continue
                
            if os.path.getsize(abs_path) != row['file_size']:
                corrupted += 1
                continue
                
            # Check Hash
            hasher = hashlib.sha256()
            with open(abs_path, 'rb') as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    hasher.update(chunk)
            
            if hasher.hexdigest() != row['sha256']:
                corrupted += 1
                continue
                
            healthy += 1

        # Detect orphan files
        orphans = 0
        if os.path.exists(MATERIALS_DIR):
            for root, _, files in os.walk(MATERIALS_DIR):
                for f in files:
                    file_path = os.path.join(root, f)
                    if file_path not in db_valid_paths:
                        orphans += 1
                        
        return {
            "total": total,
            "healthy": healthy,
            "missing": missing,
            "corrupted": corrupted,
            "orphans": orphans
        }
