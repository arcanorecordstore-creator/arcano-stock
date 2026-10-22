"""Historial transaccional SQLite y recuperacion con control de conflictos.

Los triggers se ejecutan dentro de la misma transaccion que el cambio, incluidos
UPDATE/DELETE masivos y cascadas. No se guardan secretos de configuracion.
"""
import json
import uuid
from datetime import datetime, date
from flask import g, has_request_context, request, session, render_template, redirect, url_for, flash, jsonify
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
# play_events NO se registra (v5.77): cada escucha publica de la tienda escribia 2 filas
# en el historial (una tambien por el contador del track) -> crecia sin limite y un bot
# podia llenar el disco de Render. Las escuchas son estadistica, no algo para "deshacer".
TRACKED = set(TABLE_LABELS) | {'reserva_items', 'tracks', 'disc_images', 'status_history', 'cliente_favoritos', 'archived_records'}
# Columnas que, si son lo UNICO que cambio en un UPDATE, no vale la pena registrar
# (el contador de escuchas, la marca de "ultima modificacion").
IGNORED_COLS = {'play_count', 'fecha_modificacion', 'meta_capi_enviado'}
# Cuanto se guarda: lo automatico (escuchas, sincronizaciones al abrir pantallas) 30
# dias; lo que hiciste vos, 1 año. Los archivos de un disco/track borrado quedan
# protegidos (para poder recuperarlo) solo esos mismos 30 dias.
DIAS_AUTOMATICO = 30
DIAS_TODO = 365
DIAS_ARCHIVOS = 30


def retained_samples(dias=DIAS_ARCHIVOS):
    """Samples que figuran en el historial RECIENTE (ultimos `dias`): siguen siendo
    recuperables, asi que la limpieza no los borra. Antes los protegia para siempre y
    "Limpiar samples sin usar" nunca liberaba espacio."""
    rows = db.session.execute(text(
        "SELECT json_extract(antes, '$.sample_local'), json_extract(despues, '$.sample_local') "
        "FROM operation_changes WHERE tabla='tracks' AND fecha >= datetime('now', :lim)"),
        {'lim': f'-{int(dias)} days'})
    return {name for row in rows for name in row if name}


