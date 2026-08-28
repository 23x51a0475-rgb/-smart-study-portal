from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, send_file
)
import os
import sys
import math
import mimetypes

# Load .env file if present in local development
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from services.database_service import DatabaseService
from services.material_service import MaterialService
from services.storage_service import StorageService
from services.backup_service import BackupService

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, 'templates'),
    static_folder=os.path.join(BASE_DIR, 'static')
)

app.secret_key = os.environ.get('SECRET_KEY', 'local-development-secret')

# Safely initialize database schema without crashing import on Vercel
try:
    DatabaseService.init_db()
except Exception as init_err:
    print(f"[app.py] Database init warning: {init_err}", file=sys.stderr)

FACULTY_PASSWORD = os.environ.get('FACULTY_PASSWORD', 'SREC@2007')

def get_clean_download_filename(mat: dict) -> str:
    """
    Constructs a user-facing download filename preserving original filename and extension.
    Falls back to material title if original_filename is missing or generic.
    """
    original = (mat.get('original_filename') or '').strip()
    title = (mat.get('title') or '').strip()
    stored = (mat.get('stored_filename') or '').strip()

    ext = ""
    if original and '.' in original:
        ext = os.path.splitext(original)[1]
    elif stored and '.' in stored:
        ext = os.path.splitext(stored)[1]

    if not ext:
        ext = ".pdf"

    ext_lower = ext.lower()

    if original and original.lower() not in ['unknown', 'unknown.pdf', 'unknown file', 'file', 'null', 'none']:
        filename = original
    else:
        filename = title if title else "material"

    if not filename.lower().endswith(ext_lower):
        filename = f"{filename}{ext}"

    return filename

# ─── Routes ───────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/student_login', methods=['GET', 'POST'])
def student_login():
    if request.method == 'POST':
        name       = request.form.get('name', '').strip()
        reg_no     = request.form.get('reg_no', '').strip()
        department = request.form.get('department', '').strip()
        year       = request.form.get('year', '').strip()

        if not all([name, reg_no, department, year]):
            flash('Please fill in all fields.', 'error')
            return redirect(url_for('student_login'))

        session['role']       = 'student'
        session['name']       = name
        session['reg_no']     = reg_no
        session['department'] = department
        session['year']       = year
        return redirect(url_for('student_dashboard'))
    return render_template('student_login.html')

@app.route('/faculty_login', methods=['GET', 'POST'])
def faculty_login():
    if request.method == 'POST':
        name       = request.form.get('name', '').strip()
        department = request.form.get('department', '').strip()
        password   = request.form.get('password', '').strip()

        if not all([name, department, password]):
            flash('Please fill in all fields.', 'error')
            return redirect(url_for('faculty_login'))

        if password != FACULTY_PASSWORD:
            flash('Invalid credentials.', 'error')
            return redirect(url_for('faculty_login'))

        session['role']       = 'faculty'
        session['name']       = name
        session['department'] = department
        return redirect(url_for('faculty_dashboard'))
    return render_template('faculty_login.html')

@app.route('/student_dashboard')
def student_dashboard():
    if session.get('role') != 'student':
        flash('Please login as a student first.', 'error')
        return redirect(url_for('student_login'))

    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '').strip()
    subject = request.args.get('subject', '').strip()
    
    result = MaterialService.get_materials(page=page, limit=20, search=search if search else None, subject=subject if subject else None)
    return render_template('student_dashboard.html', materials=result['materials'], pagination=result)

@app.route('/faculty_dashboard')
def faculty_dashboard():
    if session.get('role') != 'faculty':
        flash('Please login as faculty first.', 'error')
        return redirect(url_for('faculty_login'))

    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '').strip()
    subject = request.args.get('subject', '').strip()

    result = MaterialService.get_materials(page=page, limit=20, search=search if search else None, subject=subject if subject else None)
    return render_template('faculty_dashboard.html', materials=result['materials'], pagination=result)

# ─── Material Upload ───────────────────

@app.route('/upload', methods=['POST'])
@app.route('/api/materials/upload', methods=['POST'])
def upload():
    if session.get('role') != 'faculty':
        flash('Unauthorized access.', 'error')
        return redirect(url_for('faculty_login'))

    title       = request.form.get('title', '').strip()
    subject     = request.form.get('subject', '').strip()
    description = request.form.get('description', '').strip()
    file_obj    = request.files.get('pdf')

    if not title or not subject or not file_obj or file_obj.filename == '':
        flash('Missing required fields or file.', 'error')
        return redirect(url_for('faculty_dashboard'))

    uploader = session.get('name', 'Faculty')
    
    result = MaterialService.upload_material(file_obj, title, subject, description, uploader)
    
    if result['success']:
        flash(result['message'], 'success')
    else:
        flash(result['message'], 'error')
        
    return redirect(url_for('faculty_dashboard'))

