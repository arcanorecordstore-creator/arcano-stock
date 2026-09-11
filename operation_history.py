"""Historial transaccional SQLite y recuperacion con control de conflictos.

Los triggers se ejecutan dentro de la misma transaccion que el cambio, incluidos
UPDATE/DELETE masivos y cascadas. No se guardan secretos de configuracion.
"""
import json
import uuid
from datetime import datetime, date
from flask import g, has_request_context, request, session, render_template, redirect, url_for, flash
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from models import db
import auth


class OperationChange(db.Model):
    __tablename__ = 'operation_changes'
    id = db.Column(db.Integer, primary_key=True)
    batch = db.Column(db.String(40), nullable=False, index=True)
    fecha = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    actor = db.Column(db.String(100), nullable=False)
    accion = db.Column(db.String(150), nullable=False)
    motivo = db.Column(db.String(400), nullable=False, default='')
    tabla = db.Column(db.String(60), nullable=False)
    registro_id = db.Column(db.Integer, nullable=False)
    antes = db.Column(db.Text)
    despues = db.Column(db.Text)


class ArchivedRecord(db.Model):
    __tablename__ = 'archived_records'
    id = db.Column(db.Integer, primary_key=True)
    tabla = db.Column(db.String(60), nullable=False)
    registro_id = db.Column(db.Integer, nullable=False)
    __table_args__ = (db.UniqueConstraint('tabla', 'registro_id'),)


TABLE_LABELS = {'discs':'Discos / ventas', 'reservas':'Reservas', 'clientes':'Clientes',
                'egresos':'Gastos', 'pedidos':'Pedidos', 'movimientos_caja':'Caja',
                'egresos_recurrentes':'Gastos recurrentes'}
TRACKED = set(TABLE_LABELS) | {'reserva_items', 'tracks', 'disc_images', 'status_history', 'cliente_favoritos', 'play_events', 'archived_records'}


def retained_samples():
    """Referencias del historial: no son huerfanos descartables."""
    rows = db.session.execute(text("SELECT json_extract(antes, '$.sample_local'), json_extract(despues, '$.sample_local') FROM operation_changes WHERE tabla='tracks'"))
    return {name for row in rows for name in row if name}


def _batch():
    if not has_request_context():
        return ''
    if not hasattr(g, 'operation_batch'):
        g.operation_batch = uuid.uuid4().hex
    return g.operation_batch


@event.listens_for(Engine, 'connect')
def _register_sql_functions(connection, _record):
    if not hasattr(connection, 'create_function'):
        return
    connection.create_function('audit_batch', 0, _batch)
    connection.create_function('audit_actor', 0, lambda: str(session.get('admin_user') or ('cliente' if session.get('cliente_id') else 'sistema/web')) if has_request_context() else 'sistema')
    connection.create_function('audit_action', 0, lambda: (request.endpoint or request.path) if has_request_context() else 'sistema')
    connection.create_function('audit_reason', 0, lambda: str(request.form.get('motivo', '') or '')[:400] if has_request_context() else '')


def install_triggers():
    with db.engine.begin() as conn:
        for table in db.metadata.sorted_tables:
            if table.name not in TRACKED or 'id' not in table.c:
                continue
            def snapshot(prefix):
                args = ','.join("'%s',%s.\"%s\"" % (c.name, prefix, c.name) for c in table.columns)
                return 'json_object(' + args + ')'
            for verb in ('INSERT', 'UPDATE', 'DELETE'):
                before = 'NULL' if verb == 'INSERT' else snapshot('OLD')
                after = 'NULL' if verb == 'DELETE' else snapshot('NEW')
                row = 'OLD' if verb == 'DELETE' else 'NEW'
                name = f'audit_{table.name}_{verb.lower()}'
                # Regenerar tras migraciones permite incluir columnas agregadas.
                conn.exec_driver_sql(f'DROP TRIGGER IF EXISTS "{name}"')
                conn.exec_driver_sql(f'''CREATE TRIGGER "{name}" AFTER {verb} ON "{table.name}"
                    WHEN audit_batch() != ''
                    BEGIN INSERT INTO operation_changes
                    (batch,fecha,actor,accion,motivo,tabla,registro_id,antes,despues)
                    VALUES (audit_batch(),datetime('now'),audit_actor(),audit_action(),audit_reason(),
                    '{table.name}',{row}.id,{before},{after}); END''')


def archived_ids(table):
    if not has_request_context():
        return set()
    if not hasattr(g, 'archive_map'):
        g.archive_map = {}
        for item in ArchivedRecord.query.all():
            g.archive_map.setdefault(item.tabla, set()).add(item.registro_id)
    return g.archive_map.get(table, set())


def visible_records(records, table):
    hidden = archived_ids(table)
    return [r for r in records if r.id not in hidden]


