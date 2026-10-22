"""Traduce el historial tecnico (operation_changes) a algo que se entienda:
que se hizo, sobre que (disco, reserva, cliente...), que campos cambiaron
(antes -> despues) y desde donde se hizo. Lo usa la pagina /admin/historial."""
import json
from datetime import timedelta

# --- Desde donde se hizo (endpoint de Flask -> texto) -------------------------
ORIGENES = {
    # Stock / discos
    'disc_quick_update': 'Edición rápida en Stock',
    'disc_bulk_action': 'Acción en varios discos (Stock)',
    'disc_status': 'Cambio de estado del disco',
    'disc_edit': 'Edición de la ficha del disco',
    'disc_new': 'Alta de disco (carga manual)',
    'disc_new_from_url': 'Alta de disco desde Deejay.de',
    'disc_new_combo': 'Alta de disco (URL + video)',
    'disc_combo_review': 'Alta de disco (URL + video)',
    'disc_new_from_video': 'Alta de disco desde video',
    'disc_video_review': 'Alta de disco desde video',
    'disc_new_from_image': 'Alta de disco desde imagen',
    'disc_new_from_invoice': 'Alta de discos desde factura',
    'disc_invoice_review': 'Alta de discos desde factura',
    'disc_delete': 'Borrado de disco',
    'disc_duplicate': 'Duplicar disco',
    'disc_upload_cover': 'Cambio de portada',
    'disc_images_upload': 'Fotos de la galería',
    'disc_image_delete': 'Borrado de foto de la galería',
    'disc_images_reorder': 'Orden de fotos de la galería',
    'discs_reorder': 'Orden de los discos en la tienda',
    'admin_disc_sena': 'Seña de un disco',
    'api_crear_disco': 'Alta de disco desde Drop Hunter',
    'tracks_add': 'Tracks agregados', 'tracks_update': 'Edición de tracks',
    'tracks_delete': 'Track borrado', 'tracks_reorder': 'Orden de tracks',
    'tracks_parse': 'Tracklist cargado', 'tracks_upload_sample': 'Audio de un track',
    'tracks_clear_sample': 'Audio de un track quitado',
    # Reservas / ventas
    'admin_reserva_nueva': 'Nueva reserva manual',
    'admin_reserva_detail': 'Cambio en el detalle de una reserva',
    'reserva_toggle_entrega': 'Entrega de una reserva',
    'reservas_agrupar': 'Agrupar reservas', 'reservas_desagrupar': 'Desagrupar reservas',
    'public_checkout': 'Compra / reserva hecha en la web',
    'public_reservar': 'Reserva hecha en la web',
    'mp_webhook': 'Aviso de pago de Mercado Pago',
    'ventas_cargar': 'Venta cargada a mano', 'ventas_eliminar': 'Venta eliminada',
    'ventas_agrupar': 'Agrupar ventas', 'ventas_desagrupar': 'Desagrupar ventas',
    'compra_set_canal': 'Canal / método de pago de una venta',
    # Clientes
    'admin_cliente_nuevo': 'Cliente nuevo (cargado a mano)',
    'admin_cliente_editar': 'Edición de cliente', 'admin_cliente_eliminar': 'Cliente borrado',
    'admin_clientes_eliminar_multiple': 'Varios clientes borrados',
    'admin_cliente_fusionar': 'Clientes unificados',
    'admin_cliente_agregar_disco': 'Disco asignado a un cliente',
    'admin_cliente_premio': 'Premio de lealtad entregado',
    'cliente_registro': 'Un cliente creó su cuenta en la web',
    'cliente_toggle_avisos': 'Un cliente cambió sus avisos',
    # Plata
    'egreso_agregar': 'Gasto cargado', 'egreso_editar': 'Gasto editado',
    'egreso_borrar': 'Gasto borrado', 'egreso_monto_uyu': 'Monto de un gasto',
    'recurrente_agregar': 'Gasto fijo agregado', 'recurrente_borrar': 'Gasto fijo borrado',
    'generar_gastos_mes': 'Gastos fijos del mes generados',
    'pedido_agregar': 'Pedido cargado', 'pedido_editar': 'Pedido editado',
    'pedido_borrar': 'Pedido borrado', 'pedido_costo_manual': 'Costo de un pedido',
    'caja_movimiento_agregar': 'Movimiento de caja cargado',
    'caja_movimiento_editar': 'Movimiento de caja editado',
    'caja_movimiento_eliminar': 'Movimiento de caja borrado',
    'caja_movimiento_quick_update': 'Edición rápida en Caja',
    'caja_saldo_inicial': 'Saldo inicial de caja', 'caja_cuadrar': 'Caja cuadrada',
    'caja_marcar_aporte': 'Aporte de capital', 'caja_adelantar_mp': 'Adelanto de Mercado Pago',
    'caja_saldar_adelanto': 'Adelanto de Mercado Pago saldado',
    # Historial / sistema
    'admin_historial_recuperar': 'Recuperación desde el Historial',
    'admin_deshacer': 'Deshacer (Ctrl+Z)',
    'settings_recalculate': 'Recalcular stock (Ajustes)',
    'admin_limpiar_samples': 'Limpieza de audios sin usar',
    'nota_nueva': 'Nota nueva (Estrategia)', 'nota_borrar': 'Nota borrada (Estrategia)',
    'preguntar_datos': 'Pregunta a tus datos (Estrategia)',
    'preguntar_borrar': 'Pregunta borrada del historial (Estrategia)',
}

