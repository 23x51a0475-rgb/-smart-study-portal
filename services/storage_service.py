import os
import shutil
import hashlib
import uuid
import tempfile
import sys
import io
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def get_base_storage_dir():
    if os.environ.get('VERCEL') == '1':
        return os.path.join(tempfile.gettempdir(), 'storage')
    
    local_dir = os.path.join(BASE_DIR, 'storage')
    try:
        os.makedirs(local_dir, exist_ok=True)
        test_file = os.path.join(local_dir, '.write_test')
        with open(test_file, 'w') as f:
            f.write('test')
        if os.path.exists(test_file):
            os.remove(test_file)
        return local_dir
    except Exception:
        return os.path.join(tempfile.gettempdir(), 'storage')

STORAGE_DIR = get_base_storage_dir()
MATERIALS_DIR = os.path.join(STORAGE_DIR, 'materials')
TMP_DIR = os.path.join(STORAGE_DIR, 'tmp')
TRASH_DIR = os.path.join(STORAGE_DIR, 'trash')

def get_s3_client():
    bucket = os.environ.get('S3_BUCKET_NAME')
    if not bucket:
        return None, None
    try:
        import boto3
        kwargs = {}
        if os.environ.get('AWS_ACCESS_KEY_ID') and os.environ.get('AWS_SECRET_ACCESS_KEY'):
            kwargs['aws_access_key_id'] = os.environ.get('AWS_ACCESS_KEY_ID')
            kwargs['aws_secret_access_key'] = os.environ.get('AWS_SECRET_ACCESS_KEY')
        if os.environ.get('AWS_REGION'):
            kwargs['region_name'] = os.environ.get('AWS_REGION')
        if os.environ.get('S3_ENDPOINT_URL'):
            kwargs['endpoint_url'] = os.environ.get('S3_ENDPOINT_URL')
            
        client = boto3.client('s3', **kwargs)
        return client, bucket
    except Exception as e:
        print(f"[StorageService] S3 client init error: {e}", file=sys.stderr)
        return None, None

class StorageService:
    @staticmethod
    def _ensure_dirs():
        os.makedirs(MATERIALS_DIR, exist_ok=True)
        os.makedirs(TMP_DIR, exist_ok=True)
        os.makedirs(TRASH_DIR, exist_ok=True)

    @staticmethod
    def process_upload(file_obj, original_filename: str = "") -> dict:
        StorageService._ensure_dirs()
        ext = os.path.splitext(original_filename)[1].lower() if original_filename and '.' in original_filename else '.tmp'
        temp_filename = f"{uuid.uuid4().hex}{ext}"
        temp_path = os.path.join(TMP_DIR, temp_filename)
        
        hasher = hashlib.sha256()
        file_size = 0
        
        try:
            with open(temp_path, 'wb') as f:
                chunk = file_obj.read(8192)
                if not chunk:
                    raise ValueError("Empty file uploaded")
                
                while chunk:
                    f.write(chunk)
                    hasher.update(chunk)
                    file_size += len(chunk)
                    chunk = file_obj.read(8192)
        except Exception as e:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise e

        return {
            'temp_path': temp_path,
            'sha256': hasher.hexdigest(),
            'file_size': file_size
        }

    @staticmethod
    def generate_final_path(sha256: str, original_filename: str) -> tuple:
        now = datetime.now()
        year = now.strftime('%Y')
        month = now.strftime('%m')
        hash_prefix = sha256[:2]
        
        ext = os.path.splitext(original_filename)[1].lower() if original_filename and '.' in original_filename else '.pdf'
        stored_filename = f"{sha256[:8]}-{uuid.uuid4().hex[:8]}{ext}"
        
        rel_dir = os.path.join(year, month, hash_prefix)
        rel_path = os.path.join(rel_dir, stored_filename).replace('\\', '/')
        
        abs_dir = os.path.join(MATERIALS_DIR, rel_dir)
        abs_path = os.path.join(abs_dir, stored_filename)
        
        return abs_path, f"materials/{rel_path}", stored_filename

    @staticmethod
    def move_to_final(temp_path: str, abs_final_path: str):
        os.makedirs(os.path.dirname(abs_final_path), exist_ok=True)
        shutil.move(temp_path, abs_final_path)
        
        client, bucket = get_s3_client()
        if client and bucket:
            try:
                rel_path = StorageService.get_relative_key(abs_final_path)
                client.upload_file(abs_final_path, bucket, rel_path)
            except Exception as e:
                print(f"[StorageService] Failed to upload to S3: {e}", file=sys.stderr)

    @staticmethod
    def get_relative_key(abs_path: str) -> str:
        if abs_path.startswith(MATERIALS_DIR):
            sub = abs_path[len(MATERIALS_DIR):].lstrip('/\\')
            return f"materials/{sub}".replace('\\', '/')
        return os.path.basename(abs_path)

    @staticmethod
    def move_to_trash(relative_path: str):
        sub_path = relative_path[len('materials/'):] if relative_path.startswith('materials/') else relative_path
        source_abs_path = os.path.join(MATERIALS_DIR, os.path.normpath(sub_path))
        
        if os.path.exists(source_abs_path):
            StorageService._ensure_dirs()
            trash_dest_path = os.path.join(TRASH_DIR, os.path.normpath(sub_path))
            os.makedirs(os.path.dirname(trash_dest_path), exist_ok=True)
            try:
                shutil.move(source_abs_path, trash_dest_path)
            except Exception:
                pass

        client, bucket = get_s3_client()
        if client and bucket:
            try:
                client.delete_object(Bucket=bucket, Key=relative_path)
            except Exception as e:
                print(f"[StorageService] S3 delete error: {e}", file=sys.stderr)

    @staticmethod
    def get_absolute_path(relative_path: str) -> str:
        sub_path = relative_path[len('materials/'):] if relative_path.startswith('materials/') else relative_path
        return os.path.join(MATERIALS_DIR, os.path.normpath(sub_path))

    @staticmethod
    def get_file_for_serving(relative_path: str):
        """
        Returns (path_or_file_obj, is_stream)
        If local file exists, returns (abs_path, False).
        If not locally present but in S3, downloads to BytesIO and returns (bytes_io, True).
        If not found anywhere, returns (None, False).
        """
        abs_path = StorageService.get_absolute_path(relative_path)
        if os.path.exists(abs_path):
            return abs_path, False

        client, bucket = get_s3_client()
        if client and bucket:
            try:
                buf = io.BytesIO()
                client.download_fileobj(bucket, relative_path, buf)
                buf.seek(0)
                return buf, True
            except Exception as e:
                print(f"[StorageService] S3 download error: {e}", file=sys.stderr)

        return None, False