def restore_batch(batch):
    changes = OperationChange.query.filter_by(batch=batch).order_by(OperationChange.id).all()
    if not changes:
        raise ValueError('No se encontró la operación.')
    combined = {}
    for change in changes:
        key = (change.tabla, change.registro_id)
        before = json.loads(change.antes) if change.antes else None
        after = json.loads(change.despues) if change.despues else None
        if key not in combined:
            combined[key] = [before, after]
        else:
            combined[key][1] = after
    conn = db.session.connection()
    # Comparacion SQL/json: conserva las representaciones exactas de fechas.
    for (table, rid), (before, after) in combined.items():
        current = conn.execute(text(f'SELECT * FROM "{table}" WHERE id=:id'), {'id':rid}).mappings().first()
        if (dict(current) if current else None) != after:
            raise ValueError(f'El registro {TABLE_LABELS.get(table, table)} #{rid} cambió después. No se recuperó nada para evitar pisar cambios posteriores.')
    conn.exec_driver_sql('PRAGMA defer_foreign_keys=ON')
    order = {t.name:i for i,t in enumerate(db.metadata.sorted_tables)}
    # Padres antes de hijos para recrear; bajas en orden inverso.
    items = list(combined.items())
    for (table, rid), (before, after) in sorted(items, key=lambda x: order.get(x[0][0], 0)):
        if before is None:
            continue
        cols = list(before)
        params = {f'v{i}':before[k] for i,k in enumerate(cols)}
        if after is None:
            names = ','.join('"'+k+'"' for k in cols)
            values = ','.join(':v'+str(i) for i in range(len(cols)))
            conn.execute(text(f'INSERT INTO "{table}" ({names}) VALUES ({values})'), params)
        else:
            sets = ','.join(f'"{k}"=:v{i}' for i,k in enumerate(cols))
            conn.execute(text(f'UPDATE "{table}" SET {sets} WHERE id=:rid'), dict(params,rid=rid))
    for (table, rid), (before, after) in sorted(items, key=lambda x: -order.get(x[0][0], 0)):
        if before is None and after is not None:
            conn.execute(text(f'DELETE FROM "{table}" WHERE id=:id'), {'id':rid})
    db.session.commit()
    db.session.expire_all()


def register_history(app):
    with app.app_context():
        install_triggers()
    app.jinja_env.globals['is_archived'] = lambda table, rid: rid in archived_ids(table)

    @app.route('/admin/historial')
    @auth.admin_required
    def admin_historial():
        page = max(1, request.args.get('pagina', 1, type=int))
        batches = db.session.query(OperationChange.batch, db.func.max(OperationChange.id).label('last_id')).group_by(OperationChange.batch).order_by(db.desc('last_id')).limit(50).offset((page-1)*50).all()
        groups = []
        for batch, _ in batches:
            changes = OperationChange.query.filter_by(batch=batch).order_by(OperationChange.id).all()
            first = changes[0]
            groups.append({'batch':batch,'fecha':first.fecha,'actor':first.actor,'accion':first.accion,
                           'motivo':first.motivo,'changes':changes})
        table = request.args.get('tipo', 'reservas')
        if table not in TABLE_LABELS:
            table = 'reservas'
        records = db.session.execute(db.select(db.metadata.tables[table]).order_by(db.metadata.tables[table].c.id.desc()).limit(100)).mappings().all() if table in db.metadata.tables else []
        return render_template('admin_historial.html', groups=groups, pagina=page, records=records, tipo=table, labels=TABLE_LABELS)

    @app.route('/admin/historial/<batch>/recuperar', methods=['POST'])
    @auth.admin_required
    def admin_historial_recuperar(batch):
        if not request.form.get('motivo', '').strip():
            flash('Escribí el motivo de la recuperación.', 'error')
            return redirect(url_for('admin_historial'))
        try:
            restore_batch(batch)
            flash('Operación recuperada. Quedó registrada también esta recuperación.', 'success')
        except ValueError as exc:
            db.session.rollback(); flash(str(exc), 'error')
        except Exception:
            db.session.rollback(); flash('No se pudo recuperar: hay datos relacionados que requieren revisión. No se aplicó la recuperación.', 'error')
        return redirect(url_for('admin_historial'))

    @app.route('/admin/historial/archivar', methods=['POST'])
    @auth.admin_required
    def admin_historial_archivar():
        table = request.form.get('tipo')
        rid = request.form.get('registro_id', type=int)
        if table not in TABLE_LABELS or not rid:
            return 'Registro inválido', 400
        target = db.metadata.tables[table]
        if not db.session.execute(db.select(target.c.id).where(target.c.id == rid)).first():
            return 'Registro inexistente', 404
        item = ArchivedRecord.query.filter_by(tabla=table,registro_id=rid).first()
        if request.form.get('accion') == 'mostrar':
            if item: db.session.delete(item)
        elif item is None:
            db.session.add(ArchivedRecord(tabla=table,registro_id=rid))
        db.session.commit()
        flash('Visibilidad actualizada. Los importes y el estado de la operación no cambiaron.', 'success')
        return redirect(url_for('admin_historial', tipo=table))