# Operaciones que no hiciste vos a proposito: escuchas y favoritos de visitantes,
# y las sincronizaciones que hacen algunas pantallas solas al abrirse.
AUTOMATICAS = {
    'api_track_play', 'cliente_favoritos_sync', 'cliente_favoritos_toggle', 'push_subscribe',
    'caja', 'dashboard', 'contabilidad', 'ventas_panel', 'admin_reservas', 'pedidos',
    'radar_demanda', 'admin_clientes', 'home', 'home_admin', 'admin_historial',
    'sistema', 'cliente_login_page', 'admin_login',
    # Exportes e informes: sincronizan la caja antes de armar el archivo
    'contabilidad_exportar', 'contabilidad_informe_pdf', 'informe_ejecutivo_pdf',
    # Preguntar a tus datos: sincroniza la caja antes de leer los saldos
    'preguntar_datos',
}

# --- Que es cada tabla ---------------------------------------------------------
TIPOS = {
    'discs': ('disco', 'Disco'), 'reservas': ('reserva', 'Reserva'),
    'reserva_items': ('reserva', 'Disco de una reserva'), 'clientes': ('cliente', 'Cliente'),
    'egresos': ('gasto', 'Gasto'), 'egresos_recurrentes': ('gasto', 'Gasto fijo'),
    'pedidos': ('pedido', 'Pedido'), 'movimientos_caja': ('caja', 'Movimiento de caja'),
    'tracks': ('disco', 'Track'), 'disc_images': ('disco', 'Foto de disco'),
    'status_history': ('disco', 'Historial de estado'), 'cliente_favoritos': ('cliente', 'Favorito'),
    'play_events': ('disco', 'Escucha'), 'archived_records': ('sistema', 'Archivado'),
}
# Filas "de apoyo": se cuentan pero no se listan (ya se ven en el cambio principal).
SECUNDARIAS = {'status_history', 'play_events', 'cliente_favoritos', 'archived_records'}
# Orden para elegir el cambio principal de la operacion (el del titulo).
PRIORIDAD = ['reservas', 'discs', 'clientes', 'pedidos', 'egresos', 'egresos_recurrentes',
             'movimientos_caja', 'reserva_items', 'tracks', 'disc_images',
             'status_history', 'cliente_favoritos', 'play_events', 'archived_records']

