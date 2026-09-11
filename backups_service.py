"""Copias consistentes de SQLite y respaldo de archivos con manifiesto verificable."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone

from flask import send_file
from models import db


def sqlite_snapshot(destination):
    source = db.engine.raw_connection()
    target = sqlite3.connect(str(destination))
    try:
        with target:
            source.driver_connection.backup(target, pages=256, sleep=0.01)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('La copia de la base no pasó la verificación de integridad.')
    finally:
        target.close()
        source.close()


def download_database():
    # TemporaryFile se elimina al cerrar; no usa RAM proporcional al backup.
    with tempfile.TemporaryDirectory(prefix='arcano_db_') as tmp:
        snapshot = Path(tmp) / 'arcano_stock.db'
        sqlite_snapshot(snapshot)
        output = tempfile.TemporaryFile()
        with snapshot.open('rb') as src:
            import shutil
            shutil.copyfileobj(src, output)
    output.seek(0)
    response = send_file(output, as_attachment=True, download_name=f'arcano_backup_{datetime.now():%Y%m%d_%H%M%S}.db', mimetype='application/octet-stream')
    response.call_on_close(output.close)
    return response


def build_complete_backup(app, output):
    manifest = {'format': 1, 'created_utc': datetime.now(timezone.utc).isoformat(), 'files': {}}
    with tempfile.TemporaryDirectory(prefix='arcano_backup_') as tmp:
        snapshot = Path(tmp) / 'arcano_stock.db'
        sqlite_snapshot(snapshot)
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            def add(path, name):
                digest = hashlib.sha256()
                size = 0
                with path.open('rb') as src, archive.open(name, 'w', force_zip64=True) as dst:
                    for chunk in iter(lambda: src.read(1024 * 1024), b''):
                        dst.write(chunk); digest.update(chunk); size += len(chunk)
                manifest['files'][name] = {'size': size, 'sha256': digest.hexdigest()}
            add(snapshot, 'database/arcano_stock.db')
            roots = [(key.lower().replace('_dir', ''), Path(app.config[key]))
                     for key in ('COVERS_DIR', 'SAMPLES_DIR', 'COMPROBANTES_DIR')]
            # Archivos antiguos servidos desde static: se preservan por separado.
            roots += [('legacy_static/' + name, Path(app.static_folder) / name)
                      for name in ('covers', 'samples', 'comprobantes')
                      if (Path(app.static_folder) / name).resolve() not in {p.resolve() for _, p in roots}]
            for label, root in roots:
                if not root.exists():
                    continue
                for path in sorted(root.rglob('*')):
                    if path.is_file() and not path.is_symlink() and root.resolve() in path.resolve().parents:
                        add(path, label + '/' + path.relative_to(root).as_posix())
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    output.seek(0)
    return manifest


def download_complete(app):
    output = tempfile.TemporaryFile()
    try:
        build_complete_backup(app, output)
        response = send_file(output, as_attachment=True, download_name=f'arcano_completo_{datetime.now():%Y%m%d_%H%M%S}.zip', mimetype='application/zip')
        response.call_on_close(output.close)
        return response
    except Exception:
        output.close()
        raise