def podar_historial():
    """Borra lo que ya no sirve del historial (best-effort, se llama al arrancar):
    escuchas viejas, lo automatico de mas de 30 dias y todo lo de mas de 1 año."""
    from historial_legible import AUTOMATICAS
    try:
        autos = ','.join("'%s'" % a.replace("'", "") for a in AUTOMATICAS)
        with db.engine.begin() as conn:
            conn.exec_driver_sql(
                "DELETE FROM operation_changes WHERE tabla='play_events' OR accion='api_track_play'")
            conn.exec_driver_sql(
                f"DELETE FROM operation_changes WHERE accion IN ({autos}) "
                f"AND fecha < datetime('now', '-{DIAS_AUTOMATICO} days')")
            conn.exec_driver_sql(
                f"DELETE FROM operation_changes WHERE fecha < datetime('now', '-{DIAS_TODO} days')")
    except Exception as e:
        print(f"Historial: no se pudo podar ({e})")


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
        # Indices para que el historial no se ponga lento al crecer (antes solo 'batch').
        for col in ('fecha', 'accion', 'tabla'):
            conn.exec_driver_sql(
                f'CREATE INDEX IF NOT EXISTS ix_operation_changes_{col} ON operation_changes ({col})')
        for table in db.metadata.sorted_tables:
            if table.name not in TRACKED or 'id' not in table.c:
                # Tabla que dejo de registrarse (ej. play_events): sacar sus triggers viejos.
                for verb in ('insert', 'update', 'delete'):
                    conn.exec_driver_sql(f'DROP TRIGGER IF EXISTS "audit_{table.name}_{verb}"')
                continue
            def snapshot(prefix):
                args = ','.join("'%s',%s.\"%s\"" % (c.name, prefix, c.name) for c in table.columns)
                return 'json_object(' + args + ')'
            # UPDATE: solo registrar si cambio algo que importa (no el contador de
            # escuchas ni la marca de ultima modificacion).
            relevantes = [c.name for c in table.columns if c.name not in IGNORED_COLS]
            cambio = ' OR '.join('OLD."%s" IS NOT NEW."%s"' % (c, c) for c in relevantes) or '1'
            for verb in ('INSERT', 'UPDATE', 'DELETE'):
                before = 'NULL' if verb == 'INSERT' else snapshot('OLD')
                after = 'NULL' if verb == 'DELETE' else snapshot('NEW')
                row = 'OLD' if verb == 'DELETE' else 'NEW'
                name = f'audit_{table.name}_{verb.lower()}'
                cond = f"audit_batch() != '' AND ({cambio})" if verb == 'UPDATE' else "audit_batch() != ''"
                # Regenerar tras migraciones permite incluir columnas agregadas.
                conn.exec_driver_sql(f'DROP TRIGGER IF EXISTS "{name}"')
                conn.exec_driver_sql(f'''CREATE TRIGGER "{name}" AFTER {verb} ON "{table.name}"
                    WHEN {cond}
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
        podar_historial()
    app.jinja_env.globals['is_archived'] = lambda table, rid: rid in archived_ids(table)

    @app.route('/admin/historial')
    @auth.admin_required
    def admin_historial():
        from historial_legible import describir, nombres_de_discos, AUTOMATICAS, TIPOS
        from models import today_uy
        page = max(1, request.args.get('pagina', 1, type=int))
        # ver: 'mias' (lo que hizo Facundo, sin escuchas ni sincronizaciones solas) | 'todo'
        ver = 'todo' if request.args.get('ver') == 'todo' else 'mias'
        que = request.args.get('que', '')   # disco | reserva | cliente | gasto | pedido | caja
        q = db.session.query(OperationChange.batch, db.func.max(OperationChange.id).label('last_id'))
        if ver == 'mias':
            q = q.filter(~OperationChange.accion.in_(AUTOMATICAS))
        tablas_que = [t for t, (k, _) in TIPOS.items() if k == que]
        if tablas_que:
            q = q.filter(OperationChange.batch.in_(
                db.session.query(OperationChange.batch).filter(OperationChange.tabla.in_(tablas_que))))
        batches = q.group_by(OperationChange.batch).order_by(db.desc('last_id')).limit(50).offset((page-1)*50).all()
        todos = OperationChange.query.filter(OperationChange.batch.in_([b for b, _ in batches])).order_by(OperationChange.id).all()
        por_batch = {}
        for ch in todos:
            por_batch.setdefault(ch.batch, []).append(ch)
        discos = nombres_de_discos(todos)
        groups = []
        for batch, _ in batches:
            changes = por_batch.get(batch) or []
            if not changes:
                continue
            info = describir(changes, discos)
            info.update({'batch': batch, 'fecha': changes[0].fecha, 'n_cambios': len(changes)})
            groups.append(info)
        # Agrupar por dia (hora de Uruguay)
        hoy = today_uy()
        dias = []
        for g_ in groups:
            dia = g_['fecha_uy'].date()
            if not dias or dias[-1]['dia'] != dia:
                dif = (hoy - dia).days
                nombre = 'Hoy' if dif == 0 else ('Ayer' if dif == 1 else None)
                dias.append({'dia': dia, 'nombre': nombre, 'ops': []})
            dias[-1]['ops'].append(g_)
        table = request.args.get('tipo', 'reservas')
        if table not in TABLE_LABELS:
            table = 'reservas'
        records = db.session.execute(db.select(db.metadata.tables[table]).order_by(db.metadata.tables[table].c.id.desc()).limit(100)).mappings().all() if table in db.metadata.tables else []
        return render_template('admin_historial.html', groups=groups, dias=dias, pagina=page, records=records,
                               tipo=table, labels=TABLE_LABELS, ver=ver, que=que)

    @app.route('/admin/historial/<batch>/recuperar', methods=['POST'])
    @auth.admin_required
    def admin_historial_recuperar(batch):
        if not request.form.get('motivo', '').strip():
            flash('Escribí el motivo para deshacer el cambio.', 'error')
            return redirect(request.referrer or url_for('admin_historial'))
        try:
            restore_batch(batch)
            flash('Listo: quedó como estaba antes de ese cambio. Esta recuperación también queda en el Historial.', 'success')
        except ValueError as exc:
            db.session.rollback(); flash(str(exc), 'error')
        except Exception:
            db.session.rollback(); flash('No se pudo recuperar: hay datos relacionados que requieren revisión. No se aplicó la recuperación.', 'error')
        return redirect(request.referrer or url_for('admin_historial'))

    # Endpoints (vistas GET) que hacen escrituras "de sincronizacion" solas al
    # abrirse (ej. la caja se sincroniza). No son acciones deliberadas, asi que
    # Ctrl+Z las saltea para deshacer lo ultimo que hizo el usuario a proposito.
    # Misma lista que usa el Historial para "automaticas" (antes eran dos listas que
    # se desincronizaron y Ctrl+Z deshacia una escucha o la sincronizacion de caja
    # del Home en vez de tu ultimo cambio).
    from historial_legible import AUTOMATICAS as _VISTAS_SYNC

    def _ultima_operacion_deshacible():
        actor = str(session.get('admin_user') or '')
        q = OperationChange.query
        if actor:
            q = q.filter_by(actor=actor)
        q = q.filter(~OperationChange.accion.in_(_VISTAS_SYNC))
        # Ctrl+Z es para "me equivoque recien": solo lo de las ultimas 3 horas. Lo mas
        # viejo se deshace desde el Historial, viendo bien que es (antes podia
        # proponer deshacer algo de hace dias sin que se notara).
        from datetime import timedelta as _td
        q = q.filter(OperationChange.fecha >= datetime.utcnow() - _td(hours=3))
        return q.order_by(OperationChange.id.desc()).first()

    @app.route('/admin/deshacer', methods=['GET', 'POST'])
    @auth.admin_required
    def admin_deshacer():
        last = _ultima_operacion_deshacible()
        if not last:
            if request.method == 'GET':
                return jsonify({'hay': False})
            flash('No hay ninguna operación reciente para deshacer.', 'error')
            return redirect(request.referrer or url_for('dashboard'))
        batch = last.batch
        changes = OperationChange.query.filter_by(batch=batch).order_by(OperationChange.id).all()
        # Resumen en castellano (el mismo texto del Historial) y hora de Uruguay.
        try:
            from historial_legible import describir, nombres_de_discos
            info = describir(changes, nombres_de_discos(changes))
            resumen = info['titulo']
            if info['items'] and info['items'][0]['campos']:
                n, a, d = info['items'][0]['campos'][0]
                resumen += f' ({n}: {a} → {d})' if a != 'cambió' else f' ({n})'
        except Exception:
            resumen = ' · '.join(sorted({TABLE_LABELS.get(c.tabla, c.tabla) for c in changes})) or 'cambios'
        from datetime import timedelta as _td
        if request.method == 'GET':
            return jsonify({
                'hay': True,
                'resumen': resumen,
                'fecha': (changes[0].fecha - _td(hours=3)).strftime('%d/%m %H:%M'),
            })
        try:
            restore_batch(batch)
            flash(f'Se deshizo la última operación ({resumen}). Quedó registrada en Historial.', 'success')
        except ValueError as exc:
            db.session.rollback(); flash(str(exc), 'error')
        except Exception:
            db.session.rollback(); flash('No se pudo deshacer: hay datos relacionados que requieren revisión. Revisá el Historial.', 'error')
        return redirect(request.referrer or url_for('dashboard'))

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