# --- Nombres de los campos -------------------------------------------------------
CAMPOS = {
    'artista': 'Artista', 'titulo': 'Título', 'sello': 'Sello', 'anio': 'Año', 'formato': 'Formato',
    'pais': 'País', 'genero': 'Género', 'tracklist': 'Tracklist', 'cover_url': 'Portada (link)',
    'cover_local': 'Portada', 'source_url': 'Link de origen', 'costo_eur': 'Costo €',
    'envio_eur': 'Envío €', 'tc_usado': 'Tipo de cambio', 'costo_final_uyu': 'Costo final',
    'precio_uyu': 'Precio', 'stock': 'Stock', 'estado': 'Estado', 'condicion': 'Condición',
    'origen': 'Origen', 'notas': 'Notas', 'descripcion_publica': 'Descripción pública', 'tags': 'Tags',
    'destacado': 'Destacado', 'en_nuevos': 'En "Nuevos ingresos"', 'cartel_nuevo': 'Cartel "Nuevo"',
    'cartel_ultima': 'Cartel "Última copia"', 'precio_oferta': 'Precio de oferta', 'oferta_hasta': 'Oferta hasta',
    'en_fire_sale': 'En Fire Sale', 'precio_venta_uyu': 'Precio de venta', 'mostrar_agotado': 'Mostrar agotado', 'oculto_web': 'NO mostrar en la web',
    'es_tip': 'Staff Pick', 'es_mystery_box': 'Mystery Box', 'resena_corta': 'Reseña corta',
    'audio_estado': 'Estado del audio', 'es_preorder': 'Pre-order', 'copias_vendidas': 'Copias vendidas',
    'pedido_id': 'Pedido', 'cliente_id': 'Cliente', 'canal_venta': 'Canal de venta',
    'metodo_pago_venta': 'Método de pago', 'venta_grupo': 'Grupo de venta', 'sena_cobrada': 'Seña cobrada',
    'sena_monto_uyu': 'Monto de la seña', 'sena_fecha': 'Fecha de la seña', 'sena_metodo': 'Método de la seña',
    'orden': 'Orden en la tienda', 'fecha_ingreso': 'Fecha de ingreso', 'fecha_reserva': 'Fecha de reserva',
    'fecha_venta': 'Fecha de venta',
    # reservas
    'disc_id': 'Disco', 'nombre': 'Nombre', 'whatsapp': 'WhatsApp', 'email': 'Email', 'nota': 'Nota',
    'metodo_pago': 'Método de pago', 'comprobante_file': 'Comprobante', 'pagado': 'Pagado',
    'mp_status': 'Estado en Mercado Pago', 'total_cobrado_uyu': 'Total cobrado', 'entrega_tipo': 'Entrega',
    'envio_costo_uyu': 'Costo de envío', 'envio_direccion': 'Dirección de envío', 'envio_agencia': 'Agencia',
    'entregado': 'Entregado', 'canal': 'Canal', 'grupo': 'Grupo', 'fecha': 'Fecha',
    'disc_artista_snap': 'Artista', 'disc_titulo_snap': 'Título', 'disc_precio_uyu_snap': 'Precio',
    'artista_snap': 'Artista', 'titulo_snap': 'Título', 'precio_uyu_snap': 'Precio',
    'mbox_cantidad': 'Cantidad (Mystery Box)', 'mbox_texto': 'Detalle (Mystery Box)',
    # clientes
    'instagram': 'Instagram', 'lealtad_base': 'Base de lealtad', 'premio1_cobrado': 'Premio cobrado',
    'acepta_avisos': 'Acepta avisos', 'fecha_registro': 'Fecha de registro',
    # plata
    'categoria': 'Categoría', 'descripcion': 'Descripción', 'monto': 'Monto', 'moneda': 'Moneda',
    'monto_uyu': 'Monto en $', 'activo': 'Activo', 'numero': 'Número', 'cant_discos': 'Cantidad de discos',
    'costo_usd': 'Costo US$', 'costo_uyu_manual': 'Costo manual', 'seguimiento_url': 'Link de seguimiento', 'proveedor': 'Proveedor',
    'servicio_eur': 'Servicio €', 'comision_banco_uyu': 'Comisión del banco', 'tipo': 'Tipo',
    'concepto': 'Concepto', 'metodo': 'Método', 'origen_id': 'Origen (número)', 'es_aporte': 'Es aporte',
    'adelanto_mp': 'Adelanto de MP', 'manual_override': 'Editado a mano', 'origen_categoria': 'Categoría de origen',
    # tracks / imagenes
    'posicion': 'Posición', 'lado': 'Lado', 'duracion': 'Duración', 'sample_local': 'Audio',
    'youtube_url': 'Link de YouTube / SoundCloud', 'youtube_start': 'Segundo de inicio',
    'play_count': 'Escuchas', 'filename': 'Archivo',
}
# Campos tecnicos que no aportan nada al leer el cambio.
OCULTOS = {'id', 'slug', 'fecha_modificacion', 'fecha_actualizacion', 'fecha_es_utc', 'password_hash',
           'mp_preference_id', 'mp_payment_id', 'creado', 'sample_url'}