@app.route('/api/materials/ticker', methods=['GET'])
def api_materials_ticker():
    """Returns materials metadata for the top scrolling bar/ticker."""
    limit = request.args.get('limit', 100, type=int)
    limit = min(max(limit, 1), 100)
    subject = request.args.get('subject', '').strip()
    result = MaterialService.get_materials(page=1, limit=limit, subject=subject if subject else None)
    ticker_items = []
    for mat in result['materials']:
        filename = get_clean_download_filename(mat)
        ticker_items.append({
            'id': mat['id'],
            'title': mat['title'],
            'filename': filename,
            'subject': mat['subject'],
            'file_size': mat.get('file_size', 0),
            'uploaded_by': mat.get('uploaded_by', 'Faculty'),
            'uploaded_at': str(mat['uploaded_at']) if mat.get('uploaded_at') else ''
        })
    return jsonify({'success': True, 'materials': ticker_items}), 200

@app.route('/api/materials', methods=['GET'])
def api_materials():
    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 100, type=int)
    search = request.args.get('search', '').strip()
    subject = request.args.get('subject', '').strip()
    
    limit = min(limit, 100) # Max 100 per request
    
    result = MaterialService.get_materials(page=page, limit=limit, search=search if search else None, subject=subject if subject else None)
    for mat in result.get('materials', []):
        if 'uploaded_at' in mat and mat['uploaded_at'] is not None:
            mat['uploaded_at'] = str(mat['uploaded_at'])
        if 'updated_at' in mat and mat['updated_at'] is not None:
            mat['updated_at'] = str(mat['updated_at'])
    return jsonify(result), 200

@app.route('/api/materials/<int:material_id>', methods=['GET'])
def get_material(material_id):
    mat = MaterialService.get_material_by_id(material_id)
    if not mat:
        return jsonify({'success': False, 'message': 'Material not found'}), 404
    if 'uploaded_at' in mat and mat['uploaded_at'] is not None:
        mat['uploaded_at'] = str(mat['uploaded_at'])
    if 'updated_at' in mat and mat['updated_at'] is not None:
        mat['updated_at'] = str(mat['updated_at'])
    return jsonify({'success': True, 'material': mat}), 200

@app.route('/api/materials/<int:material_id>/view', methods=['GET'])
def view_material(material_id):
    """Serve material inline for browser viewing"""
    mat = MaterialService.get_material_by_id(material_id)
    if not mat:
        return "Material not found or deleted", 404
        
    serving_item, is_stream = StorageService.get_file_for_serving(mat['relative_path'])
    if not serving_item:
        return "File is missing from disk or cloud storage", 404
        
    download_filename = get_clean_download_filename(mat)
    mime_type = mat.get('mime_type') or mimetypes.guess_type(download_filename)[0] or 'application/octet-stream'

    return send_file(
        serving_item,
        mimetype=mime_type,
        as_attachment=False,
        download_name=download_filename,
        conditional=not is_stream
    )

@app.route('/api/materials/<int:material_id>/download', methods=['GET'])
def download_material(material_id):
    """Download material as attachment with original filename"""
    mat = MaterialService.get_material_by_id(material_id)
    if not mat:
        return "Material not found or deleted", 404
        
    serving_item, is_stream = StorageService.get_file_for_serving(mat['relative_path'])
    if not serving_item:
        return "File is missing from disk or cloud storage", 404

    download_filename = get_clean_download_filename(mat)
    mime_type = mat.get('mime_type') or mimetypes.guess_type(download_filename)[0] or 'application/octet-stream'

    return send_file(
        serving_item,
        mimetype=mime_type,
        as_attachment=True,
        download_name=download_filename,
        conditional=not is_stream
    )

@app.route('/api/materials/<int:material_id>', methods=['DELETE'])
def delete_material(material_id):
    if session.get('role') != 'faculty':
        return jsonify({'success': False, 'message': 'Unauthorized. Faculty access required.'}), 403

    result = MaterialService.delete_material(material_id)
    if result['success']:
        return jsonify(result), 200
    else:
        status_code = 404 if result.get('message') == 'Material not found.' else 500
        return jsonify(result), status_code

@app.route('/api/materials/integrity', methods=['GET'])
def check_integrity():
    if session.get('role') != 'faculty':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    return jsonify(MaterialService.check_integrity()), 200
    
@app.route('/api/materials/backup', methods=['POST'])
def trigger_backup():
    if session.get('role') != 'faculty':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    try:
        res = BackupService.create_backup()
        return jsonify(res), 200 if res['success'] else 500
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

# ── Legacy support for frontend links ──

@app.route('/api/materials/file/<filename>')
def legacy_view(filename):
    mat = MaterialService.get_material_by_filename(filename)
    if mat:
        return redirect(url_for('view_material', material_id=mat['id']))
    return "Not found", 404

@app.route('/download/<filename>')
def legacy_download(filename):
    mat = MaterialService.get_material_by_filename(filename)
    if mat:
        return redirect(url_for('download_material', material_id=mat['id']))
    return "Not found", 404

@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'success')
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True)