PLATA = {'precio_uyu', 'costo_final_uyu', 'precio_oferta', 'precio_venta_uyu', 'sena_monto_uyu',
         'total_cobrado_uyu', 'envio_costo_uyu', 'disc_precio_uyu_snap', 'precio_uyu_snap', 'monto_uyu',
         'costo_uyu_manual', 'comision_banco_uyu', 'lealtad_base'}
LARGOS = {'tracklist', 'descripcion_publica', 'notas', 'nota', 'mbox_texto'}
BOOLS = {'destacado', 'en_nuevos', 'cartel_nuevo', 'cartel_ultima', 'en_fire_sale', 'mostrar_agotado', 'oculto_web',
         'es_tip', 'es_mystery_box', 'es_preorder', 'sena_cobrada', 'pagado', 'entregado',
         'premio1_cobrado', 'acepta_avisos', 'activo', 'es_aporte', 'manual_override', 'fecha_es_utc'}


def _fmt(campo, v):
    if v is None or v == '':
        return '—'
    if campo in BOOLS:
        return 'Sí' if v in (1, True, '1', 'true') else 'No'
    if campo in PLATA:
        try:
            return '$ ' + f'{float(v):,.0f}'.replace(',', '.')
        except (TypeError, ValueError):
            return str(v)
    s = str(v)
    if campo.startswith('fecha') and len(s) >= 16 and s[4] == '-':
        # '2026-09-13 00:28:21.126' -> '13/09/2026 00:28' (fechas tal cual estan guardadas)
        return f'{s[8:10]}/{s[5:7]}/{s[0:4]}' + (f' {s[11:16]}' if len(s) > 10 else '')
    if campo in LARGOS or len(s) > 70:
        s = s.replace('\n', ' · ')
        return s[:70] + '…' if len(s) > 70 else s
    return s


def _nombre(tabla, fila, discos):
    """Nombre legible del registro a partir de su foto (antes o despues)."""
    f = fila or {}
    if tabla == 'discs':
        return ' — '.join(x for x in (f.get('artista'), f.get('titulo')) if x) or f'Disco #{f.get("id")}'
    if tabla == 'reservas':
        quien = f.get('nombre') or 'sin nombre'
        return f'Reserva #{f.get("id")} de {quien}'
    if tabla == 'reserva_items':
        t = ' — '.join(x for x in (f.get('artista_snap'), f.get('titulo_snap')) if x) or 'un disco'
        return f'{t} (en la reserva #{f.get("reserva_id")})'
    if tabla == 'clientes':
        return f.get('nombre') or f.get('email') or f'Cliente #{f.get("id")}'
    if tabla in ('egresos', 'egresos_recurrentes'):
        d = f.get('descripcion') or f.get('categoria') or 'Gasto'
        return f'{d} ({_fmt("monto", f.get("monto"))} {f.get("moneda") or ""})'.strip()
    if tabla == 'pedidos':
        return f'Pedido {f.get("numero") or "#" + str(f.get("id"))}' + (f' · {f["proveedor"]}' if f.get('proveedor') else '')
    if tabla == 'movimientos_caja':
        return f'{f.get("concepto") or "Movimiento"} ({_fmt("monto_uyu", f.get("monto_uyu"))})'
    if tabla in ('tracks', 'disc_images', 'status_history', 'play_events', 'cliente_favoritos'):
        disco = discos.get(f.get('disc_id'))
        extra = {'tracks': f.get('titulo') or 'track', 'disc_images': 'foto',
                 'status_history': f.get('estado') or 'estado'}.get(tabla, '')
        return f'{extra} de {disco}' if disco and extra else (disco or extra or tabla)
    return f'#{f.get("id")}'


def _diff(antes, despues):
    """Campos que cambiaron: [(nombre legible, antes, despues)]"""
    out = []
    for k in despues:
        if k in OCULTOS:
            continue
        a, d = antes.get(k), despues.get(k)
        if a == d or (a in (None, '') and d in (None, '')):
            continue
        if k in LARGOS:
            out.append((CAMPOS.get(k, k), 'cambió', _fmt(k, d)))
        else:
            out.append((CAMPOS.get(k, k), _fmt(k, a), _fmt(k, d)))
    return out


def describir(changes, discos=None):
    """Arma la descripcion de UNA operacion (todos los cambios de un mismo batch)."""
    discos = discos if discos is not None else {}
    # Combinar varios cambios del mismo registro (primer antes, ultimo despues)
    combinados = {}
    for ch in changes:
        key = (ch.tabla, ch.registro_id)
        a = json.loads(ch.antes) if ch.antes else None
        d = json.loads(ch.despues) if ch.despues else None
        if key not in combinados:
            combinados[key] = [a, d]
        else:
            combinados[key][1] = d
    items = []
    secundarios = 0
    for (tabla, rid), (a, d) in combinados.items():
        if tabla in SECUNDARIAS:
            secundarios += 1
            continue
        if a is None and d is None:
            continue
        accion = 'alta' if a is None else ('baja' if d is None else 'cambio')
        campos = _diff(a, d) if accion == 'cambio' else []
        if accion == 'cambio' and not campos:
            secundarios += 1   # solo cambiaron campos tecnicos
            continue
        tipo, tipo_label = TIPOS.get(tabla, ('sistema', tabla))
        items.append({'tabla': tabla, 'tipo': tipo, 'tipo_label': tipo_label, 'accion': accion,
                      'nombre': _nombre(tabla, d or a, discos), 'campos': campos,
                      'registro_id': rid})
    items.sort(key=lambda it: PRIORIDAD.index(it['tabla']) if it['tabla'] in PRIORIDAD else 99)

    first = changes[0]
    origen = ORIGENES.get(first.accion) or first.accion.replace('_', ' ').capitalize()
    automatica = first.accion in AUTOMATICAS
    if items:
        p = items[0]
        verbo = {'alta': 'Se creó', 'baja': 'Se borró', 'cambio': 'Se modificó'}[p['accion']]
        titulo = f'{verbo}: {p["tipo_label"].lower()} «{p["nombre"]}»'
        tipo = p['tipo']
        accion = p['accion']
    else:
        titulo = origen
        tipo, accion = 'sistema', 'cambio'
    actor = first.actor
    actor = {'sistema/web': 'La web', 'cliente': 'Un cliente', 'sistema': 'El sistema'}.get(actor, actor)
    return {'titulo': titulo, 'origen': origen, 'automatica': automatica, 'items': items,
            'secundarios': secundarios, 'tipo': tipo, 'accion': accion, 'actor': actor,
            'motivo': first.motivo, 'fecha_uy': first.fecha - timedelta(hours=3)}


def nombres_de_discos(changes):
    """{disc_id: 'Artista — Titulo'} para nombrar tracks/fotos/estados por su disco."""
    from models import Disc
    ids = set()
    for ch in changes:
        for raw in (ch.antes, ch.despues):
            if raw:
                try:
                    v = json.loads(raw).get('disc_id')
                except Exception:
                    v = None
                if v:
                    ids.add(v)
            if ch.tabla == 'discs':
                ids.discard(None)
    out = {}
    if ids:
        for d in Disc.query.filter(Disc.id.in_(ids)).all():
            out[d.id] = ' — '.join(x for x in (d.artista, d.titulo) if x)
    # Discos borrados: sacar el nombre de su foto en el mismo historial
    for ch in changes:
        if ch.tabla == 'discs' and ch.registro_id in ids and ch.registro_id not in out:
            raw = ch.antes or ch.despues
            if raw:
                f = json.loads(raw)
                out[ch.registro_id] = ' — '.join(x for x in (f.get('artista'), f.get('titulo')) if x)
    return out
