"""Arcano Stock - Aplicacion Flask principal."""
import csv
import io
import os
from datetime import datetime, timedelta

from flask import (
    Flask,
    render_template,
    jsonify,
    request,
    redirect,
    url_for,
    flash,
    session,
    Response,
)
from sqlalchemy import or_

import config
from models import (
    db,
    init_db,
    Disc,
    DiscImage,
    Cliente,
    Setting,
    StatusHistory,
    Reserva,
    ReservaItem,
    Track,
    parse_tracklist_text,
    ESTADOS_DISCO,
    ESTADOS_RESERVA,
    CONDICIONES_DISCO,
)
import vision
import scraper
import metrics
import auth
import mailer
import mp


# Registro en memoria de intentos de login fallidos por IP (rate limiting).
# Se reinicia con cada deploy; suficiente para frenar fuerza bruta.
_login_attempts: dict = {}
_cliente_login_attempts: dict = {}  # idem para el login de clientes de la tienda


def convertir_cover_a_webp(path_original: str) -> str:
    """Convierte una imagen recien guardada a WebP (calidad 85).

    Devuelve el nombre del archivo .webp resultante. Si algo falla,
    devuelve el nombre original (la imagen queda como estaba).
    Solo aplica a archivos NUEVOS: las covers existentes no se tocan.
    """
    import os as _os
    try:
        from PIL import Image
        base, ext = _os.path.splitext(path_original)
        if ext.lower() == ".webp":
            return _os.path.basename(path_original)
        img = Image.open(path_original)
        # WebP no soporta todos los modos; normalizar
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA") if "A" in img.mode or img.mode == "P" else img.convert("RGB")
            if img.mode == "RGBA":
                # aplanar transparencia sobre negro (covers de discos)
                from PIL import Image as _Img
                fondo = _Img.new("RGB", img.size, (10, 10, 10))
                fondo.paste(img, mask=img.split()[-1])
                img = fondo
        elif img.mode != "RGB":
            img = img.convert("RGB")
        webp_path = base + ".webp"
        img.save(webp_path, "WEBP", quality=85, method=4)
        _os.remove(path_original)
        return _os.path.basename(webp_path)
    except Exception:
        return _os.path.basename(path_original)


def create_app() -> Flask:
    app = Flask(__name__)

    # Ruta de la base de datos: configurable para produccion (disco persistente).
    # En local usa instance/arcano_stock.db como siempre.
    db_path = os.environ.get("DATABASE_PATH", "").strip()
    if db_path:
        app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    else:
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///arcano_stock.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # SECRET_KEY: en produccion viene de entorno (secreta). En local usa la de dev.
    _DEV_SECRET_KEY = "arcano-local-dev-key-change-me"
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", _DEV_SECRET_KEY)
    # En produccion (Render) es preferible FALLAR FUERTE a correr inseguro por un
    # olvido de configuracion: sin SECRET_KEY propia se podrian falsificar sesiones,
    # y sin credenciales de admin el panel quedaria abierto.
    if os.environ.get("RENDER"):
        if app.config["SECRET_KEY"] == _DEV_SECRET_KEY:
            raise RuntimeError(
                "SECRET_KEY no configurada en produccion. Cargala como variable de entorno en Render."
            )
        if not auth.auth_enabled():
            raise RuntimeError(
                "ADMIN_USER/ADMIN_PASS no configurados en produccion: el admin quedaria abierto."
            )
    app.config["MAX_CONTENT_LENGTH"] = 35 * 1024 * 1024  # 35 MB (permite samples de video)

    # --- Seguridad de cookies de sesion ---
    # HTTPONLY: el JS no puede leer la cookie (previene robo por XSS).
    # SAMESITE Lax: la cookie no viaja en requests cross-site (mitiga CSRF).
    # SECURE solo en produccion (Render tiene HTTPS; en local http fallaria).
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    if os.environ.get("RENDER"):
        app.config["SESSION_COOKIE_SECURE"] = True

    # --- Compresion gzip (reduce ~80% el peso de HTML/CSS/JS) ---
    try:
        from flask_compress import Compress
        Compress(app)
    except ImportError:
        pass  # en local sin la lib instalada, la app funciona igual

    # --- Headers de seguridad en todas las respuestas ---
    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        # HSTS: obliga al navegador a usar SIEMPRE HTTPS por un ano. Solo en
        # produccion (Render tiene HTTPS); en local http se omite para no romper.
        if os.environ.get("RENDER"):
            resp.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        # El HTML no se cachea: asi los cambios de la web llegan siempre en la
        # proxima carga (evita el clasico "no me aparecen los cambios").
        ctype = resp.headers.get("Content-Type", "")
        if ctype.startswith("text/html"):
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp

    # --- Cache-busting de estaticos ---
    # Agrega ?v=<fecha de modificacion> a cada CSS/JS/imagen servido por
    # url_for('static', ...). Cuando cambia un archivo su URL cambia, y el
    # navegador baja la version nueva si o si (mata el cache viejo del navegador).
    @app.url_defaults
    def _static_cache_bust(endpoint, values):
        if endpoint == "static" and "filename" in values:
            try:
                fpath = os.path.join(app.static_folder, values["filename"])
                values["v"] = int(os.stat(fpath).st_mtime)
            except OSError:
                pass

    # --- Proteccion CSRF liviana para el admin: verificar el origen de los POST ---
    # Si un POST a /admin viene desde otro sitio (Origin/Referer ajeno), se rechaza.
    # No requiere tokens. Cubre admin Y tienda publica (reservas, checkout, cuenta);
    # excluye el webhook de Mercado Pago, que lo llama MP desde otro servidor.
    @app.before_request
    def csrf_origin_check():
        if request.method in ("POST", "PUT", "DELETE") and not request.path.startswith("/mp/webhook"):
            origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
            if origin:
                from urllib.parse import urlparse
                origin_host = urlparse(origin).netloc.split(":")[0]
                request_host = (request.host or "").split(":")[0]
                if origin_host and request_host and origin_host != request_host:
                    return "Origen invalido", 403

    # Carpeta de portadas. En produccion (Render) se define COVERS_PATH apuntando
    # al disco persistente; en local usa static/covers como siempre.
    covers_path = os.environ.get("COVERS_PATH", "").strip()
    if covers_path:
        os.makedirs(covers_path, exist_ok=True)
        app.config["COVERS_DIR"] = covers_path
    else:
        app.config["COVERS_DIR"] = os.path.join(app.static_folder, "covers")

    # Ruta que sirve las portadas desde COVERS_DIR (para que funcionen tanto en
    # local como cuando viven en el disco persistente fuera de /static).
    from flask import send_from_directory

    @app.route("/covers/<path:filename>")
    def serve_cover(filename):
        return send_from_directory(app.config["COVERS_DIR"], filename)

    # Carpeta de samples de audio/video subidos a mano. En produccion (Render)
    # se define SAMPLES_PATH apuntando al disco persistente; en local usa
    # static/samples. Sin esto, los archivos subidos se borrarian en cada deploy.
    samples_path = os.environ.get("SAMPLES_PATH", "").strip()
    if samples_path:
        os.makedirs(samples_path, exist_ok=True)
        app.config["SAMPLES_DIR"] = samples_path
    else:
        app.config["SAMPLES_DIR"] = os.path.join(app.static_folder, "samples")

    @app.route("/sample/<path:filename>")
    def serve_sample(filename):
        # Los nuevos uploads viven en SAMPLES_DIR (disco persistente); los viejos
        # que hayan quedado en static/samples se sirven como fallback.
        d = app.config["SAMPLES_DIR"]
        if os.path.exists(os.path.join(d, filename)):
            return send_from_directory(d, filename)
        return send_from_directory(os.path.join(app.static_folder, "samples"), filename)

    # Carpeta de comprobantes de transferencia (PRIVADOS). En produccion caen al
    # disco persistente (junto a covers/samples) de forma automatica; en local a
    # static/comprobantes. Se sirven SOLO al admin (son datos financieros).
    comprobantes_path = os.environ.get("COMPROBANTES_PATH", "").strip()
    if not comprobantes_path and covers_path:
        comprobantes_path = os.path.join(os.path.dirname(covers_path.rstrip("/\\")), "comprobantes")
    if comprobantes_path:
        os.makedirs(comprobantes_path, exist_ok=True)
        app.config["COMPROBANTES_DIR"] = comprobantes_path
    else:
        app.config["COMPROBANTES_DIR"] = os.path.join(app.static_folder, "comprobantes")

    @app.route("/admin/comprobante/<path:filename>")
    @auth.admin_required
    def serve_comprobante(filename):
        return send_from_directory(app.config["COMPROBANTES_DIR"], filename)

    init_db(app)
    register_routes(app)

    # Context processor: vars disponibles en todos los templates
    @app.context_processor
    def inject_globals():
        try:
            reservas_pendientes_count = Reserva.query.filter_by(estado="Pendiente").count()
        except Exception:
            reservas_pendientes_count = 0

        def url_pagina(nro):
            """Arma la URL de la pagina `nro` conservando los filtros actuales."""
            args = request.args.to_dict(flat=False)
            args["pagina"] = [str(nro)]
            return url_for(request.endpoint, **{k: v for k, v in args.items()})

        def sort_url(campo, direccion):
            """Arma la URL para ordenar por `campo` conservando los filtros."""
            args = request.args.to_dict(flat=False)
            args["order"] = [campo]
            args["dir"] = [direccion]
            args.pop("pagina", None)  # volver a la primera pagina al reordenar
            return url_for(request.endpoint, **{k: v for k, v in args.items()})

        def _todos_los_pedidos():
            """Lista de pedidos para el selector del form del disco."""
            try:
                from models import Pedido
                return Pedido.query.order_by(Pedido.fecha.desc()).all()
            except Exception:
                return []

        # Cliente logueado (para el nav de la tienda)
        cliente_actual = None
        try:
            cid = auth.cliente_actual_id()
            if cid:
                cliente_actual = Cliente.query.get(cid)
        except Exception:
            cliente_actual = None

        return {
            "reservas_pendientes_count": reservas_pendientes_count,
            "now": datetime.utcnow(),
            "url_pagina": url_pagina,
            "sort_url": sort_url,
            "cliente_actual": cliente_actual,
            "todos_los_pedidos": _todos_los_pedidos(),
        }

    return app


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

ORDER_FIELDS = {
    "orden": Disc.orden,
    "fecha_ingreso": Disc.fecha_ingreso,
    "artista": Disc.artista,
    "sello": Disc.sello,
    "formato": Disc.formato,
    "costo_eur": Disc.costo_eur,
    "envio_eur": Disc.envio_eur,
    "costo_final_uyu": Disc.costo_final_uyu,
    "precio_uyu": Disc.precio_uyu,
    "estado": Disc.estado,
    "origen": Disc.origen,
    "stock": Disc.stock,
}


def query_discs(args):
    q = Disc.query

    search = (args.get("q") or "").strip()
    if search:
        like = f"%{search}%"
        q = q.filter(
            or_(
                Disc.artista.ilike(like),
                Disc.titulo.ilike(like),
                Disc.sello.ilike(like),
            )
        )

    estado = (args.get("estado") or "").strip()
    if estado and estado != "todos":
        q = q.filter(Disc.estado == estado)

    sello = (args.get("sello") or "").strip()
    if sello and sello != "todos":
        q = q.filter(Disc.sello == sello)

    origen = (args.get("origen") or "").strip()
    if origen and origen != "todos":
        q = q.filter(Disc.origen == origen)

    # Filtro por fechas (rango). tipo_fecha: 'ingreso' (compra) o 'venta'.
    from datetime import datetime as _dt, timedelta as _td
    tipo_fecha = (args.get("tipo_fecha") or "ingreso").strip()
    campo_fecha = Disc.fecha_venta if tipo_fecha == "venta" else Disc.fecha_ingreso
    fecha_desde = (args.get("fecha_desde") or "").strip()
    fecha_hasta = (args.get("fecha_hasta") or "").strip()
    if fecha_desde:
        try:
            q = q.filter(campo_fecha >= _dt.strptime(fecha_desde, "%Y-%m-%d"))
        except ValueError:
            pass
    if fecha_hasta:
        try:
            # incluir todo el dia 'hasta' (hasta las 23:59)
            hasta_dt = _dt.strptime(fecha_hasta, "%Y-%m-%d") + _td(days=1)
            q = q.filter(campo_fecha < hasta_dt)
        except ValueError:
            pass

    order_by = (args.get("order") or "orden").strip()
    direction = (args.get("dir") or "desc").strip()

    # Campos calculados (propiedades, no columnas): se ordenan en Python
    if order_by in ("ganancia_uyu", "margen_pct"):
        discs = q.all()
        reverse = direction != "asc"
        if order_by == "ganancia_uyu":
            discs.sort(key=lambda d: d.ganancia_uyu or 0, reverse=reverse)
        else:
            discs.sort(key=lambda d: d.margen_pct or 0, reverse=reverse)
        return discs

    col = ORDER_FIELDS.get(order_by, Disc.fecha_ingreso)
    q = q.order_by(col.asc()) if direction == "asc" else q.order_by(col.desc())
    return q


def compute_totals(discs):
    total = len(discs)
    suma_costo_eur = 0.0
    suma_envio_eur = 0.0
    suma_costo_final_uyu = 0.0
    suma_precio_uyu = 0.0
    ganancia_potencial_uyu = 0.0
    ganancia_realizada_uyu = 0.0
    stock_total = 0

    suma_ganancia_uyu = 0.0
    for d in discs:
        suma_costo_eur += (d.costo_eur or 0) * (d.stock or 1)
        suma_envio_eur += (d.envio_eur or 0) * (d.stock or 1)
        suma_costo_final_uyu += (d.costo_final_uyu or 0) * (d.stock or 1)
        suma_precio_uyu += (d.precio_uyu or 0) * (d.stock or 1)
        suma_ganancia_uyu += (d.ganancia_uyu or 0) * (d.stock or 1)
        stock_total += d.stock or 0
        if d.estado == "Disponible":
            ganancia_potencial_uyu += (d.ganancia_uyu or 0) * (d.stock or 1)
        elif d.estado == "Vendido":
            ganancia_realizada_uyu += (d.ganancia_uyu or 0) * (d.stock or 1)

    return {
        "total": total,
        "stock_total": stock_total,
        "suma_costo_eur": round(suma_costo_eur, 2),
        "suma_envio_eur": round(suma_envio_eur, 2),
        "suma_costo_final_uyu": round(suma_costo_final_uyu, 2),
        "suma_precio_uyu": round(suma_precio_uyu, 2),
        "suma_ganancia_uyu": round(suma_ganancia_uyu, 2),
        "ganancia_potencial_uyu": round(ganancia_potencial_uyu, 2),
        "ganancia_realizada_uyu": round(ganancia_realizada_uyu, 2),
    }


def get_sellos_unicos():
    rows = (
        db.session.query(Disc.sello)
        .filter(Disc.sello.isnot(None), Disc.sello != "")
        .distinct()
        .order_by(Disc.sello.asc())
        .all()
    )
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Form parsing
# ---------------------------------------------------------------------------

def _parse_float(value, default=0.0):
    if value is None:
        return default
    s = str(value).strip().replace(",", ".")
    if not s:
        return default
    try:
        return float(s)
    except ValueError:
        return default


def _parse_int(value, default=None):
    if value is None:
        return default
    s = str(value).strip()
    if not s:
        return default
    try:
        return int(float(s))
    except ValueError:
        return default


def _str_or_none(value):
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def populate_disc_from_form(disc: Disc, form) -> list[str]:
    errores = []

    artista = _str_or_none(form.get("artista"))
    titulo = _str_or_none(form.get("titulo"))
    if not artista:
        errores.append("El campo Artista es obligatorio.")
    if not titulo:
        errores.append("El campo Titulo es obligatorio.")

    stock = _parse_int(form.get("stock"), default=1) or 1
    if stock < 1:
        errores.append("El stock debe ser mayor o igual a 1.")

    estado = _str_or_none(form.get("estado")) or "Disponible"
    if estado not in ESTADOS_DISCO:
        errores.append(f"Estado invalido: {estado}.")

    condicion = _str_or_none(form.get("condicion"))
    if condicion and condicion not in CONDICIONES_DISCO:
        errores.append(f"Condicion invalida: {condicion}.")

    if errores:
        return errores

    disc.artista = artista
    disc.titulo = titulo
    disc.sello = _str_or_none(form.get("sello"))
    disc.anio = _parse_int(form.get("anio"))
    disc.formato = _str_or_none(form.get("formato"))
    disc.pais = _str_or_none(form.get("pais"))
    disc.genero = _str_or_none(form.get("genero"))
    disc.tracklist = _str_or_none(form.get("tracklist"))

    disc.cover_url = _str_or_none(form.get("cover_url"))
    disc.cover_local = _str_or_none(form.get("cover_local"))
    disc.sample_url = _str_or_none(form.get("sample_url"))
    disc.source_url = _str_or_none(form.get("source_url"))

    disc.costo_eur = _parse_float(form.get("costo_eur"), default=0.0)
    disc.envio_eur = _parse_float(form.get("envio_eur"), default=0.0)
    disc.precio_uyu = _parse_float(form.get("precio_uyu"), default=0.0)
    # Precio de oferta (opcional). Vacio o 0 = sin oferta.
    _po = _parse_float(form.get("precio_oferta"), default=0.0)
    disc.precio_oferta = _po if _po and _po > 0 else None

    if not disc.tc_usado:
        disc.tc_usado = Setting.get_tc()
    disc.recalcular_costo_final()

    disc.stock = stock
    disc.estado = estado
    disc.condicion = condicion
    # Copias vendidas (para facturacion)
    try:
        cv = int(form.get("copias_vendidas") or 0)
        disc.copias_vendidas = max(0, cv)
    except (ValueError, TypeError):
        disc.copias_vendidas = disc.copias_vendidas or 0
    # Pedido al que pertenece
    pedido_id_raw = form.get("pedido_id") or ""
    disc.pedido_id = int(pedido_id_raw) if pedido_id_raw.isdigit() else None
    origen = _str_or_none(form.get("origen")) or "arcano"
    if origen in ("arcano", "personal"):
        disc.origen = origen
    disc.notas = _str_or_none(form.get("notas"))
    disc.descripcion_publica = _str_or_none(form.get("descripcion_publica"))
    disc.tags = _str_or_none(form.get("tags"))
    disc.destacado = form.get("destacado") == "1"
    disc.en_nuevos = form.get("en_nuevos") == "1"
    disc.es_tip = form.get("es_tip") == "1"
    disc.resena_corta = _str_or_none(form.get("resena_corta"))
    disc.cartel_ultima = form.get("cartel_ultima") == "1"
    disc.mostrar_agotado = form.get("mostrar_agotado") == "1"
    disc.es_preorder = form.get("es_preorder") == "1"
    cn = (form.get("cartel_nuevo") or "auto").strip()
    if cn in ("auto", "si", "no"):
        disc.cartel_nuevo = cn

    # Fechas editables (formato YYYY-MM-DD desde el input type=date).
    # Solo se tocan si el form las trae (modo edicion); vacio = borrar la fecha.
    from datetime import datetime as _dt
    if "fecha_ingreso" in form:
        fi = (form.get("fecha_ingreso") or "").strip()
        if fi:
            try:
                disc.fecha_ingreso = _dt.strptime(fi, "%Y-%m-%d")
            except ValueError:
                errores.append("Fecha de compra invalida.")
    if "fecha_venta" in form:
        fv = (form.get("fecha_venta") or "").strip()
        if fv:
            try:
                disc.fecha_venta = _dt.strptime(fv, "%Y-%m-%d")
            except ValueError:
                errores.append("Fecha de venta invalida.")
        else:
            disc.fecha_venta = None

    return errores


def crear_tracks_desde_json(disc: Disc, tracks_json: str | None) -> int:
    """Crea tracks estructurados desde el JSON del editor de alta.

    tracks_json es una lista de {lado, titulo, artista, youtube_url}. Se usa
    cuando en la pantalla de alta ya se buscaron los audios. Devuelve cuantos creo.
    """
    if not tracks_json or disc.tracks:
        return 0
    import json
    try:
        items = json.loads(tracks_json)
    except (ValueError, TypeError):
        return 0
    if not isinstance(items, list) or not items:
        return 0
    creados = 0
    for pos, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        titulo = (item.get("titulo") or "").strip()
        if not titulo:
            continue
        db.session.add(Track(
            disc_id=disc.id,
            posicion=pos,
            lado=(item.get("lado") or None),
            titulo=titulo[:300],
            artista=(item.get("artista") or None),
            youtube_url=(item.get("youtube_url") or None),
        ))
        creados += 1
    return creados


def crear_tracks_desde_tracklist(disc: Disc) -> int:
    """Convierte el tracklist en texto del disco a tracks estructurados.

    Solo actua si el disco tiene tracklist en texto y todavia NO tiene tracks
    estructurados (para no pisar los que ya existan). Devuelve la cantidad creada.

    Se usa al crear un disco (manual, por scrape o por vision) para que al entrar
    a editar ya esten los tracks listos para buscarles el sample.
    """
    if not disc.tracklist:
        return 0
    if disc.tracks:  # ya tiene tracks estructurados
        return 0
    items = parse_tracklist_text(disc.tracklist)
    if not items:
        return 0
    for item in items:
        db.session.add(Track(
            disc_id=disc.id,
            posicion=item["posicion"],
            lado=item["lado"],
            titulo=item["titulo"],
            artista=item["artista"],
        ))
    return len(items)


def register_status_change(disc: Disc, nuevo_estado: str, nota: str | None = None):
    if nuevo_estado not in ESTADOS_DISCO:
        raise ValueError(f"Estado invalido: {nuevo_estado}")

    ahora = datetime.utcnow()
    disc.estado = nuevo_estado

    if nuevo_estado == "Reservado" and not disc.fecha_reserva:
        disc.fecha_reserva = ahora
    elif nuevo_estado == "Vendido" and not disc.fecha_venta:
        disc.fecha_venta = ahora
        # Si no se registraron copias vendidas, asumir al menos 1
        if not getattr(disc, "copias_vendidas", 0):
            disc.copias_vendidas = 1

    evento = StatusHistory(
        disc_id=disc.id,
        estado=nuevo_estado,
        nota=nota,
        fecha=ahora,
    )
    db.session.add(evento)


# ---------------------------------------------------------------------------
# API cost tracking helpers
# ---------------------------------------------------------------------------

def _track_api_cost(cost_usd: float) -> None:
    """Acumula gasto en API en settings (clave: api_cost_total_usd, api_calls_count)."""
    total_str = Setting.get("api_cost_total_usd", "0")
    count_str = Setting.get("api_calls_count", "0")
    try:
        total = float(total_str) + cost_usd
    except (TypeError, ValueError):
        total = cost_usd
    try:
        count = int(count_str) + 1
    except (TypeError, ValueError):
        count = 1
    Setting.set("api_cost_total_usd", f"{total:.6f}")
    Setting.set("api_calls_count", str(count))


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------

def register_routes(app: Flask) -> None:

    @app.errorhandler(404)
    def page_not_found(e):
        # Si la URL empieza con /admin, devolver el 404 plano de Flask
        if request.path.startswith("/admin"):
            return "404 not found", 404
        # Para rutas publicas, mostrar la 404 pintada
        ctx = {
            "tagline": Setting.get("public_tagline", ""),
            "about": Setting.get("public_about", ""),
            "envios": Setting.get("public_envios", ""),
            "instagram": Setting.get("public_instagram", ""),
            "whatsapp": Setting.get("public_whatsapp", ""),
        }
        return render_template("public/404.html", **ctx), 404

    @app.route("/admin/")
    @auth.admin_required
    def dashboard():
        _res = query_discs(request.args)
        discs = _res if isinstance(_res, list) else _res.all()
        totals = compute_totals(discs)  # totales sobre TODO lo filtrado
        sellos = get_sellos_unicos()
        tc = Setting.get_tc()

        # --- Paginacion: la tabla renderiza de a 50 (configurable) ---
        # Los totales siguen siendo del conjunto completo filtrado.
        try:
            por_pagina = int(request.args.get("por_pagina", 50))
        except (ValueError, TypeError):
            por_pagina = 50
        if por_pagina not in (25, 50, 100, 0):  # 0 = todos
            por_pagina = 50
        try:
            pagina_actual = max(1, int(request.args.get("pagina", 1)))
        except (ValueError, TypeError):
            pagina_actual = 1
        total_discos_filtrados = len(discs)
        if por_pagina > 0:
            import math as _math
            total_paginas = max(1, _math.ceil(total_discos_filtrados / por_pagina))
            pagina_actual = min(pagina_actual, total_paginas)
            inicio_idx = (pagina_actual - 1) * por_pagina
            discs = discs[inicio_idx:inicio_idx + por_pagina]
        else:
            total_paginas = 1
            pagina_actual = 1

        filtros = {
            "q": request.args.get("q", ""),
            "estado": request.args.get("estado", "todos"),
            "sello": request.args.get("sello", "todos"),
            "origen": request.args.get("origen", "todos"),
            "order": request.args.get("order", "orden"),
            "dir": request.args.get("dir", "desc"),
            "tipo_fecha": request.args.get("tipo_fecha", "ingreso"),
            "fecha_desde": request.args.get("fecha_desde", ""),
            "fecha_hasta": request.args.get("fecha_hasta", ""),
        }

        return render_template(
            "dashboard.html",
            discs=discs,
            totals=totals,
            sellos=sellos,
            estados=ESTADOS_DISCO,
            filtros=filtros,
            tc=tc,
            pagina_actual=pagina_actual,
            total_paginas=total_paginas,
            por_pagina=por_pagina,
            total_discos_filtrados=total_discos_filtrados,
        )

    @app.route("/admin/discs/reorder", methods=["POST"])
    @auth.admin_required
    def discs_reorder():
        """Recibe una lista de IDs en el orden nuevo (de arriba hacia abajo) y
        reasigna el campo `orden` DENTRO del conjunto mostrado: los valores de
        orden que ya tenian esos discos se reparten segun el nuevo orden. Asi,
        reordenar dentro de una pagina no altera el resto del catalogo."""
        ids_raw = request.form.get("ids", "") or (request.get_json(silent=True) or {}).get("ids", "")
        if isinstance(ids_raw, list):
            ids = [int(x) for x in ids_raw if str(x).isdigit()]
        else:
            ids = [int(x) for x in str(ids_raw).split(",") if x.strip().isdigit()]
        if not ids:
            return jsonify({"ok": False, "error": "Sin ids"}), 400

        discs = Disc.query.filter(Disc.id.in_(ids)).all()
        by_id = {d.id: d for d in discs}
        ids_validos = [i for i in ids if i in by_id]
        if not ids_validos:
            return jsonify({"ok": False, "error": "IDs invalidos"}), 400

        # Valores de orden actuales del conjunto, de mayor a menor (arriba = mayor)
        valores = sorted(
            [(by_id[i].orden if by_id[i].orden is not None else 0) for i in ids_validos],
            reverse=True,
        )
        for pos, did in enumerate(ids_validos):
            by_id[did].orden = valores[pos]
        db.session.commit()
        return jsonify({"ok": True, "count": len(ids_validos)})

    @app.route("/admin/discs/new", methods=["GET", "POST"])
    @auth.admin_required
    def disc_new():
        tc = Setting.get_tc()

        # Prellenado desde extraccion via vision (se guarda en session
        # con clave 'vision_prefill' al volver de /discs/new/from-image).
        prefill = session.pop("vision_prefill", None)
        prefill_info = session.pop("vision_prefill_info", None)

        if request.method == "POST":
            disc = Disc(estado="Disponible", stock=1)
            errores = populate_disc_from_form(disc, request.form)
            if errores:
                for e in errores:
                    flash(e, "error")
                return render_template(
                    "disc_form.html",
                    mode="new",
                    disc=disc,
                    form_data=request.form,
                    estados=ESTADOS_DISCO,
                    condiciones=CONDICIONES_DISCO,
                    tc=tc,
                    prefill_info=None,
                )

            db.session.add(disc)
            db.session.flush()
            evento = StatusHistory(
                disc_id=disc.id,
                estado=disc.estado,
                nota="Ingreso al stock",
            )
            db.session.add(evento)
            # Convertir el tracklist en texto a tracks estructurados
            # Tracks: si el editor de alta mando tracks_json (con audios ya
            # buscados), usarlos; sino, convertir el tracklist en texto.
            n_tracks = crear_tracks_desde_json(disc, request.form.get("tracks_json"))
            if n_tracks == 0:
                n_tracks = crear_tracks_desde_tracklist(disc)
            db.session.commit()

            # Si venimos de una cola de URLs, avanzar el contador
            cola = session.get("url_queue")
            if cola is not None:
                session["url_queue_done"] = session.get("url_queue_done", 0) + 1

            if n_tracks:
                flash(f"Disco agregado con {n_tracks} track(s). Busca los samples abajo.", "success")
            else:
                flash(f"Disco agregado: {disc.artista} - {disc.titulo}", "success")
            return redirect(url_for("disc_edit", disc_id=disc.id) + "#tracks-section")

        # GET: si hay prefill, usarlo
        if prefill:
            disc = Disc(
                estado="Disponible",
                stock=1,
                artista=prefill.get("artista"),
                titulo=prefill.get("titulo"),
                sello=prefill.get("sello"),
                anio=prefill.get("anio"),
                formato=prefill.get("formato"),
                pais=prefill.get("pais"),
                genero=prefill.get("genero"),
                tracklist=prefill.get("tracklist"),
                cover_url=prefill.get("cover_url"),
                cover_local=prefill.get("cover_local"),
                sample_url=prefill.get("sample_url"),
                source_url=prefill.get("source_url"),
                costo_eur=prefill.get("costo_eur") or 0.0,
            )
            return render_template(
                "disc_form.html",
                mode="new",
                disc=disc,
                form_data={},
                estados=ESTADOS_DISCO,
                condiciones=CONDICIONES_DISCO,
                tc=tc,
                prefill_info=prefill_info,
                **_url_queue_ctx(),
            )

        return render_template(
            "disc_form.html",
            mode="new",
            disc=Disc(estado="Disponible", stock=1),
            form_data={},
            estados=ESTADOS_DISCO,
            condiciones=CONDICIONES_DISCO,
            tc=tc,
            prefill_info=None,
            **_url_queue_ctx(),
        )

    @app.route("/admin/discs/new/from-image", methods=["GET", "POST"])
    @auth.admin_required
    def disc_new_from_image():
        """Pantalla de subida de imagen + extraccion via Claude vision."""
        api_key_set = bool(config.ANTHROPIC_API_KEY)

        if request.method == "POST":
            if not api_key_set:
                flash(
                    "La API key de Anthropic no esta configurada. "
                    "Editar .env y agregar ANTHROPIC_API_KEY.",
                    "error",
                )
                return redirect(url_for("disc_new_from_image"))

            file = request.files.get("screenshot")
            if not file or not file.filename:
                flash("No se selecciono ninguna imagen.", "error")
                return redirect(url_for("disc_new_from_image"))

            media_type = (file.mimetype or "").lower()
            image_bytes = file.read()

            result = vision.extract_disc_from_image(image_bytes, media_type)

            # Acumular gasto aunque haya habido error parcial
            if result.cost_usd > 0:
                _track_api_cost(result.cost_usd)
                db.session.commit()

            if not result.ok:
                flash(f"Error en la extraccion: {result.error}", "error")
                return redirect(url_for("disc_new_from_image"))

            # Guardar en session para que /discs/new (GET) lo prelle
            session["vision_prefill"] = result.data
            session["vision_prefill_info"] = {
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cost_usd": result.cost_usd,
                "confianza": result.data.get("confianza"),
                "notas_extraccion": result.data.get("notas_extraccion"),
            }
            flash(
                f"Datos extraidos. Costo: ${result.cost_usd:.4f} USD "
                f"({result.input_tokens + result.output_tokens} tokens). "
                "Revisar y completar antes de guardar.",
                "success",
            )
            return redirect(url_for("disc_new"))

        return render_template(
            "disc_from_image.html",
            tc=Setting.get_tc(),
            api_key_set=api_key_set,
        )

    @app.route("/admin/discs/new/from-url", methods=["GET", "POST"])
    @auth.admin_required
    def disc_new_from_url():
        """Pantalla de pegado de URL(s) + extraccion via scraping (Deejay.de).

        Acepta varias URLs (una por linea). Las guarda en una cola en sesion y
        procesa la primera; al guardar cada disco, se sigue con la siguiente.
        """
        if request.method == "POST":
            urls_raw = (request.form.get("urls") or request.form.get("url") or "").strip()
            # Separar por lineas, limpiar vacios y duplicados conservando orden
            urls = []
            for linea in urls_raw.splitlines():
                u = linea.strip()
                if u and u not in urls:
                    urls.append(u)

            if not urls:
                flash("Pega al menos una URL antes de continuar.", "error")
                return redirect(url_for("disc_new_from_url"))

            # Guardar la cola: la primera se procesa ahora, el resto queda pendiente
            session["url_queue"] = urls[1:]
            session["url_queue_total"] = len(urls)
            session["url_queue_done"] = 0
            return _procesar_url_scrape(urls[0])

        return render_template(
            "disc_from_url.html",
            failed_url=session.pop("scrape_failed_url", None),
        )

    def _procesar_url_scrape(url: str):
        """Scrapea una URL y redirige al alta prellenada. Helper de la cola."""
        result = scraper.scrape_url(url)

        if not result.ok:
            session["scrape_failed_url"] = url
            # Si venia de una cola, avisar y seguir con la siguiente si hay
            cola = session.get("url_queue") or []
            if cola:
                siguiente = cola.pop(0)
                session["url_queue"] = cola
                flash(f"Error scrapeando una URL ({result.error}). Sigo con la siguiente.", "error")
                return _procesar_url_scrape(siguiente)
            flash(f"Error en el scraping: {result.error}", "error")
            return redirect(url_for("disc_new_from_url"))

        # Intentar descargar la portada localmente
        cover_local = None
        if result.data.get("cover_url"):
            covers_dir = app.config["COVERS_DIR"]
            cover_local = scraper.download_cover(result.data["cover_url"], covers_dir)
            if cover_local:
                cover_local = convertir_cover_a_webp(os.path.join(covers_dir, cover_local))

        prefill = dict(result.data)
        if cover_local:
            prefill["cover_local"] = cover_local

        session["vision_prefill"] = prefill

        # Info de progreso de la cola (si hay)
        total = session.get("url_queue_total", 1)
        done = session.get("url_queue_done", 0)
        pendientes = len(session.get("url_queue") or [])
        nota_cola = ""
        if total > 1:
            nota_cola = f" (disco {done + 1} de {total})"

        session["vision_prefill_info"] = {
            "fuente": "scraping",
            "confianza": prefill.get("confianza", "media"),
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "notas_extraccion": (
                "Datos extraidos del HTML de la pagina (gratis, sin API). "
                "Revisar especialmente precio y tracklist." + nota_cola
            ),
        }

        msg = f"Datos extraidos via scraping (sin costo de API).{nota_cola} Revisar y completar antes de guardar."
        if total > 1 and pendientes > 0:
            msg += f" Al guardar, sigue el proximo ({pendientes} en cola)."
        flash(msg, "success")
        return redirect(url_for("disc_new"))

    def _url_queue_ctx():
        """Contexto de la cola de URLs para pasar a los templates de alta/edicion."""
        return {
            "url_queue_pendientes": len(session.get("url_queue") or []),
            "url_queue_total": session.get("url_queue_total", 0),
            "url_queue_done": session.get("url_queue_done", 0),
        }

    @app.route("/admin/discs/new/url-queue-next", methods=["POST"])
    @auth.admin_required
    def disc_url_queue_next():
        """Procesa el siguiente disco de la cola de URLs (o termina)."""
        cola = session.get("url_queue") or []
        if not cola:
            # Cola vacia: limpiar y volver al dashboard
            session.pop("url_queue", None)
            session.pop("url_queue_total", None)
            session.pop("url_queue_done", None)
            flash("Terminaste de cargar toda la tanda de URLs.", "success")
            return redirect(url_for("dashboard"))
        siguiente = cola.pop(0)
        session["url_queue"] = cola
        return _procesar_url_scrape(siguiente)

    @app.route("/admin/discs/new/url-queue-cancel", methods=["POST"])
    @auth.admin_required
    def disc_url_queue_cancel():
        """Cancela la cola de URLs pendientes."""
        session.pop("url_queue", None)
        session.pop("url_queue_total", None)
        session.pop("url_queue_done", None)
        flash("Cola de URLs cancelada.", "success")
        return redirect(url_for("dashboard"))

        # GET
        failed_url = session.pop("scrape_failed_url", None)
        return render_template(
            "disc_from_url.html",
            tc=Setting.get_tc(),
            failed_url=failed_url,
        )

    @app.route("/admin/discs/new/from-invoice", methods=["GET", "POST"])
    @auth.admin_required
    def disc_new_from_invoice():
        """Subida de factura multi-disco para extraccion via Claude vision."""
        api_key_set = bool(config.ANTHROPIC_API_KEY)

        if request.method == "POST":
            if not api_key_set:
                flash(
                    "La API key de Anthropic no esta configurada. "
                    "Editar .env y agregar ANTHROPIC_API_KEY.",
                    "error",
                )
                return redirect(url_for("disc_new_from_invoice"))

            file = request.files.get("invoice")
            if not file or not file.filename:
                flash("No se selecciono ninguna imagen.", "error")
                return redirect(url_for("disc_new_from_invoice"))

            media_type = (file.mimetype or "").lower()
            image_bytes = file.read()

            result = vision.extract_invoice_from_image(image_bytes, media_type)

            if result.cost_usd > 0:
                _track_api_cost(result.cost_usd)
                db.session.commit()

            if not result.ok:
                flash(f"Error en la extraccion: {result.error}", "error")
                return redirect(url_for("disc_new_from_invoice"))

            # Pre-calcular envio por disco (partes iguales)
            discos = result.data.get("discos", [])
            envio_total = result.data.get("envio_total_eur") or 0.0
            envio_por_disco = (envio_total / len(discos)) if discos else 0.0
            envio_por_disco = round(envio_por_disco, 2)

            covers_dir = app.config["COVERS_DIR"]

            # Detectar duplicados existentes + buscar portada/anio en Deejay.de
            items = []
            portadas_encontradas = 0
            for d in discos:
                artista = (d.get("artista") or "").strip()
                titulo = (d.get("titulo") or "").strip()
                existente = None
                if artista and titulo:
                    existente = Disc.query.filter(
                        db.func.lower(Disc.artista) == artista.lower(),
                        db.func.lower(Disc.titulo) == titulo.lower(),
                    ).first()

                # Buscar portada + anio en Deejay.de (al analizar, no al confirmar)
                cover_local_auto = None
                anio_auto = d.get("anio")
                if artista and titulo:
                    try:
                        found = scraper.find_cover_and_year(artista, titulo, covers_dir)
                        if found.get("cover_local"):
                            cover_local_auto = found["cover_local"]
                            portadas_encontradas += 1
                        if not anio_auto and found.get("anio"):
                            anio_auto = found["anio"]
                    except Exception:
                        pass

                items.append({
                    "artista": artista or None,
                    "titulo": titulo or None,
                    "sello": d.get("sello"),
                    "anio": anio_auto,
                    "formato": d.get("formato"),
                    "pais": d.get("pais"),
                    "costo_eur": d.get("costo_eur") or 0.0,
                    "envio_eur": envio_por_disco,
                    "duplicado_id": existente.id if existente else None,
                    "duplicado_stock_actual": existente.stock if existente else None,
                    "cover_local_manual": cover_local_auto,  # se muestra como preview en la card
                })

            # Guardar en session para la pantalla de revision
            session["invoice_items"] = items
            session["invoice_info"] = {
                "envio_total_eur": envio_total,
                "envio_por_disco_eur": envio_por_disco,
                "total_factura_eur": result.data.get("total_factura_eur"),
                "subtotal_discos_eur": result.data.get("subtotal_discos_eur"),
                "fuente": result.data.get("fuente_probable"),
                "confianza": result.data.get("confianza"),
                "notas_extraccion": result.data.get("notas_extraccion"),
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cost_usd": result.cost_usd,
                "portadas_encontradas": portadas_encontradas,
            }

            flash(
                f"Factura analizada: {len(items)} discos detectados, "
                f"{portadas_encontradas} portadas encontradas en Deejay.de. "
                f"Costo API: ${result.cost_usd:.4f} USD.",
                "success",
            )
            return redirect(url_for("disc_invoice_review"))

        return render_template(
            "disc_from_invoice.html",
            tc=Setting.get_tc(),
            api_key_set=api_key_set,
        )

    @app.route("/admin/discs/invoice-review", methods=["GET", "POST"])
    @auth.admin_required
    def disc_invoice_review():
        """Pantalla de revision/edicion antes de confirmar el alta en lote."""
        items = session.get("invoice_items")
        info = session.get("invoice_info")
        if not items or not info:
            flash("No hay factura en proceso. Subi una nueva.", "error")
            return redirect(url_for("disc_new_from_invoice"))

        if request.method == "POST":
            num = int(request.form.get("item_count", 0))
            creados = 0
            actualizados = 0
            errores_validacion = []

            for idx in range(num):
                prefix = f"item_{idx}_"
                if request.form.get(prefix + "skip") == "on":
                    continue

                artista = _str_or_none(request.form.get(prefix + "artista"))
                titulo = _str_or_none(request.form.get(prefix + "titulo"))
                if not artista or not titulo:
                    errores_validacion.append(f"Item #{idx+1}: falta artista o titulo")
                    continue

                sello = _str_or_none(request.form.get(prefix + "sello"))
                anio = _parse_int(request.form.get(prefix + "anio"))
                formato = _str_or_none(request.form.get(prefix + "formato"))
                pais = _str_or_none(request.form.get(prefix + "pais"))
                costo_eur = _parse_float(request.form.get(prefix + "costo_eur"), 0.0)
                envio_eur = _parse_float(request.form.get(prefix + "envio_eur"), 0.0)
                precio_uyu = _parse_float(request.form.get(prefix + "precio_uyu"), 0.0)
                estado = _str_or_none(request.form.get(prefix + "estado")) or "En camino"
                stock_nuevo = _parse_int(request.form.get(prefix + "stock"), 1) or 1
                duplicado_id = _parse_int(request.form.get(prefix + "duplicado_id"))
                # Portada (ya buscada al analizar, o subida manualmente en la revision)
                final_cover_local = _str_or_none(request.form.get(prefix + "cover_local"))

                if estado not in ESTADOS_DISCO:
                    estado = "En camino"

                tc_actual = Setting.get_tc()

                if duplicado_id:
                    existente = Disc.query.get(duplicado_id)
                    if not existente:
                        errores_validacion.append(
                            f"Item #{idx+1}: el disco duplicado #{duplicado_id} ya no existe"
                        )
                        continue

                    existente.stock = (existente.stock or 0) + stock_nuevo
                    existente.costo_eur = costo_eur
                    existente.envio_eur = envio_eur
                    existente.tc_usado = tc_actual
                    existente.recalcular_costo_final()
                    if final_cover_local and not existente.cover_local:
                        existente.cover_local = final_cover_local
                    if anio and not existente.anio:
                        existente.anio = anio

                    if estado and estado != existente.estado:
                        ahora = datetime.utcnow()
                        existente.estado = estado
                        evento = StatusHistory(
                            disc_id=existente.id,
                            estado=estado,
                            nota=f"Cambio por carga desde factura (+{stock_nuevo} stock)",
                            fecha=ahora,
                        )
                        db.session.add(evento)
                    else:
                        evento = StatusHistory(
                            disc_id=existente.id,
                            estado=existente.estado,
                            nota=f"+{stock_nuevo} unidad(es) desde factura",
                        )
                        db.session.add(evento)
                    actualizados += 1
                else:
                    disc = Disc(
                        artista=artista,
                        titulo=titulo,
                        sello=sello,
                        anio=anio,
                        formato=formato,
                        pais=pais,
                        costo_eur=costo_eur,
                        envio_eur=envio_eur,
                        precio_uyu=precio_uyu,
                        stock=stock_nuevo,
                        estado=estado,
                        tc_usado=tc_actual,
                        cover_local=final_cover_local,
                    )
                    disc.recalcular_costo_final()
                    db.session.add(disc)
                    db.session.flush()
                    evento = StatusHistory(
                        disc_id=disc.id,
                        estado=estado,
                        nota="Ingreso desde factura",
                    )
                    db.session.add(evento)
                    creados += 1

            db.session.commit()

            session.pop("invoice_items", None)
            session.pop("invoice_info", None)

            for e in errores_validacion:
                flash(e, "error")

            partes = []
            if creados:
                partes.append(f"{creados} disco{'s' if creados != 1 else ''} nuevo{'s' if creados != 1 else ''}")
            if actualizados:
                partes.append(f"{actualizados} con stock sumado")
            if partes:
                flash("Factura procesada: " + ", ".join(partes) + ".", "success")
            else:
                flash("No se cargo ningun disco.", "error")

            return redirect(url_for("dashboard"))

        # GET: mostrar pantalla de revision
        return render_template(
            "disc_invoice_review.html",
            items=items,
            info=info,
            estados=ESTADOS_DISCO,
            tc=Setting.get_tc(),
        )

    @app.route("/admin/discs/invoice-cancel", methods=["POST"])
    @auth.admin_required
    def disc_invoice_cancel():
        """Descarta la factura en proceso sin guardar nada."""
        session.pop("invoice_items", None)
        session.pop("invoice_info", None)
        flash("Factura descartada. No se cargo ningun disco.", "success")
        return redirect(url_for("dashboard"))

    @app.route("/admin/discs/<int:disc_id>")
    @auth.admin_required
    def disc_detail(disc_id: int):
        disc = Disc.query.get_or_404(disc_id)
        # disc.history ya viene ordenado desc por la relacion del modelo
        return render_template(
            "disc_detail.html",
            disc=disc,
            history=disc.history,
            estados=ESTADOS_DISCO,
            tc=Setting.get_tc(),
        )

    @app.route("/admin/discs/<int:disc_id>/upload-cover", methods=["POST"])
    @auth.admin_required
    def disc_upload_cover(disc_id: int):
        """Sube una portada manual a un disco existente.

        Soporta dos modos:
        - HTML normal: redirige a la pantalla de edicion.
        - AJAX (header X-Requested-With: XMLHttpRequest): responde JSON.
        """
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        disc = Disc.query.get_or_404(disc_id)

        def _err(msg, status=400):
            if is_ajax:
                return jsonify({"ok": False, "error": msg}), status
            flash(msg, "error")
            return redirect(url_for("disc_edit", disc_id=disc.id))

        file = request.files.get("cover")
        if not file or not file.filename:
            return _err("No se selecciono ningun archivo.")

        media_type = (file.mimetype or "").lower()
        if media_type not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
            return _err(f"Tipo de archivo no soportado: {media_type}.")

        ext_map = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
        }
        ext = ext_map.get(media_type, ".jpg")

        import uuid as _uuid
        filename = f"cover_{_uuid.uuid4().hex[:12]}{ext}"
        covers_dir = app.config["COVERS_DIR"]
        os.makedirs(covers_dir, exist_ok=True)
        dest_path = os.path.join(covers_dir, filename)

        try:
            file.save(dest_path)
        except Exception as e:
            return _err(f"Error al guardar archivo: {e}", status=500)

        # Convertir a WebP (menos peso, misma calidad visual)
        filename = convertir_cover_a_webp(dest_path)

        disc.cover_local = filename
        db.session.commit()

        if is_ajax:
            return jsonify({
                "ok": True,
                "filename": filename,
                "url": url_for("serve_cover", filename=filename),
            })

        flash(f"Portada actualizada para {disc.artista} - {disc.titulo}.", "success")
        return redirect(url_for("disc_edit", disc_id=disc.id))

    @app.route("/admin/discs/<int:disc_id>/images", methods=["GET"])
    @auth.admin_required
    def disc_images_list(disc_id: int):
        """Lista las imagenes de galeria de un disco (JSON)."""
        disc = Disc.query.get_or_404(disc_id)
        return jsonify({
            "ok": True,
            "imagenes": [
                {"id": img.id, "posicion": img.posicion,
                 "url": url_for("serve_cover", filename=img.filename)}
                for img in disc.imagenes
            ],
        })

    @app.route("/admin/discs/<int:disc_id>/images/upload", methods=["POST"])
    @auth.admin_required
    def disc_images_upload(disc_id: int):
        """Sube una o varias fotos de galeria a un disco."""
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        disc = Disc.query.get_or_404(disc_id)

        def _err(msg, status=400):
            if is_ajax:
                return jsonify({"ok": False, "error": msg}), status
            flash(msg, "error")
            return redirect(url_for("disc_edit", disc_id=disc.id))

        files = request.files.getlist("images") or request.files.getlist("image")
        files = [f for f in files if f and f.filename]
        if not files:
            return _err("No se selecciono ninguna imagen.")

        ext_map = {"image/jpeg": ".jpg", "image/png": ".png",
                   "image/gif": ".gif", "image/webp": ".webp"}
        covers_dir = app.config["COVERS_DIR"]
        os.makedirs(covers_dir, exist_ok=True)

        import uuid as _uuid
        pos = len(disc.imagenes)
        subidas = []
        for file in files:
            media_type = (file.mimetype or "").lower()
            if media_type not in ext_map:
                continue
            ext = ext_map[media_type]
            filename = f"img_{_uuid.uuid4().hex[:12]}{ext}"
            try:
                _dest = os.path.join(covers_dir, filename)
                file.save(_dest)
                filename = convertir_cover_a_webp(_dest)
            except Exception:
                continue
            img = DiscImage(disc_id=disc.id, posicion=pos, filename=filename)
            db.session.add(img)
            pos += 1
            subidas.append(img)
        db.session.commit()

        if is_ajax:
            return jsonify({
                "ok": True,
                "imagenes": [
                    {"id": i.id, "posicion": i.posicion,
                     "url": url_for("serve_cover", filename=i.filename)}
                    for i in subidas
                ],
            })
        flash(f"{len(subidas)} imagen(es) agregada(s).", "success")
        return redirect(url_for("disc_edit", disc_id=disc.id))

    @app.route("/admin/images/<int:image_id>/delete", methods=["POST"])
    @auth.admin_required
    def disc_image_delete(image_id: int):
        img = DiscImage.query.get_or_404(image_id)
        disc_id = img.disc_id
        # Borrar el archivo fisico (best-effort)
        try:
            path = os.path.join(app.config["COVERS_DIR"], img.filename)
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
        db.session.delete(img)
        db.session.commit()
        return jsonify({"ok": True, "disc_id": disc_id})

    @app.route("/admin/discs/<int:disc_id>/images/reorder", methods=["POST"])
    @auth.admin_required
    def disc_images_reorder(disc_id: int):
        disc = Disc.query.get_or_404(disc_id)
        ids_raw = request.form.get("ids", "")
        ids = [int(x) for x in str(ids_raw).split(",") if x.strip().isdigit()]
        by_id = {img.id: img for img in disc.imagenes}
        for pos, iid in enumerate(ids):
            if iid in by_id:
                by_id[iid].posicion = pos
        db.session.commit()
        return jsonify({"ok": True})

    @app.route("/admin/discs/invoice-review/upload-cover", methods=["POST"])
    @auth.admin_required
    def disc_invoice_upload_cover():
        """Sube una portada manual a un item de la factura en revision.

        Espera form-data con:
          - item_idx (int): indice del item en session
          - cover (file)

        Devuelve JSON con el nombre del archivo guardado.
        """
        item_idx = request.form.get("item_idx")
        try:
            item_idx = int(item_idx)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "item_idx invalido"}), 400

        items = session.get("invoice_items")
        if not items or item_idx < 0 or item_idx >= len(items):
            return jsonify({"ok": False, "error": "item no encontrado en sesion"}), 404

        file = request.files.get("cover")
        if not file or not file.filename:
            return jsonify({"ok": False, "error": "archivo vacio"}), 400

        media_type = (file.mimetype or "").lower()
        if media_type not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
            return jsonify({"ok": False, "error": f"tipo no soportado: {media_type}"}), 400

        ext_map = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
        }
        ext = ext_map.get(media_type, ".jpg")

        import uuid as _uuid
        filename = f"cover_{_uuid.uuid4().hex[:12]}{ext}"
        covers_dir = app.config["COVERS_DIR"]
        os.makedirs(covers_dir, exist_ok=True)
        dest_path = os.path.join(covers_dir, filename)

        try:
            file.save(dest_path)
            filename = convertir_cover_a_webp(dest_path)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

        # Actualizar en session
        items[item_idx]["cover_local_manual"] = filename
        session["invoice_items"] = items
        session.modified = True

        return jsonify({
            "ok": True,
            "filename": filename,
            "url": url_for("serve_cover", filename=filename),
        })

    @app.route("/admin/discs/bulk-action", methods=["POST"])
    @auth.admin_required
    def disc_bulk_action():
        """Acciones masivas sobre discos seleccionados desde el dashboard.

        Espera:
          - action: "delete" | "status"
          - ids[]: lista de IDs
          - nuevo_estado: (solo si action=status)
        """
        action = (request.form.get("action") or "").strip()
        ids = request.form.getlist("ids[]") or request.form.getlist("ids")
        try:
            ids = [int(x) for x in ids if x]
        except ValueError:
            flash("IDs invalidos.", "error")
            return redirect(url_for("dashboard"))

        if not ids:
            flash("No se selecciono ningun disco.", "error")
            return redirect(url_for("dashboard"))

        discs = Disc.query.filter(Disc.id.in_(ids)).all()
        if not discs:
            flash("Los discos seleccionados ya no existen.", "error")
            return redirect(url_for("dashboard"))

        if action == "delete":
            count = len(discs)
            for d in discs:
                db.session.delete(d)
            db.session.commit()
            flash(f"{count} disco{'s' if count != 1 else ''} eliminado{'s' if count != 1 else ''}.", "success")
            return redirect(url_for("dashboard"))

        if action == "status":
            nuevo = (request.form.get("nuevo_estado") or "").strip()
            if nuevo not in ESTADOS_DISCO:
                flash(f"Estado invalido: {nuevo}", "error")
                return redirect(url_for("dashboard"))

            cambios = 0
            for d in discs:
                if d.estado != nuevo:
                    register_status_change(d, nuevo, nota=f"Cambio masivo desde dashboard")
                    cambios += 1
            db.session.commit()
            flash(f"{cambios} disco{'s' if cambios != 1 else ''} actualizado{'s' if cambios != 1 else ''} a {nuevo}.", "success")
            return redirect(url_for("dashboard"))

        if action == "set_field":
            # Cambio masivo de un parametro booleano o el cartel Nuevo
            campo = (request.form.get("campo") or "").strip()
            valor = (request.form.get("valor") or "").strip()
            campos_bool = ("destacado", "en_nuevos", "cartel_ultima", "mostrar_agotado", "es_preorder", "es_tip")
            cambios = 0
            if campo in campos_bool:
                nuevo_val = valor in ("1", "true", "on", "si")
                for d in discs:
                    setattr(d, campo, nuevo_val)
                    cambios += 1
                db.session.commit()
                etiquetas = {
                    "destacado": "Destacado", "en_nuevos": "Nuevos ingresos", "es_tip": "Tip",
                    "cartel_ultima": "Ultima copia", "mostrar_agotado": "Agotado visible",
                    "es_preorder": "Carrusel Pre-order",
                }
                estado_txt = "activado" if nuevo_val else "desactivado"
                flash(f"{cambios} disco(s): {etiquetas.get(campo, campo)} {estado_txt}.", "success")
            elif campo == "cartel_nuevo":
                if valor in ("auto", "si", "no"):
                    for d in discs:
                        d.cartel_nuevo = valor
                        cambios += 1
                    db.session.commit()
                    flash(f"{cambios} disco(s): cartel Nuevo = {valor}.", "success")
                else:
                    flash("Valor invalido para cartel Nuevo.", "error")
            elif campo == "quitar_oferta":
                for d in discs:
                    d.precio_oferta = None
                    cambios += 1
                db.session.commit()
                flash(f"{cambios} disco(s): oferta quitada.", "success")
            else:
                flash(f"Campo no valido para cambio masivo: {campo}", "error")
            return redirect(url_for("dashboard"))

        flash(f"Accion desconocida: {action}", "error")
        return redirect(url_for("dashboard"))

    # ----------------------------------------------------------------------
    # CRUD de Tracks (endpoints AJAX)
    # ----------------------------------------------------------------------

    @app.route("/admin/discs/<int:disc_id>/tracks", methods=["GET"])
    @auth.admin_required
    def tracks_list(disc_id: int):
        """Devuelve JSON con los tracks del disco."""
        disc = Disc.query.get_or_404(disc_id)
        return jsonify({
            "ok": True,
            "tracks": [t.to_dict() for t in disc.tracks],
        })

    @app.route("/admin/discs/<int:disc_id>/tracks/add", methods=["POST"])
    @auth.admin_required
    def tracks_add(disc_id: int):
        """Agrega un track vacio al final del listado."""
        disc = Disc.query.get_or_404(disc_id)
        max_pos = max((t.posicion for t in disc.tracks), default=-1)
        t = Track(
            disc_id=disc.id,
            posicion=max_pos + 1,
            lado=None,
            titulo="Nuevo track",
        )
        db.session.add(t)
        db.session.commit()
        return jsonify({"ok": True, "track": t.to_dict()})

    @app.route("/admin/discs/<int:disc_id>/tracks/parse", methods=["POST"])
    @auth.admin_required
    def tracks_parse(disc_id: int):
        """Parsea texto plano y reemplaza (o agrega) los tracks."""
        disc = Disc.query.get_or_404(disc_id)
        texto = (request.form.get("texto") or "").strip()
        modo = (request.form.get("modo") or "reemplazar").strip()

        if not texto:
            return jsonify({"ok": False, "error": "Texto vacio"}), 400

        items = parse_tracklist_text(texto)
        if not items:
            return jsonify({"ok": False, "error": "No se pudo parsear ningun track"}), 400

        if modo == "reemplazar":
            # Borrar los actuales
            Track.query.filter_by(disc_id=disc.id).delete()
            db.session.flush()
            for item in items:
                t = Track(
                    disc_id=disc.id,
                    posicion=item["posicion"],
                    lado=item["lado"],
                    titulo=item["titulo"],
                    artista=item["artista"],
                )
                db.session.add(t)
        else:  # agregar
            max_pos = max((t.posicion for t in disc.tracks), default=-1)
            for item in items:
                t = Track(
                    disc_id=disc.id,
                    posicion=max_pos + 1 + item["posicion"],
                    lado=item["lado"],
                    titulo=item["titulo"],
                    artista=item["artista"],
                )
                db.session.add(t)

        db.session.commit()
        return jsonify({
            "ok": True,
            "tracks": [t.to_dict() for t in disc.tracks],
            "count": len(items),
        })

    @app.route("/admin/tracks/<int:track_id>", methods=["POST"])
    @auth.admin_required
    def tracks_update(track_id: int):
        """Actualiza campos de un track."""
        t = Track.query.get_or_404(track_id)

        campos_str = ["lado", "titulo", "artista", "youtube_url", "duracion"]
        for campo in campos_str:
            if campo in request.form:
                val = request.form.get(campo, "").strip() or None
                setattr(t, campo, val)

        if "youtube_start" in request.form:
            val = request.form.get("youtube_start", "").strip()
            if val:
                try:
                    t.youtube_start = int(val)
                except ValueError:
                    t.youtube_start = None
            else:
                t.youtube_start = None

        if "posicion" in request.form:
            try:
                t.posicion = int(request.form.get("posicion"))
            except (TypeError, ValueError):
                pass

        db.session.commit()
        return jsonify({"ok": True, "track": t.to_dict()})

    @app.route("/admin/tracks/<int:track_id>/delete", methods=["POST"])
    @auth.admin_required
    def tracks_delete(track_id: int):
        t = Track.query.get_or_404(track_id)
        disc_id = t.disc_id
        db.session.delete(t)
        db.session.commit()
        return jsonify({"ok": True, "disc_id": disc_id})

    @app.route("/admin/discs/<int:disc_id>/tracks/reorder", methods=["POST"])
    @auth.admin_required
    def tracks_reorder(disc_id: int):
        """Recibe una lista de IDs en el orden nuevo, actualiza `posicion`."""
        disc = Disc.query.get_or_404(disc_id)
        ids_str = request.form.get("ids", "") or (request.get_json(silent=True) or {}).get("ids", "")
        if isinstance(ids_str, list):
            ids = [int(x) for x in ids_str if str(x).isdigit()]
        else:
            ids = [int(x) for x in str(ids_str).split(",") if x.strip().isdigit()]

        if not ids:
            return jsonify({"ok": False, "error": "Sin ids"}), 400

        # Validar que todos los IDs pertenecen al disco
        tracks_del_disc = {t.id: t for t in disc.tracks}
        for tid in ids:
            if tid not in tracks_del_disc:
                return jsonify({"ok": False, "error": f"Track {tid} no pertenece al disco"}), 400

        for pos, tid in enumerate(ids):
            tracks_del_disc[tid].posicion = pos

        db.session.commit()
        return jsonify({"ok": True, "count": len(ids)})

    @app.route("/admin/parse-tracklist", methods=["POST"])
    @auth.admin_required
    def parse_tracklist_route():
        """Parsea un tracklist en texto a filas estructuradas SIN guardar nada.

        Sirve para el editor de tracks en la pantalla de alta (el disco todavia
        no existe). Devuelve JSON con los tracks parseados.
        """
        data = request.get_json(silent=True) or request.form
        texto = (data.get("texto") or "").strip()
        if not texto:
            return jsonify({"ok": False, "tracks": []})
        items = parse_tracklist_text(texto)
        return jsonify({"ok": True, "tracks": items})

    @app.route("/admin/track-search", methods=["POST"])
    @auth.admin_required
    def track_search_route():
        """Busca un track en YouTube + SoundCloud y devuelve candidatos (JSON).

        Recibe JSON o form: artista, titulo, plataformas (opcional, csv).
        """
        import track_search

        data = request.get_json(silent=True) or request.form
        artista = (data.get("artista") or "").strip()
        titulo = (data.get("titulo") or "").strip()
        plataformas_raw = (data.get("plataformas") or "youtube,soundcloud").strip()
        plataformas = [p.strip() for p in plataformas_raw.split(",") if p.strip() in ("youtube", "soundcloud")]

        if not titulo and not artista:
            return jsonify({"ok": False, "error": "Falta artista o titulo."}), 400

        try:
            resultado = track_search.buscar_track(artista, titulo, plataformas=plataformas)
        except Exception as ex:
            return jsonify({"ok": False, "error": f"Error en la busqueda: {ex}"}), 500

        return jsonify(resultado)

    @app.route("/admin/tracks/<int:track_id>/upload-sample", methods=["POST"])
    @auth.admin_required
    def tracks_upload_sample(track_id: int):
        """Sube un MP3 para el track. Reemplaza el existente si hay."""
        t = Track.query.get_or_404(track_id)
        file = request.files.get("sample")
        if not file or not file.filename:
            return jsonify({"ok": False, "error": "No se selecciono archivo"}), 400

        media_type = (file.mimetype or "").lower()
        ext_map = {
            "audio/mpeg": ".mp3",
            "audio/mp3": ".mp3",
            "audio/wav": ".wav",
            "audio/x-wav": ".wav",
            "audio/ogg": ".ogg",
            "audio/webm": ".webm",
            # Video: se guarda el archivo tal cual; el player reproduce el audio
            # del video con <audio> (suena aunque no muestre imagen).
            "video/mp4": ".mp4",
            "audio/mp4": ".m4a",
            "audio/x-m4a": ".m4a",
            "video/quicktime": ".mov",
            "video/webm": ".webm",
            "video/x-m4v": ".m4v",
        }
        if media_type not in ext_map:
            return jsonify({"ok": False, "error": f"Tipo no soportado: {media_type}. Usar MP3, WAV, M4A o video MP4/MOV."}), 400
        ext = ext_map.get(media_type, ".mp3")

        # Borrar sample anterior si habia
        samples_dir = app.config["SAMPLES_DIR"]
        os.makedirs(samples_dir, exist_ok=True)
        if t.sample_local:
            old_path = os.path.join(samples_dir, t.sample_local)
            if os.path.exists(old_path):
                try:
                    os.remove(old_path)
                except OSError:
                    pass

        import uuid as _uuid
        filename = f"sample_{_uuid.uuid4().hex[:12]}{ext}"
        dest_path = os.path.join(samples_dir, filename)

        # Limitar tamano a 30 MB (los videos pesan mas que un mp3)
        MAX_BYTES = 30 * 1024 * 1024
        content = file.read()
        if len(content) > MAX_BYTES:
            return jsonify({
                "ok": False,
                "error": f"Archivo demasiado grande ({len(content)//1024//1024} MB). Maximo: 30 MB.",
            }), 400

        try:
            with open(dest_path, "wb") as f:
                f.write(content)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

        t.sample_local = filename
        db.session.commit()
        return jsonify({
            "ok": True,
            "track": t.to_dict(),
            "url": url_for("serve_sample", filename=filename),
        })

    @app.route("/admin/tracks/<int:track_id>/clear-sample", methods=["POST"])
    @auth.admin_required
    def tracks_clear_sample(track_id: int):
        """Borra el sample MP3 local del track."""
        t = Track.query.get_or_404(track_id)
        if t.sample_local:
            samples_dir = app.config["SAMPLES_DIR"]
            old_path = os.path.join(samples_dir, t.sample_local)
            if os.path.exists(old_path):
                try:
                    os.remove(old_path)
                except OSError:
                    pass
            t.sample_local = None
            db.session.commit()
        return jsonify({"ok": True, "track": t.to_dict()})

    @app.route("/admin/discs/<int:disc_id>/edit", methods=["GET", "POST"])
    @auth.admin_required
    def disc_edit(disc_id: int):
        disc = Disc.query.get_or_404(disc_id)
        tc = Setting.get_tc()
        estado_anterior = disc.estado

        if request.method == "POST":
            errores = populate_disc_from_form(disc, request.form)
            if errores:
                for e in errores:
                    flash(e, "error")
                return render_template(
                    "disc_form.html",
                    mode="edit",
                    disc=disc,
                    form_data=request.form,
                    estados=ESTADOS_DISCO,
                    condiciones=CONDICIONES_DISCO,
                    tc=tc,
                    prefill_info=None,
                    **_url_queue_ctx(),
                )

            if disc.estado != estado_anterior:
                ahora = datetime.utcnow()
                if disc.estado == "Reservado" and not disc.fecha_reserva:
                    disc.fecha_reserva = ahora
                elif disc.estado == "Vendido" and not disc.fecha_venta:
                    disc.fecha_venta = ahora
                evento = StatusHistory(
                    disc_id=disc.id,
                    estado=disc.estado,
                    nota=f"Cambio desde {estado_anterior} (edicion)",
                    fecha=ahora,
                )
                db.session.add(evento)

            db.session.commit()
            flash(f"Disco actualizado: {disc.artista} - {disc.titulo}", "success")

            # Si hay una cola de URLs activa, avanzar al siguiente disco en vez
            # de volver al dashboard (asi no se pierde la tanda al guardar).
            cola = session.get("url_queue")
            if cola:
                siguiente = cola.pop(0)
                session["url_queue"] = cola
                return _procesar_url_scrape(siguiente)
            elif cola is not None:
                # Cola vacia pero existente: era el ultimo, limpiar y terminar
                session.pop("url_queue", None)
                session.pop("url_queue_total", None)
                session.pop("url_queue_done", None)
                flash("Terminaste de cargar toda la tanda de URLs.", "success")

            return redirect(url_for("dashboard"))

        return render_template(
            "disc_form.html",
            mode="edit",
            disc=disc,
            form_data={},
            estados=ESTADOS_DISCO,
            condiciones=CONDICIONES_DISCO,
            tc=tc,
            prefill_info=None,
            **_url_queue_ctx(),
        )

    @app.route("/admin/discs/<int:disc_id>/delete", methods=["POST"])
    @auth.admin_required
    def disc_delete(disc_id: int):
        disc = Disc.query.get_or_404(disc_id)
        nombre = f"{disc.artista} - {disc.titulo}"
        db.session.delete(disc)
        db.session.commit()
        flash(f"Disco eliminado: {nombre}", "success")
        return redirect(url_for("dashboard"))

    @app.route("/admin/discs/<int:disc_id>/duplicate", methods=["POST"])
    @auth.admin_required
    def disc_duplicate(disc_id: int):
        """Crea una copia identica de un disco (util para copias multiples
        del mismo titulo). Copia campos, tracks e imagenes; arranca Disponible."""
        from models import DiscImage, Track
        from sqlalchemy import inspect as _sa_inspect
        orig = Disc.query.get_or_404(disc_id)

        nuevo = Disc()
        # Copiar todas las columnas menos id y timestamps unicos
        excluir = {"id"}
        for col in _sa_inspect(Disc).columns.keys():
            if col in excluir:
                continue
            setattr(nuevo, col, getattr(orig, col))
        # La copia arranca como Disponible y sin copias vendidas
        nuevo.estado = "Disponible"
        nuevo.copias_vendidas = 0
        nuevo.fecha_venta = None
        nuevo.fecha_ingreso = datetime.utcnow()
        db.session.add(nuevo)
        db.session.flush()  # para tener el id del nuevo

        # Copiar tracks
        for t in orig.tracks:
            nt = Track()
            for col in _sa_inspect(Track).columns.keys():
                if col in ("id", "disc_id"):
                    continue
                setattr(nt, col, getattr(t, col))
            nt.disc_id = nuevo.id
            db.session.add(nt)

        # Copiar imagenes
        for img in orig.imagenes:
            ni = DiscImage()
            for col in _sa_inspect(DiscImage).columns.keys():
                if col in ("id", "disc_id"):
                    continue
                setattr(ni, col, getattr(img, col))
            ni.disc_id = nuevo.id
            db.session.add(ni)

        db.session.commit()
        flash(f"Disco duplicado: {nuevo.artista} - {nuevo.titulo}. La copia esta como Disponible.", "success")
        return redirect(url_for("disc_edit", disc_id=nuevo.id))

    @app.route("/admin/discs/<int:disc_id>/status", methods=["POST"])
    @auth.admin_required
    def disc_status(disc_id: int):
        disc = Disc.query.get_or_404(disc_id)
        nuevo = (request.form.get("estado") or "").strip()
        if nuevo not in ESTADOS_DISCO:
            flash(f"Estado invalido: {nuevo}", "error")
            return redirect(url_for("dashboard"))
        if nuevo == disc.estado:
            flash(f"El disco ya esta en estado {nuevo}.", "error")
            return redirect(url_for("dashboard"))
        register_status_change(disc, nuevo, nota="Cambio rapido desde dashboard")
        db.session.commit()
        flash(f"Estado actualizado a {nuevo}: {disc.artista} - {disc.titulo}", "success")
        return redirect(url_for("dashboard"))

    @app.route("/admin/discs/<int:disc_id>/quick-update", methods=["POST"])
    @auth.admin_required
    def disc_quick_update(disc_id: int):
        """Edicion rapida de un campo desde el dashboard (AJAX). Devuelve JSON.

        Campos permitidos: precio_uyu, stock, estado.
        """
        disc = Disc.query.get_or_404(disc_id)
        data = request.get_json(silent=True) or request.form
        campo = (data.get("campo") or "").strip()
        valor = data.get("valor")

        if campo == "precio_uyu":
            try:
                nuevo = int(float(valor))
                if nuevo < 0:
                    raise ValueError()
            except (ValueError, TypeError):
                return jsonify({"ok": False, "error": "Precio invalido"}), 400
            disc.precio_uyu = nuevo
            db.session.commit()
            return jsonify({
                "ok": True,
                "campo": campo,
                "valor": disc.precio_uyu,
                "ganancia_uyu": round(disc.ganancia_uyu or 0),
                "margen_pct": round(disc.margen_pct or 0, 1),
            })

        elif campo == "stock":
            try:
                nuevo = int(valor)
                if nuevo < 0:
                    raise ValueError()
            except (ValueError, TypeError):
                return jsonify({"ok": False, "error": "Stock invalido"}), 400
            disc.stock = nuevo
            db.session.commit()
            return jsonify({"ok": True, "campo": campo, "valor": disc.stock})

        elif campo == "estado":
            nuevo = (valor or "").strip()
            if nuevo not in ESTADOS_DISCO:
                return jsonify({"ok": False, "error": "Estado invalido"}), 400
            if nuevo != disc.estado:
                register_status_change(disc, nuevo, nota="Edicion rapida desde dashboard")
                db.session.commit()
            return jsonify({"ok": True, "campo": campo, "valor": disc.estado})

        elif campo in ("costo_eur", "envio_eur"):
            try:
                nuevo = round(float(str(valor).replace(",", ".")), 2)
                if nuevo < 0:
                    raise ValueError()
            except (ValueError, TypeError):
                return jsonify({"ok": False, "error": "Valor invalido"}), 400
            setattr(disc, campo, nuevo)
            disc.recalcular_costo_final()
            db.session.commit()
            return jsonify({
                "ok": True,
                "campo": campo,
                "valor": nuevo,
                "costo_final_uyu": round(disc.costo_final_uyu or 0),
                "ganancia_uyu": round(disc.ganancia_uyu or 0),
                "margen_pct": round(disc.margen_pct or 0, 1),
            })

        elif campo == "origen":
            nuevo = (valor or "").strip()
            if nuevo not in ("arcano", "personal"):
                return jsonify({"ok": False, "error": "Origen invalido"}), 400
            disc.origen = nuevo
            db.session.commit()
            return jsonify({"ok": True, "campo": campo, "valor": disc.origen})

        elif campo == "pedido_id":
            raw = (str(valor) if valor is not None else "").strip()
            if raw == "" or raw == "0":
                disc.pedido_id = None
            elif raw.isdigit():
                from models import Pedido
                if Pedido.query.get(int(raw)):
                    disc.pedido_id = int(raw)
                else:
                    return jsonify({"ok": False, "error": "Pedido inexistente"}), 400
            else:
                return jsonify({"ok": False, "error": "Pedido invalido"}), 400
            db.session.commit()
            return jsonify({"ok": True, "campo": campo, "valor": disc.pedido_id or ""})

        elif campo in ("destacado", "en_nuevos", "cartel_ultima", "mostrar_agotado", "es_preorder", "es_tip"):
            # valor viene como "1"/"0" o true/false
            nuevo = str(valor) in ("1", "true", "True", "on")
            setattr(disc, campo, nuevo)
            db.session.commit()
            return jsonify({"ok": True, "campo": campo, "valor": nuevo})

        elif campo == "cartel_nuevo":
            nuevo = (valor or "auto").strip()
            if nuevo not in ("auto", "si", "no"):
                return jsonify({"ok": False, "error": "Valor invalido"}), 400
            disc.cartel_nuevo = nuevo
            db.session.commit()
            return jsonify({"ok": True, "campo": campo, "valor": nuevo})

        elif campo in ("sello", "artista", "titulo", "genero"):
            nuevo = (valor or "").strip()
            # artista y titulo no pueden quedar vacios
            if campo in ("artista", "titulo") and not nuevo:
                return jsonify({"ok": False, "error": f"El {campo} no puede quedar vacio"}), 400
            setattr(disc, campo, nuevo or None)
            db.session.commit()
            return jsonify({"ok": True, "campo": campo, "valor": getattr(disc, campo) or ""})

        return jsonify({"ok": False, "error": "Campo no editable"}), 400

    # ----------------------------------------------------------------------
    # Contabilidad / metricas
    # ----------------------------------------------------------------------

    @app.route("/admin/resumen")
    @auth.admin_required
    def resumen():
        # Redirect de la URL vieja a la nueva
        return redirect(url_for("contabilidad"))

    @app.route("/admin/radar")
    @auth.admin_required
    def radar_demanda():
        """Radar de demanda: rankea discos por interes relativo
        (reproducciones / dias disponible), cruzado con el estado. Separa los
        disponibles (accionables) de los vendidos (validacion del ojo)."""
        ahora = datetime.utcnow()
        filas = []
        for d in Disc.query.filter(Disc.estado != "Descartado").all():
            plays = sum((t.play_count or 0) for t in d.tracks)
            if d.fecha_ingreso:
                fin = d.fecha_venta if (d.estado == "Vendido" and d.fecha_venta) else ahora
                dias = max((fin - d.fecha_ingreso).days, 1)
            else:
                dias = 1
            filas.append({
                "disc": d, "plays": plays, "dias": dias,
                "interes": round(plays / dias, 2),
            })
        con_escuchas = [f for f in filas if f["plays"] > 0]
        disponibles = sorted(
            [f for f in con_escuchas if f["disc"].estado in
             ("Disponible", "Pre-order", "Reservado", "Apartado para mi")],
            key=lambda f: f["interes"], reverse=True,
        )
        vendidos = sorted(
            [f for f in con_escuchas if f["disc"].estado == "Vendido"],
            key=lambda f: f["interes"], reverse=True,
        )
        return render_template(
            "radar.html",
            disponibles=disponibles,
            vendidos=vendidos,
            con_escuchas=len(con_escuchas),
            sin_escuchas=len(filas) - len(con_escuchas),
            total_discos=len(filas),
        )

    @app.route("/admin/contabilidad")
    @auth.admin_required
    def contabilidad():
        from models import Egreso, EgresoRecurrente
        periodo = request.args.get("periodo", "mes_actual")
        desde = request.args.get("desde")
        hasta = request.args.get("hasta")
        data = metrics.get_all_metrics()
        conta = metrics.contabilidad(periodo, desde, hasta)
        # Lista de egresos del periodo para mostrar
        inicio, fin = metrics._rango_periodo(periodo, desde, hasta)
        egresos = (
            Egreso.query
            .filter(Egreso.fecha >= inicio, Egreso.fecha < fin)
            .order_by(Egreso.fecha.desc())
            .all()
        )
        recurrentes = EgresoRecurrente.query.order_by(EgresoRecurrente.categoria).all()
        por_cobrar = metrics.dinero_por_cobrar()
        ingresos_egresos_6m = metrics.ingresos_vs_egresos_6m()
        from models import Pedido
        pedidos_lista = Pedido.query.order_by(Pedido.fecha.desc()).all()
        return render_template(
            "contabilidad.html",
            tc=Setting.get_tc(),
            tc_usd=Setting.get_tc_usd(),
            conta=conta,
            periodo=periodo,
            desde=desde or "",
            hasta=hasta or "",
            egresos=egresos,
            recurrentes=recurrentes,
            categorias=Setting.get_categorias_egreso(),
            monedas=["UYU", "USD", "EUR"],
            por_cobrar=por_cobrar,
            ingresos_egresos_6m=ingresos_egresos_6m,
            pedidos_lista=pedidos_lista,
            **data,
        )

    def _convertir_a_uyu(monto, moneda):
        """Convierte un monto a UYU segun la moneda y devuelve (monto_uyu, tc)."""
        if moneda == "USD":
            tc = Setting.get_tc_usd()
            return monto * tc, tc
        elif moneda == "EUR":
            tc = Setting.get_tc()
            return monto * tc, tc
        return monto, 1.0

    @app.route("/admin/contabilidad/egreso/agregar", methods=["POST"])
    @auth.admin_required
    def egreso_agregar():
        from models import Egreso
        from datetime import datetime as _dt
        try:
            monto = float(request.form.get("monto") or 0)
        except ValueError:
            monto = 0
        moneda = (request.form.get("moneda") or "UYU").strip().upper()
        if moneda not in ("UYU", "USD", "EUR"):
            moneda = "UYU"
        categoria = (request.form.get("categoria") or "Otros").strip()
        # Categoria nueva?
        nueva_cat = (request.form.get("categoria_nueva") or "").strip()
        if nueva_cat:
            Setting.add_categoria_egreso(nueva_cat)
            categoria = nueva_cat
        descripcion = (request.form.get("descripcion") or "").strip()
        fecha_str = request.form.get("fecha") or ""
        try:
            fecha = _dt.strptime(fecha_str, "%Y-%m-%d").date() if fecha_str else _dt.utcnow().date()
        except ValueError:
            fecha = _dt.utcnow().date()

        if monto <= 0:
            flash("El monto debe ser mayor a 0.", "error")
            return redirect(url_for("contabilidad"))

        monto_uyu, tc = _convertir_a_uyu(monto, moneda)
        e = Egreso(
            fecha=fecha, categoria=categoria, descripcion=descripcion,
            monto=monto, moneda=moneda, tc_usado=tc, monto_uyu=monto_uyu,
        )
        db.session.add(e)
        db.session.commit()
        flash(f"Egreso agregado: {categoria} ${monto_uyu:,.0f} UYU.", "success")
        return redirect(url_for("contabilidad"))

    @app.route("/admin/contabilidad/egreso/<int:egreso_id>/borrar", methods=["POST"])
    @auth.admin_required
    def egreso_borrar(egreso_id):
        from models import Egreso
        e = Egreso.query.get_or_404(egreso_id)
        db.session.delete(e)
        db.session.commit()
        flash("Egreso eliminado.", "success")
        return redirect(url_for("contabilidad"))

    @app.route("/admin/contabilidad/egreso/<int:egreso_id>/monto-uyu", methods=["POST"])
    @auth.admin_required
    def egreso_monto_uyu(egreso_id):
        """Edita el monto real en UYU de un egreso (lo que debito el banco).

        Guardar vacio recalcula desde la moneda original y el TC. Si el egreso
        ya era en UYU, no hay nada que recalcular, asi que vacio no hace nada.
        """
        from models import Egreso
        e = Egreso.query.get_or_404(egreso_id)
        raw = (request.form.get("monto_uyu") or "").strip().replace(".", "").replace(",", ".")
        if raw == "":
            # Recalcular desde la moneda original * tc (si es UYU, queda igual)
            if e.moneda == "UYU":
                e.monto_uyu = e.monto
            else:
                e.monto_uyu = round((e.monto or 0) * (e.tc_usado or 1), 2)
            db.session.commit()
            flash("Monto recalculado con el tipo de cambio.", "success")
        else:
            try:
                val = float(raw)
                if val < 0:
                    raise ValueError()
                e.monto_uyu = val
                db.session.commit()
                flash(f"Monto en pesos actualizado: {val:,.0f} UYU.".replace(",", "."), "success")
            except (ValueError, TypeError):
                flash("Monto invalido.", "error")
        return redirect(url_for("contabilidad"))

    @app.route("/admin/contabilidad/recurrente/agregar", methods=["POST"])
    @auth.admin_required
    def recurrente_agregar():
        from models import EgresoRecurrente
        try:
            monto = float(request.form.get("monto") or 0)
        except ValueError:
            monto = 0
        moneda = (request.form.get("moneda") or "UYU").strip().upper()
        categoria = (request.form.get("categoria") or "Otros").strip()
        descripcion = (request.form.get("descripcion") or "").strip()
        if monto <= 0:
            flash("El monto del gasto recurrente debe ser mayor a 0.", "error")
            return redirect(url_for("contabilidad"))
        r = EgresoRecurrente(categoria=categoria, descripcion=descripcion, monto=monto, moneda=moneda)
        db.session.add(r)
        db.session.commit()
        flash(f"Gasto mensual agregado: {categoria}.", "success")
        return redirect(url_for("contabilidad"))

    @app.route("/admin/contabilidad/recurrente/<int:rec_id>/borrar", methods=["POST"])
    @auth.admin_required
    def recurrente_borrar(rec_id):
        from models import EgresoRecurrente
        r = EgresoRecurrente.query.get_or_404(rec_id)
        db.session.delete(r)
        db.session.commit()
        flash("Gasto mensual eliminado.", "success")
        return redirect(url_for("contabilidad"))

    @app.route("/admin/contabilidad/generar-mes", methods=["POST"])
    @auth.admin_required
    def generar_gastos_mes():
        """Genera los egresos del mes actual a partir de los recurrentes activos.
        Evita duplicar: no genera si ya hay un egreso de esa categoria+descripcion este mes."""
        from models import Egreso, EgresoRecurrente
        from datetime import datetime as _dt, date
        hoy = date.today()
        inicio = date(hoy.year, hoy.month, 1)
        recurrentes = EgresoRecurrente.query.filter_by(activo=True).all()
        creados = 0
        for r in recurrentes:
            # Ya existe este mes?
            existe = (
                Egreso.query
                .filter(Egreso.fecha >= inicio)
                .filter(Egreso.categoria == r.categoria)
                .filter(Egreso.descripcion == r.descripcion)
                .filter(Egreso.monto == r.monto)
                .first()
            )
            if existe:
                continue
            monto_uyu, tc = _convertir_a_uyu(r.monto, r.moneda)
            e = Egreso(
                fecha=hoy, categoria=r.categoria, descripcion=r.descripcion,
                monto=r.monto, moneda=r.moneda, tc_usado=tc, monto_uyu=monto_uyu,
            )
            db.session.add(e)
            creados += 1
        db.session.commit()
        if creados:
            flash(f"{creados} gasto(s) mensual(es) generado(s) para este mes.", "success")
        else:
            flash("No habia gastos mensuales nuevos para generar (ya estaban cargados).", "info")
        return redirect(url_for("contabilidad"))

    @app.route("/admin/contabilidad/exportar")
    @auth.admin_required
    def contabilidad_exportar():
        """Exporta los egresos del periodo a CSV."""
        from models import Egreso
        import csv
        import io
        periodo = request.args.get("periodo", "mes_actual")
        desde = request.args.get("desde")
        hasta = request.args.get("hasta")
        inicio, fin = metrics._rango_periodo(periodo, desde, hasta)
        egresos = (
            Egreso.query
            .filter(Egreso.fecha >= inicio, Egreso.fecha < fin)
            .order_by(Egreso.fecha.asc())
            .all()
        )
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Fecha", "Categoria", "Descripcion", "Monto", "Moneda", "TC usado", "Monto UYU"])
        for e in egresos:
            writer.writerow([
                e.fecha.strftime("%Y-%m-%d"), e.categoria, e.descripcion or "",
                f"{e.monto:.2f}", e.moneda, f"{e.tc_usado:.2f}", f"{e.monto_uyu:.2f}",
            ])
        # Tambien las ventas del periodo
        from datetime import datetime as _dt
        inicio_dt = _dt(inicio.year, inicio.month, inicio.day)
        fin_dt = _dt(fin.year, fin.month, fin.day)
        vendidos = (
            Disc.query.filter(Disc.estado == "Vendido")
            .filter(Disc.fecha_venta >= inicio_dt, Disc.fecha_venta < fin_dt)
            .order_by(Disc.fecha_venta.asc()).all()
        )
        writer.writerow([])
        writer.writerow(["VENTAS DEL PERIODO"])
        writer.writerow(["Fecha venta", "Artista", "Titulo", "Sello", "Precio UYU", "Copias", "Costo UYU", "Ganancia UYU"])
        for d in vendidos:
            precio = d.precio_mostrar or d.precio_uyu or 0
            writer.writerow([
                d.fecha_venta.strftime("%Y-%m-%d") if d.fecha_venta else "",
                d.artista, d.titulo, d.sello or "",
                f"{precio:.0f}", d.copias_facturadas,
                f"{(d.costo_final_uyu or 0):.0f}",
                f"{((precio - (d.costo_final_uyu or 0)) * d.copias_facturadas):.0f}",
            ])
        from flask import Response
        csv_data = output.getvalue()
        output.close()
        filename = f"arcano_contabilidad_{periodo}.csv"
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    # ---- PEDIDOS (compras de stock) ----
    @app.route("/admin/pedidos")
    @auth.admin_required
    def pedidos():
        from models import Pedido, PROVEEDORES_PEDIDO
        from sqlalchemy.orm import selectinload as _selectinload
        lista = (
            Pedido.query
            .options(_selectinload(Pedido.discos))
            .order_by(Pedido.fecha.desc())
            .all()
        )
        # Para comparacion: mejor y peor por margen (solo los que tienen ventas)
        con_ventas = [p for p in lista if p.discos_vendidos > 0 and p.costo_total_uyu > 0]
        mejor_pedido = max(con_ventas, key=lambda p: p.margen_pct) if con_ventas else None
        peor_pedido = min(con_ventas, key=lambda p: p.margen_pct) if con_ventas else None
        # Totales globales
        total_invertido = sum(p.costo_total_uyu for p in lista)
        total_recaudado = sum(p.recaudado_uyu for p in lista)
        return render_template(
            "pedidos.html",
            pedidos=lista,
            proveedores=PROVEEDORES_PEDIDO,
            tc=Setting.get_tc(),
            tc_usd=Setting.get_tc_usd(),
            mejor_pedido=mejor_pedido,
            peor_pedido=peor_pedido,
            total_invertido=total_invertido,
            total_recaudado=total_recaudado,
        )

    @app.route("/admin/pedidos/agregar", methods=["POST"])
    @auth.admin_required
    def pedido_agregar():
        from models import Pedido
        from datetime import datetime as _dt
        numero = (request.form.get("numero") or "").strip()
        proveedor = (request.form.get("proveedor") or "Deejay.de").strip()
        try:
            cant = int(request.form.get("cant_discos") or 0)
        except ValueError:
            cant = 0
        try:
            costo_eur = float(request.form.get("costo_eur") or 0)
        except ValueError:
            costo_eur = 0
        try:
            costo_usd = float(request.form.get("costo_usd") or 0)
        except ValueError:
            costo_usd = 0
        fecha_str = request.form.get("fecha") or ""
        try:
            fecha = _dt.strptime(fecha_str, "%Y-%m-%d").date() if fecha_str else _dt.utcnow().date()
        except ValueError:
            fecha = _dt.utcnow().date()
        notas = (request.form.get("notas") or "").strip()
        ped = Pedido(
            numero=numero, fecha=fecha, cant_discos=cant,
            costo_eur=costo_eur, costo_usd=costo_usd, proveedor=proveedor, notas=notas,
        )
        db.session.add(ped)
        db.session.commit()
        flash(f"Pedido {numero or ped.id} agregado.", "success")
        return redirect(url_for("pedidos"))

    @app.route("/admin/pedidos/<int:pedido_id>/borrar", methods=["POST"])
    @auth.admin_required
    def pedido_borrar(pedido_id):
        from models import Pedido, Disc
        ped = Pedido.query.get_or_404(pedido_id)
        # Desvincular los discos (no borrarlos)
        Disc.query.filter_by(pedido_id=ped.id).update({"pedido_id": None})
        db.session.delete(ped)
        db.session.commit()
        flash("Pedido eliminado (los discos quedaron sin pedido).", "success")
        return redirect(url_for("pedidos"))

    @app.route("/admin/pedidos/<int:pedido_id>/costo-manual", methods=["POST"])
    @auth.admin_required
    def pedido_costo_manual(pedido_id):
        """Setea (o limpia) el costo real en UYU del pedido (lo que debito el banco)."""
        from models import Pedido
        ped = Pedido.query.get_or_404(pedido_id)
        raw = (request.form.get("costo_uyu_manual") or "").strip().replace(".", "").replace(",", ".")
        if raw == "":
            # Vacio => volver al calculo automatico
            ped.costo_uyu_manual = None
            db.session.commit()
            flash("Costo del pedido: vuelve al calculo automatico.", "success")
        else:
            try:
                val = float(raw)
                if val < 0:
                    raise ValueError()
                ped.costo_uyu_manual = val
                db.session.commit()
                flash(f"Costo real del pedido guardado: {val:,.0f} UYU.".replace(",", "."), "success")
            except (ValueError, TypeError):
                flash("Monto invalido.", "error")
        return redirect(url_for("pedidos"))

    @app.route("/admin/pedidos/exportar/excel")
    @auth.admin_required
    def pedidos_exportar_excel():
        """Exporta todos los pedidos a Excel: KPIs globales + resumen completo + detalle."""
        from models import Pedido
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
        import io

        pedidos = Pedido.query.order_by(Pedido.fecha.desc()).all()
        wb = openpyxl.Workbook()

        header_font = Font(bold=True, color="FFFFFF", name="Arial")
        header_fill = PatternFill(start_color="1A1A1A", end_color="1A1A1A", fill_type="solid")
        titulo_font = Font(bold=True, size=14, name="Arial")
        label_font = Font(bold=True, name="Arial", color="666666")

        # Totales globales
        total_invertido = sum(p.costo_total_uyu for p in pedidos)
        total_recaudado = sum(p.recaudado_uyu for p in pedidos)
        total_discos = sum(p.discos_asignados for p in pedidos)
        total_vendidos = sum(p.discos_vendidos for p in pedidos)
        con_ventas = [p for p in pedidos if p.discos_vendidos > 0 and p.costo_total_uyu > 0]
        mejor = max(con_ventas, key=lambda p: p.margen_pct) if con_ventas else None
        peor = min(con_ventas, key=lambda p: p.margen_pct) if con_ventas else None
        recuperados = sum(1 for p in pedidos if p.recuperado)

        # --- Hoja 1: Panorama (KPIs globales) ---
        ws0 = wb.active
        ws0.title = "Panorama"
        ws0["A1"] = "Arcano Record Store - Informe de pedidos"
        ws0["A1"].font = titulo_font
        from datetime import datetime as _dt
        ws0["A2"] = f"Generado el {_dt.now().strftime('%d/%m/%Y')}"
        ws0["A2"].font = Font(italic=True, color="888888", name="Arial")

        kpis = [
            ("Total de pedidos", len(pedidos)),
            ("Total invertido (UYU)", round(total_invertido)),
            ("Total recaudado (UYU)", round(total_recaudado)),
            ("Resultado global (UYU)", round(total_recaudado - total_invertido)),
            ("Discos asignados", total_discos),
            ("Discos vendidos", total_vendidos),
            ("Pedidos recuperados", f"{recuperados} de {len(pedidos)}"),
            ("Mejor pedido", f"{mejor.numero or mejor.id} ({mejor.margen_pct}%)" if mejor else "-"),
            ("Peor pedido", f"{peor.numero or peor.id} ({peor.margen_pct}%)" if peor else "-"),
        ]
        r = 4
        for label, val in kpis:
            ws0.cell(row=r, column=1, value=label).font = label_font
            ws0.cell(row=r, column=2, value=val)
            r += 1
        ws0.column_dimensions["A"].width = 26
        ws0.column_dimensions["B"].width = 22

        # --- Hoja 2: Resumen de pedidos (con todas las metricas) ---
        ws = wb.create_sheet("Pedidos")
        headers = ["Pedido", "Fecha", "Proveedor", "Costo EUR", "Costo USD", "Costo total UYU",
                   "Discos declarados", "Discos asignados", "Vendidos", "Rotacion %",
                   "Recaudado UYU", "Resultado UYU", "Margen %", "Proyeccion UYU",
                   "Margen potencial %", "Costo x disco UYU", "Dias recuperacion", "Estado"]
        for col, h in enumerate(headers, 1):
            c = ws.cell(row=1, column=col, value=h)
            c.font = header_font
            c.fill = header_fill
            c.alignment = Alignment(horizontal="center")
        row = 2
        for p in pedidos:
            ws.cell(row=row, column=1, value=p.numero or f"#{p.id}")
            ws.cell(row=row, column=2, value=p.fecha.strftime("%d/%m/%Y") if p.fecha else "")
            ws.cell(row=row, column=3, value=p.proveedor)
            ws.cell(row=row, column=4, value=round(p.costo_eur, 2))
            ws.cell(row=row, column=5, value=round(p.costo_usd, 2))
            ws.cell(row=row, column=6, value=round(p.costo_total_uyu, 2))
            ws.cell(row=row, column=7, value=p.cant_discos)
            ws.cell(row=row, column=8, value=p.discos_asignados)
            ws.cell(row=row, column=9, value=p.discos_vendidos)
            ws.cell(row=row, column=10, value=int(p.rotacion_pct))
            ws.cell(row=row, column=11, value=round(p.recaudado_uyu, 2))
            ws.cell(row=row, column=12, value=round(p.resultado_uyu, 2))
            ws.cell(row=row, column=13, value=p.margen_pct)
            ws.cell(row=row, column=14, value=round(p.resultado_potencial_uyu, 2))
            ws.cell(row=row, column=15, value=p.margen_potencial_pct)
            ws.cell(row=row, column=16, value=round(p.costo_por_disco_uyu, 2))
            ws.cell(row=row, column=17, value=p.dias_recuperacion if p.dias_recuperacion is not None else "-")
            ws.cell(row=row, column=18, value="Recuperado" if p.recuperado else "En perdida")
            row += 1
        for col in range(1, len(headers) + 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(col)].width = 15

        # --- Hoja 3: Detalle de discos ---
        ws2 = wb.create_sheet("Detalle discos")
        headers2 = ["Pedido", "Artista", "Titulo", "Sello", "Formato", "Precio UYU", "Costo UYU", "Estado", "Copias vendidas"]
        for col, h in enumerate(headers2, 1):
            c = ws2.cell(row=1, column=col, value=h)
            c.font = header_font
            c.fill = header_fill
            c.alignment = Alignment(horizontal="center")
        row = 2
        for p in pedidos:
            for d in p.discos:
                ws2.cell(row=row, column=1, value=p.numero or f"#{p.id}")
                ws2.cell(row=row, column=2, value=d.artista)
                ws2.cell(row=row, column=3, value=d.titulo)
                ws2.cell(row=row, column=4, value=d.sello or "")
                ws2.cell(row=row, column=5, value=d.formato or "")
                ws2.cell(row=row, column=6, value=round(d.precio_mostrar or d.precio_uyu or 0))
                ws2.cell(row=row, column=7, value=round(d.costo_final_uyu or 0))
                ws2.cell(row=row, column=8, value=d.estado)
                ws2.cell(row=row, column=9, value=d.copias_facturadas)
                row += 1
        for col in range(1, len(headers2) + 1):
            ws2.column_dimensions[openpyxl.utils.get_column_letter(col)].width = 16

        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        from flask import send_file
        return send_file(
            output,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name="arcano_pedidos.xlsx",
        )

    @app.route("/admin/pedidos/exportar/pdf")
    @auth.admin_required
    def pedidos_exportar_pdf():
        """Exporta todos los pedidos a PDF: resumen + detalle de discos."""
        from models import Pedido
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.units import cm
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        import io

        pedidos = Pedido.query.order_by(Pedido.fecha.desc()).all()
        output = io.BytesIO()
        doc = SimpleDocTemplate(output, pagesize=landscape(A4),
                                leftMargin=1*cm, rightMargin=1*cm, topMargin=1.2*cm, bottomMargin=1*cm)
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle("titulo", parent=styles["Title"], fontSize=18, textColor=colors.HexColor("#1a1a1a"))
        sub_style = ParagraphStyle("sub", parent=styles["Normal"], fontSize=9, textColor=colors.grey)
        h2_style = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=13)
        elems = []

        elems.append(Paragraph("Arcano Record Store &mdash; Pedidos", title_style))
        from datetime import datetime as _dt
        elems.append(Paragraph(f"Generado el {_dt.now().strftime('%d/%m/%Y')}", sub_style))
        elems.append(Spacer(1, 0.5*cm))

        # --- RESUMEN EJECUTIVO (KPIs globales) ---
        total_invertido = sum(p.costo_total_uyu for p in pedidos)
        total_recaudado = sum(p.recaudado_uyu for p in pedidos)
        total_discos = sum(p.discos_asignados for p in pedidos)
        total_vendidos = sum(p.discos_vendidos for p in pedidos)
        con_ventas = [p for p in pedidos if p.discos_vendidos > 0 and p.costo_total_uyu > 0]
        mejor = max(con_ventas, key=lambda p: p.margen_pct) if con_ventas else None
        peor = min(con_ventas, key=lambda p: p.margen_pct) if con_ventas else None
        recuperados = sum(1 for p in pedidos if p.recuperado)
        resultado_global = total_recaudado - total_invertido

        elems.append(Paragraph("Resumen ejecutivo", h2_style))
        elems.append(Spacer(1, 0.2*cm))

        def fmt(n):
            return f"{n:,.0f}".replace(",", ".")

        kpi_data = [
            ["Total pedidos", str(len(pedidos)), "Total invertido", f"{fmt(total_invertido)} UYU"],
            ["Total recaudado", f"{fmt(total_recaudado)} UYU", "Resultado global", f"{'+' if resultado_global >= 0 else ''}{fmt(resultado_global)} UYU"],
            ["Discos asignados", str(total_discos), "Discos vendidos", str(total_vendidos)],
            ["Pedidos recuperados", f"{recuperados} de {len(pedidos)}",
             "Mejor / Peor pedido",
             (f"{mejor.numero or mejor.id} ({mejor.margen_pct}%) / {peor.numero or peor.id} ({peor.margen_pct}%)" if mejor and peor else "-")],
        ]
        kpi_table = Table(kpi_data, colWidths=[4.5*cm, 5*cm, 4.5*cm, 6*cm])
        kpi_table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
            ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
            ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#666666")),
            ("TEXTCOLOR", (2, 0), (2, -1), colors.HexColor("#666666")),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f5f5f5")),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.white),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        elems.append(kpi_table)
        elems.append(Spacer(1, 0.7*cm))

        # --- Tabla resumen (con metricas) ---
        elems.append(Paragraph("Detalle por pedido", h2_style))
        elems.append(Spacer(1, 0.2*cm))
        data = [["Pedido", "Fecha", "Costo UYU", "Discos", "Vend.", "Rotac.", "Recaudado",
                 "Resultado", "Margen", "Proyec.", "Recup.", "Estado"]]
        for p in pedidos:
            data.append([
                p.numero or f"#{p.id}",
                p.fecha.strftime("%d/%m/%y") if p.fecha else "",
                fmt(p.costo_total_uyu),
                f"{p.discos_asignados}/{p.cant_discos}",
                str(p.discos_vendidos),
                f"{int(p.rotacion_pct)}%",
                fmt(p.recaudado_uyu),
                f"{'+' if p.resultado_uyu >= 0 else ''}{fmt(p.resultado_uyu)}",
                f"{p.margen_pct}%",
                f"{'+' if p.resultado_potencial_uyu >= 0 else ''}{fmt(p.resultado_potencial_uyu)}",
                f"{p.dias_recuperacion}d" if p.dias_recuperacion is not None else "-",
                "OK" if p.recuperado else "Pdte",
            ])
        t = Table(data, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a1a1a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
            ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        elems.append(t)
        elems.append(Spacer(1, 0.8*cm))

        # --- Detalle de discos por pedido ---
        elems.append(Paragraph("Discos por pedido", h2_style))
        elems.append(Spacer(1, 0.2*cm))
        for p in pedidos:
            if not p.discos:
                continue
            elems.append(Spacer(1, 0.3*cm))
            costo_extra = " (costo manual)" if p.usa_costo_manual else ""
            elems.append(Paragraph(
                f"<b>Pedido {p.numero or p.id}</b> &mdash; {p.proveedor} &mdash; "
                f"{p.discos_asignados} discos &mdash; {fmt(p.costo_total_uyu)} UYU{costo_extra} &mdash; "
                f"margen {p.margen_pct}%", sub_style))
            ddata = [["Artista", "Titulo", "Sello", "Precio", "Estado"]]
            for d in p.discos:
                ddata.append([
                    d.artista or "", d.titulo or "", d.sello or "",
                    fmt(d.precio_mostrar or d.precio_uyu or 0),
                    d.estado,
                ])
            dt = Table(ddata, repeatRows=1, colWidths=[5*cm, 6*cm, 4*cm, 3*cm, 3*cm])
            dt.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#444444")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 7),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#eeeeee")),
                ("ALIGN", (3, 0), (3, -1), "RIGHT"),
            ]))
            elems.append(dt)

        doc.build(elems)
        output.seek(0)
        from flask import send_file
        return send_file(
            output, mimetype="application/pdf",
            as_attachment=True, download_name="arcano_pedidos.pdf",
        )

    # ----------------------------------------------------------------------
    # Settings
    @app.route("/admin/diag-banner")
    @auth.admin_required
    def diag_banner():
        """Diagnostico temporal del banner: muestra que hay guardado y si el
        archivo existe. Sirve para depurar por que no se ve el banner."""
        url = Setting.get("public_banner_url", "")
        video = Setting.get("public_banner_video_url", "")
        covers_dir = app.config["COVERS_DIR"]
        info = [f"public_banner_url guardada: {url!r}"]
        info.append(f"public_banner_video_url: {video!r}")
        info.append(f"COVERS_DIR: {covers_dir}")
        if url:
            filename = url.rstrip("/").split("/")[-1].split("?")[0]
            info.append(f"filename extraido: {filename!r}")
            p1 = os.path.join(covers_dir, filename)
            info.append(f"existe en COVERS_DIR: {os.path.exists(p1)} ({p1})")
        # Listar archivos banner_ en el directorio
        try:
            archivos = [f for f in os.listdir(covers_dir) if "banner" in f.lower()]
            info.append(f"archivos con 'banner' en COVERS_DIR: {archivos}")
        except Exception as e:
            info.append(f"error listando COVERS_DIR: {e}")
        info.append(f"_banner_url_si_existe() devuelve: {_banner_url_si_existe()!r}")
        return "<pre>" + "\n".join(info) + "</pre>"

    @app.route("/admin/backup/db")
    @auth.admin_required
    def backup_db():
        """Descarga la base de datos SQLite completa (discos, ventas, reservas,
        clientes, egresos, pedidos, todo). Para el backup semanal."""
        from flask import send_file
        db_uri = app.config["SQLALCHEMY_DATABASE_URI"]
        db_file = db_uri.replace("sqlite:///", "")
        if not os.path.isabs(db_file):
            db_file = os.path.join(app.instance_path, db_file)
        if not os.path.exists(db_file):
            flash("No se encontro el archivo de la base de datos.", "error")
            return redirect(url_for("settings_page"))
        fecha = datetime.utcnow().strftime("%Y%m%d")
        return send_file(
            db_file, as_attachment=True,
            download_name=f"arcano_backup_{fecha}.db",
            mimetype="application/octet-stream",
        )

    @app.route("/admin/backup/covers")
    @auth.admin_required
    def backup_covers():
        """Descarga todas las portadas en un ZIP."""
        import zipfile
        import io as _io
        covers_dir = app.config.get("COVERS_DIR")
        if not covers_dir or not os.path.isdir(covers_dir):
            flash("No se encontro la carpeta de portadas.", "error")
            return redirect(url_for("settings_page"))
        buf = _io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname in os.listdir(covers_dir):
                fpath = os.path.join(covers_dir, fname)
                if os.path.isfile(fpath):
                    zf.write(fpath, arcname=fname)
        buf.seek(0)
        from flask import send_file
        fecha = datetime.utcnow().strftime("%Y%m%d")
        return send_file(
            buf, as_attachment=True,
            download_name=f"arcano_covers_{fecha}.zip",
            mimetype="application/zip",
        )

    @app.route("/admin/settings")
    @auth.admin_required
    def settings_page():
        tc = Setting.get_tc()
        tc_row = Setting.query.filter_by(key="tc_eur_uyu").first()

        discos_disponibles = Disc.query.filter_by(estado="Disponible").count()
        total_discos = Disc.query.count()
        discos_por_estado = (
            db.session.query(Disc.estado, db.func.count(Disc.id))
            .group_by(Disc.estado)
            .all()
        )

        # API tracking
        api_cost_total = Setting.get("api_cost_total_usd", "0")
        api_calls_count = Setting.get("api_calls_count", "0")
        try:
            api_cost_total = float(api_cost_total)
        except (TypeError, ValueError):
            api_cost_total = 0.0
        try:
            api_calls_count = int(api_calls_count)
        except (TypeError, ValueError):
            api_calls_count = 0

        return render_template(
            "settings.html",
            tc=tc,
            tc_updated_at=tc_row.updated_at if tc_row else None,
            discos_disponibles=discos_disponibles,
            total_discos=total_discos,
            discos_por_estado=discos_por_estado,
            api_cost_total=api_cost_total,
            api_calls_count=api_calls_count,
            api_key_set=bool(config.ANTHROPIC_API_KEY),
            # Textos publicos
            public_tagline=Setting.get("public_tagline", ""),
            public_anuncio_texto=Setting.get("public_anuncio_texto", ""),
            public_about=Setting.get("public_about", ""),
            public_about_curaduria=Setting.get("public_about_curaduria", ""),
            public_about_como_funciona=Setting.get("public_about_como_funciona", ""),
            public_envios=Setting.get("public_envios", ""),
            public_instagram=Setting.get("public_instagram", ""),
            public_whatsapp=Setting.get("public_whatsapp", ""),
            analytics_snippet=Setting.get("analytics_snippet", ""),
            tip_label=Setting.get("tip_label", "Tip"),
            carrusel_preorder=Setting.get("carrusel_preorder", "1") == "1",
            carrusel_tips=Setting.get("carrusel_tips", "1") == "1",
            carrusel_escuchados=Setting.get("carrusel_escuchados", "1") == "1",
            carrusel_destacados=Setting.get("carrusel_destacados", "1") == "1",
            carrusel_nuevos=Setting.get("carrusel_nuevos", "1") == "1",
            mercadopago_activo=Setting.get("mercadopago_activo", "0") == "1",
            mp_recargo_pct=Setting.get("mp_recargo_pct", "7"),
            mp_credencial_ok=bool(mp.access_token()),
            mp_sandbox=mp.es_sandbox(),
            transferencia_activa=Setting.get("transferencia_activa", "0") == "1",
            transferencia_datos=Setting.get("transferencia_datos", ""),
            public_banner_url=Setting.get("public_banner_url", ""),
            public_hero_title=Setting.get("public_hero_title", ""),
            public_hero_subtitle=Setting.get("public_hero_subtitle", ""),
            public_banner_video_url=Setting.get("public_banner_video_url", ""),
            public_legal_terminos=Setting.get("public_legal_terminos", ""),
            public_legal_envios=Setting.get("public_legal_envios", ""),
            public_legal_privacidad=Setting.get("public_legal_privacidad", ""),
            public_legal_cookies=Setting.get("public_legal_cookies", ""),
            public_grid_columnas=Setting.get("public_grid_columnas", "auto"),
            public_grid_mobile=Setting.get("public_grid_mobile", "1"),
            public_discos_por_pagina=Setting.get("public_discos_por_pagina", "24"),
            public_carrusel_velocidad=Setting.get("public_carrusel_velocidad", "7"),
            public_card_tamano=Setting.get("public_card_tamano", "mediano"),
            # SMTP status
            smtp_configured=mailer.is_configured(),
        )

    @app.route("/admin/settings/tc", methods=["POST"])
    @auth.admin_required
    def settings_update_tc():
        nuevo_tc = _parse_float(request.form.get("tc_eur_uyu"), default=0.0)
        if nuevo_tc <= 0:
            flash("El tipo de cambio debe ser un numero mayor a 0.", "error")
            return redirect(url_for("settings_page"))
        if nuevo_tc > 10000:
            flash("El tipo de cambio parece demasiado alto. Verificalo.", "error")
            return redirect(url_for("settings_page"))

        anterior = Setting.get_tc()
        Setting.set("tc_eur_uyu", str(nuevo_tc))
        flash(
            f"Tipo de cambio actualizado: {anterior} -> {nuevo_tc}. "
            f"Los discos existentes mantienen su costo final original.",
            "success",
        )
        return redirect(url_for("settings_page"))

    @app.route("/admin/settings/recalculate", methods=["POST"])
    @auth.admin_required
    def settings_recalculate():
        tc_actual = Setting.get_tc()
        discos = Disc.query.filter_by(estado="Disponible").all()
        recalculados = 0
        for d in discos:
            d.tc_usado = tc_actual
            d.recalcular_costo_final()
            recalculados += 1
        db.session.commit()
        flash(
            f"Recalculados {recalculados} discos disponibles con TC = {tc_actual}.",
            "success",
        )
        return redirect(url_for("settings_page"))

    @app.route("/admin/settings/reset-api-counter", methods=["POST"])
    @auth.admin_required
    def settings_reset_api_counter():
        Setting.set("api_cost_total_usd", "0")
        Setting.set("api_calls_count", "0")
        flash("Contador de gasto API reiniciado.", "success")
        return redirect(url_for("settings_page"))

    # ----------------------------------------------------------------------
    # Exportacion CSV
    # ----------------------------------------------------------------------

    @app.route("/admin/export/csv")
    @auth.admin_required
    def export_csv():
        """Exporta toda la DB de discos como CSV con UTF-8 BOM (compatible Excel ES)."""
        discs = Disc.query.order_by(Disc.fecha_ingreso.asc()).all()

        # Construir CSV en memoria
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=",", quoting=csv.QUOTE_MINIMAL)

        # Encabezados
        writer.writerow([
            "id",
            "artista",
            "titulo",
            "sello",
            "anio",
            "formato",
            "genero",
            "tracklist",
            "estado",
            "condicion",
            "stock",
            "costo_eur",
            "envio_eur",
            "tc_usado",
            "costo_final_uyu",
            "precio_uyu",
            "ganancia_uyu",
            "margen_pct",
            "notas",
            "cover_url",
            "cover_local",
            "sample_url",
            "source_url",
            "fecha_ingreso",
            "fecha_reserva",
            "fecha_venta",
            "fecha_modificacion",
        ])

        def _fdate(dt):
            return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""

        for d in discs:
            writer.writerow([
                d.id,
                d.artista or "",
                d.titulo or "",
                d.sello or "",
                d.anio or "",
                d.formato or "",
                d.genero or "",
                # Reemplazar saltos de linea del tracklist por " | " para que entre en una celda
                (d.tracklist or "").replace("\r\n", " | ").replace("\n", " | "),
                d.estado or "",
                d.condicion or "",
                d.stock or 0,
                f"{d.costo_eur or 0:.2f}",
                f"{d.envio_eur or 0:.2f}",
                f"{d.tc_usado or 0:.2f}",
                f"{d.costo_final_uyu or 0:.2f}",
                f"{d.precio_uyu or 0:.2f}",
                f"{d.ganancia_uyu or 0:.2f}",
                f"{d.margen_pct or 0:.2f}",
                (d.notas or "").replace("\r\n", " | ").replace("\n", " | "),
                d.cover_url or "",
                d.cover_local or "",
                d.sample_url or "",
                d.source_url or "",
                _fdate(d.fecha_ingreso),
                _fdate(d.fecha_reserva),
                _fdate(d.fecha_venta),
                _fdate(d.fecha_modificacion),
            ])

        # UTF-8 BOM para que Excel en español lo abra correctamente
        csv_data = "\ufeff" + buffer.getvalue()
        buffer.close()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"arcano_stock_{timestamp}.csv"

        return Response(
            csv_data,
            mimetype="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f"attachment; filename={filename}",
            },
        )

    # ----------------------------------------------------------------------
    # Debug
    # ----------------------------------------------------------------------

    @app.route("/admin/debug/discs")
    @auth.admin_required
    def debug_discs():
        discs = Disc.query.order_by(Disc.fecha_ingreso.desc()).all()
        return jsonify({
            "count": len(discs),
            "tc_eur_uyu": Setting.get_tc(),
            "discs": [d.to_dict() for d in discs],
        })

    @app.route("/admin/debug/settings")
    @auth.admin_required
    def debug_settings():
        rows = Setting.query.all()
        return jsonify({
            row.key: {
                "value": row.value,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in rows
        })

    @app.route("/admin/debug/history/<int:disc_id>")
    @auth.admin_required
    def debug_history(disc_id: int):
        disc = Disc.query.get_or_404(disc_id)
        return jsonify({
            "disc_id": disc.id,
            "artista": disc.artista,
            "titulo": disc.titulo,
            "history": [h.to_dict() for h in disc.history],
        })

    # ----------------------------------------------------------------------
    # TIENDA PUBLICA
    # ----------------------------------------------------------------------

    def _banner_url_si_existe():
        """Devuelve la URL del banner solo si el archivo existe en disco.
        Evita mostrar una imagen rota si el banner se configuro pero el
        archivo no esta (ej: se perdio en una migracion)."""
        url = Setting.get("public_banner_url", "")
        if not url:
            return ""
        # Si es una URL externa (http), devolverla tal cual
        if url.startswith("http://") or url.startswith("https://"):
            return url
        # Extraer el filename del final de la ruta (soporta /covers/x.jpg,
        # /static/covers/x.jpg, etc.)
        filename = url.rstrip("/").split("/")[-1].split("?")[0]
        if not filename:
            return ""
        # Buscar el archivo en las ubicaciones posibles
        posibles = [
            os.path.join(app.config["COVERS_DIR"], filename),
            os.path.join(app.static_folder, "covers", filename),
        ]
        for path in posibles:
            if os.path.exists(path):
                # Normalizar la URL para servir siempre via serve_cover
                return url_for("serve_cover", filename=filename)
        return ""

    def _public_context():
        """Contexto comun para todas las paginas publicas."""
        return {
            "tagline": Setting.get("public_tagline", ""),
            "about": Setting.get("public_about", ""),
            "about_curaduria": Setting.get("public_about_curaduria", ""),
            "about_como_funciona": Setting.get("public_about_como_funciona", ""),
            "envios": Setting.get("public_envios", ""),
            "instagram": Setting.get("public_instagram", ""),
            "whatsapp": Setting.get("public_whatsapp", ""),
            "analytics_snippet": Setting.get("analytics_snippet", ""),
            "tip_label": Setting.get("tip_label", "Tip"),
            "banner_url": _banner_url_si_existe(),
            "anuncio_texto": Setting.get("public_anuncio_texto", ""),
            "hero_title": Setting.get("public_hero_title", ""),
            "hero_subtitle": Setting.get("public_hero_subtitle", ""),
            "banner_video_url": Setting.get("public_banner_video_url", ""),
            "legal_terminos": Setting.get("public_legal_terminos", ""),
            "legal_envios": Setting.get("public_legal_envios", ""),
            "legal_privacidad": Setting.get("public_legal_privacidad", ""),
            "legal_cookies": Setting.get("public_legal_cookies", ""),
            "grid_columnas": Setting.get("public_grid_columnas", "auto"),
            "grid_mobile": Setting.get("public_grid_mobile", "1"),
            "carrusel_velocidad": Setting.get("public_carrusel_velocidad", "7"),
            "card_tamano": Setting.get("public_card_tamano", "mediano"),
            "mp_activo": mp.activo(),
            "mp_recargo_pct": mp.recargo_pct(),
            "transferencia_activa": Setting.get("transferencia_activa", "0") == "1",
            "transferencia_datos": Setting.get("transferencia_datos", ""),
        }

    def _guardar_comprobante(reserva, file):
        """Guarda el comprobante de transferencia (imagen o PDF), si vino uno."""
        if not file or not file.filename:
            return
        media = (file.mimetype or "").lower()
        ext_map = {
            "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
            "image/gif": ".gif", "application/pdf": ".pdf",
        }
        if media not in ext_map:
            return
        content = file.read()
        if len(content) > 12 * 1024 * 1024:  # 12 MB
            return
        import uuid as _uuid
        fname = f"comp_{reserva.id}_{_uuid.uuid4().hex[:8]}{ext_map[media]}"
        d = app.config["COMPROBANTES_DIR"]
        os.makedirs(d, exist_ok=True)
        try:
            with open(os.path.join(d, fname), "wb") as fh:
                fh.write(content)
            reserva.comprobante_file = fname
        except Exception:
            pass

    def _mp_iniciar_pago(reserva, discos):
        """Crea la preferencia de Mercado Pago para una reserva y devuelve la URL
        de pago (o None si algo falla; el caller cae al flujo normal de reserva)."""
        subtotal = sum((d.precio_uyu or 0) for d in discos)
        if subtotal <= 0:
            return None
        recargo = mp.calcular_recargo(subtotal)
        items = [
            {"title": f"{d.artista} - {d.titulo}", "quantity": 1,
             "unit_price": float(d.precio_uyu or 0)}
            for d in discos
        ]
        if recargo > 0:
            items.append({
                "title": f"Recargo pago online ({mp.recargo_pct():.0f}%)",
                "quantity": 1, "unit_price": float(recargo),
            })
        base = request.url_root.rstrip("/")
        back = f"{base}/mp/retorno?reserva={reserva.id}"
        try:
            pref = mp.crear_preferencia(
                items,
                back_urls={"success": back, "failure": back, "pending": back},
                notification_url=f"{base}/mp/webhook",
                external_reference=reserva.id,
                payer_email=reserva.email,
            )
        except Exception as ex:
            app.logger.warning(f"MP: no se pudo crear la preferencia: {ex}")
            return None
        reserva.mp_preference_id = str(pref.get("id") or "")
        reserva.total_cobrado_uyu = subtotal + recargo
        reserva.mp_status = "pending"
        db.session.commit()
        return mp.url_checkout(pref)

    def _mp_completar_reserva(reserva, payment_id=None, status="approved"):
        """Marca la reserva como pagada/completada y sus discos como Vendido.
        Idempotente: si ya estaba pagada, no vuelve a procesar."""
        if reserva.pagado:
            return
        ahora = datetime.utcnow()
        for item in reserva.items_efectivos:
            d = Disc.query.get(item.disc_id) if getattr(item, "disc_id", None) else None
            if d and d.estado != "Vendido":
                d.estado = "Vendido"
                if not d.fecha_venta:
                    d.fecha_venta = ahora
                if not getattr(d, "copias_vendidas", 0):
                    d.copias_vendidas = 1
                d.stock = max(0, (d.stock or 1) - 1)
        reserva.pagado = True
        reserva.estado = "Completada"
        if payment_id:
            reserva.mp_payment_id = str(payment_id)
        reserva.mp_status = status
        db.session.commit()
        # Email de confirmacion (best-effort)
        if reserva.email and mailer.can_send():
            try:
                discos = [Disc.query.get(i.disc_id) for i in reserva.items_efectivos]
                mailer.send_reserva_confirmacion_cliente_multi(
                    reserva, [d for d in discos if d])
            except Exception:
                pass

    @app.route("/mp/webhook", methods=["POST", "GET"])
    def mp_webhook():
        """Mercado Pago avisa aca cuando cambia el estado de un pago. Confirmamos
        contra la API de MP y, si esta aprobado, completamos la reserva."""
        payment_id = (request.args.get("data.id") or request.args.get("id") or "")
        if not payment_id:
            data = request.get_json(silent=True) or {}
            payment_id = str((data.get("data") or {}).get("id") or data.get("id") or "")
        if not payment_id:
            return "", 200  # nada que procesar
        pago = mp.obtener_pago(payment_id)
        if not pago:
            return "", 200
        estado = pago.get("status")  # approved / pending / rejected
        ref = pago.get("external_reference")
        reserva = Reserva.query.get(int(ref)) if ref and str(ref).isdigit() else None
        if reserva:
            if estado == "approved":
                _mp_completar_reserva(reserva, payment_id=payment_id, status=estado)
            else:
                reserva.mp_status = estado
                reserva.mp_payment_id = str(payment_id)
                db.session.commit()
        return "", 200

    @app.route("/mp/retorno")
    def mp_retorno():
        """Pagina a la que vuelve el cliente despues de pagar en MP. La confirmacion
        principal la hace el webhook; aca confirmamos tambien como respaldo por si
        el webhook se demora o no llega."""
        reserva = None
        rid = request.args.get("reserva") or request.args.get("external_reference")
        if rid and str(rid).isdigit():
            reserva = Reserva.query.get(int(rid))
        estado_mp = request.args.get("status") or request.args.get("collection_status")
        payment_id = request.args.get("payment_id") or request.args.get("collection_id")
        # Respaldo idempotente: si volvio aprobado, confirmamos aca tambien.
        # Verificamos que el pago corresponda a ESTA reserva (external_reference),
        # para que nadie confirme una reserva ajena pasando un payment_id cualquiera.
        if reserva and estado_mp == "approved" and payment_id and not reserva.pagado:
            pago = mp.obtener_pago(payment_id)
            if (pago and pago.get("status") == "approved"
                    and str(pago.get("external_reference")) == str(reserva.id)):
                _mp_completar_reserva(reserva, payment_id=payment_id, status="approved")
        if not estado_mp and reserva:
            estado_mp = reserva.mp_status
        return render_template(
            "public/mp_retorno.html",
            reserva=reserva,
            estado_mp=estado_mp,
            **_public_context(),
        )

    @app.route("/api/buscar-sugerencias")
    def buscar_sugerencias():
        """Sugerencias de autocompletado para el buscador (artistas, sellos, titulos)."""
        q = (request.args.get("q") or "").strip()
        if len(q) < 2:
            return jsonify([])
        like = f"%{q}%"
        disponibles = Disc.query.filter(Disc.estado == "Disponible", Disc.stock > 0)
        # Recolectar coincidencias unicas de artista, sello y titulo
        sugerencias = []
        vistos = set()
        for d in disponibles.filter(
            or_(Disc.artista.ilike(like), Disc.titulo.ilike(like), Disc.sello.ilike(like))
        ).limit(30).all():
            for valor, tipo in [(d.artista, "artista"), (d.sello, "sello")]:
                if valor and q.lower() in valor.lower():
                    key = valor.lower()
                    if key not in vistos:
                        vistos.add(key)
                        sugerencias.append({"texto": valor, "tipo": tipo})
            # El disco mismo como resultado directo
            titulo_full = f"{d.artista} - {d.titulo}"
            if titulo_full.lower() not in vistos:
                vistos.add(titulo_full.lower())
                sugerencias.append({"texto": titulo_full, "tipo": "disco", "id": d.id})
        return jsonify(sugerencias[:8])

    @app.route("/robots.txt")
    def robots():
        """Instrucciones para buscadores."""
        base = request.url_root.rstrip("/")
        lines = [
            "User-agent: *",
            "Allow: /",
            "Disallow: /admin/",
            "Disallow: /cuenta/",
            "Disallow: /carrito",
            "Disallow: /checkout",
            "",
            f"Sitemap: {base}/sitemap.xml",
        ]
        return Response("\n".join(lines), mimetype="text/plain")

    @app.route("/")
    def home():
        """Catalogo publico de discos disponibles."""
        from sqlalchemy import or_ as _or
        from sqlalchemy.orm import selectinload as _selectinload
        all_disponibles = (
            Disc.query
            .options(_selectinload(Disc.tracks))
            .filter(
                _or(
                    # Disponibles con stock
                    (Disc.estado == "Disponible") & (Disc.stock > 0),
                    # O agotados que el admin eligio mostrar igual
                    Disc.mostrar_agotado == True,  # noqa: E712
                    # O en estado Pre-order (se reservan por adelantado)
                    Disc.estado == "Pre-order",
                )
            )
            .all()
        )
        discs = list(all_disponibles)

        # Filtros
        q = (request.args.get("q") or "").strip()
        sellos_sel = [s.strip() for s in request.args.getlist("sello") if s.strip()]
        genero_sel = [g.strip() for g in request.args.getlist("genero") if g.strip()]
        tags_sel = [t.strip() for t in request.args.getlist("tag") if t.strip()]
        formato_sel = [f.strip() for f in request.args.getlist("formato") if f.strip()]
        pais_sel = [p.strip() for p in request.args.getlist("pais") if p.strip()]
        decada_sel = [d.strip() for d in request.args.getlist("decada") if d.strip()]
        orden = (request.args.get("orden") or "nuevos").strip()
        con_sample = request.args.get("con_sample") == "1"
        precio_min = _parse_int(request.args.get("precio_min"), None)
        precio_max = _parse_int(request.args.get("precio_max"), None)

        def _decada(anio):
            if not anio:
                return None
            return f"{(anio // 10) * 10}s"

        if q:
            q_lower = q.lower()
            discs = [
                d for d in discs
                if q_lower in (d.artista or "").lower()
                or q_lower in (d.titulo or "").lower()
                or q_lower in (d.sello or "").lower()
                or q_lower in (d.genero or "").lower()
            ]
        if sellos_sel:
            sl = {s.lower() for s in sellos_sel}
            discs = [d for d in discs if (d.sello or "").lower() in sl]
        if genero_sel:
            gl = {g.lower() for g in genero_sel}
            discs = [d for d in discs
                     if any(part.strip().lower() in gl for part in (d.genero or "").split(","))]
        if tags_sel:
            tl = {t.lower() for t in tags_sel}
            discs = [d for d in discs if any(tag.lower() in tl for tag in d.tags_list)]
        if formato_sel:
            fl = {f.lower() for f in formato_sel}
            discs = [d for d in discs if (d.formato or "").lower() in fl]
        if pais_sel:
            pl = {p.lower() for p in pais_sel}
            discs = [d for d in discs if (d.pais or "").lower() in pl]
        if decada_sel:
            dl = set(decada_sel)
            discs = [d for d in discs if _decada(d.anio) in dl]
        if con_sample:
            discs = [d for d in discs if d.tracks and any(t.has_sample for t in d.tracks)]
        if precio_min is not None:
            discs = [d for d in discs if (d.precio_uyu or 0) >= precio_min]
        if precio_max is not None:
            discs = [d for d in discs if (d.precio_uyu or 0) <= precio_max]

        # Ordenamiento
        if orden == "precio_asc":
            discs.sort(key=lambda d: d.precio_uyu or 0)
        elif orden == "precio_desc":
            discs.sort(key=lambda d: d.precio_uyu or 0, reverse=True)
        elif orden == "artista":
            discs.sort(key=lambda d: (d.artista or "").lower())
        elif orden == "fecha":
            discs.sort(key=lambda d: d.fecha_ingreso or datetime.min, reverse=True)
        else:
            # Por defecto: el orden manual que el admin arma arrastrando en el
            # dashboard (mayor = primero). Los discos nuevos entran arriba.
            discs.sort(key=lambda d: (d.orden if d.orden is not None else 0), reverse=True)

        # Paginacion (sobre la lista ya filtrada y ordenada)
        # Configurable desde Ajustes (public_discos_por_pagina), con tope de 48.
        try:
            POR_PAGINA = int(Setting.get("public_discos_por_pagina", "24") or 24)
        except (ValueError, TypeError):
            POR_PAGINA = 24
        POR_PAGINA = max(4, min(POR_PAGINA, 48))
        total_resultados = len(discs)
        total_paginas = max(1, (total_resultados + POR_PAGINA - 1) // POR_PAGINA)
        pagina = _parse_int(request.args.get("pagina"), 1) or 1
        if pagina < 1:
            pagina = 1
        if pagina > total_paginas:
            pagina = total_paginas
        inicio = (pagina - 1) * POR_PAGINA
        discs_pagina = discs[inicio:inicio + POR_PAGINA]

        # Facetas con contadores (a partir de TODO el catalogo disponible)
        from collections import Counter
        sellos_count = Counter()
        generos_count = Counter()
        tags_count = Counter()
        formatos_count = Counter()
        paises_count = Counter()
        decadas_count = Counter()
        precios = []
        for d in all_disponibles:
            if d.sello:
                sellos_count[d.sello] += 1
            for g in (d.genero or "").split(","):
                if g.strip():
                    generos_count[g.strip()] += 1
            for tg in d.tags_list:
                tags_count[tg] += 1
            if d.formato:
                formatos_count[d.formato] += 1
            if d.pais:
                paises_count[d.pais] += 1
            dc = _decada(d.anio)
            if dc:
                decadas_count[dc] += 1
            if d.precio_uyu and d.precio_uyu > 0:
                precios.append(d.precio_uyu)

        sellos = sorted(sellos_count.keys())
        sellos_destacados = sellos_count.most_common(8)
        generos = sorted(generos_count.items())
        tags_disponibles = sorted(tags_count.items())
        formatos = sorted(formatos_count.items())
        paises = sorted(paises_count.items())
        decadas = sorted(decadas_count.items(), reverse=True)
        precio_rango = {
            "min": int(min(precios)) if precios else 0,
            "max": int(max(precios)) if precios else 0,
        }

        filtros_activos = sum([
            bool(sellos_sel), bool(genero_sel), bool(tags_sel),
            bool(formato_sel), bool(pais_sel), bool(decada_sel),
            con_sample, precio_min is not None, precio_max is not None,
        ])

        # Secciones de la home: solo en pagina 1, sin filtros ni busqueda
        destacados = []
        nuevos_ingresos = []
        preorders_carrusel = []
        if pagina == 1 and filtros_activos == 0 and not q and orden == "nuevos":
            # Para los carruseles solo discos realmente disponibles (o pre-order).
            # Los vendidos/agotados NO entran, ni siquiera para completar al azar.
            disponibles_carrusel = [
                d for d in all_disponibles
                if not d.esta_agotado or d.mostrar_preorder
            ]
            # --- DESTACADOS ---
            # Los marcados manualmente tienen prioridad. Si faltan para 6,
            # se completan con aleatorios que rotan cada 10 dias.
            marcados_dest = [d for d in disponibles_carrusel if d.destacado]
            destacados = list(marcados_dest[:6])
            if len(destacados) < 6:
                import random as _random
                # Semilla estable por periodo de 10 dias
                periodo = int(datetime.utcnow().timestamp() // (10 * 86400))
                resto = [d for d in disponibles_carrusel if not d.destacado]
                rng = _random.Random(periodo)
                rng.shuffle(resto)
                destacados += resto[: 6 - len(destacados)]

            # --- NUEVOS INGRESOS ---
            # Los marcados manualmente van primero, despues los mas recientes
            # por fecha de ingreso, hasta completar 6.
            marcados_nuevos = [d for d in disponibles_carrusel if d.en_nuevos]
            por_fecha = sorted(
                disponibles_carrusel,
                key=lambda d: d.fecha_ingreso or datetime.min,
                reverse=True,
            )
            nuevos_ingresos = list(marcados_nuevos[:6])
            ids_ya = {d.id for d in nuevos_ingresos}
            for d in por_fecha:
                if len(nuevos_ingresos) >= 6:
                    break
                if d.id not in ids_ya:
                    nuevos_ingresos.append(d)
                    ids_ya.add(d.id)

            # --- PRE-ORDER ---
            # Discos en estado Pre-order con el toggle de carrusel prendido
            preorders_carrusel = [d for d in all_disponibles if d.en_carrusel_preorder][:12]

        # --- TIPS DEL CURADOR ---
        # Discos marcados como tip, disponibles. Se muestra siempre que haya.
        tips_carrusel = []
        if pagina == 1 and filtros_activos == 0 and not q and orden == "nuevos":
            tips_carrusel = [
                d for d in all_disponibles
                if d.es_tip and (not d.esta_agotado or d.mostrar_preorder)
            ][:12]

        # --- LO MAS ESCUCHADO (ultimos 30 dias) ---
        mas_escuchados = []
        if pagina == 1 and filtros_activos == 0 and not q and orden == "nuevos":
            from models import PlayEvent
            from sqlalchemy import func as _func
            hace_30 = datetime.utcnow() - timedelta(days=30)
            plays_por_disco = dict(
                db.session.query(PlayEvent.disc_id, _func.count(PlayEvent.id))
                .filter(PlayEvent.fecha >= hace_30)
                .group_by(PlayEvent.disc_id)
                .all()
            )
            if plays_por_disco:
                con_plays = [
                    (d, plays_por_disco.get(d.id, 0))
                    for d in all_disponibles
                    if plays_por_disco.get(d.id, 0) > 0
                    and (not d.esta_agotado or d.mostrar_preorder)
                ]
                con_plays.sort(key=lambda x: x[1], reverse=True)
                mas_escuchados = [d for d, n in con_plays[:6]]

        return render_template(
            "public/home.html",
            discs=discs_pagina,
            destacados=destacados,
            nuevos_ingresos=nuevos_ingresos,
            preorders_carrusel=preorders_carrusel,
            tips_carrusel=tips_carrusel,
            mas_escuchados=mas_escuchados,
            car_preorder=Setting.get("carrusel_preorder", "1") == "1",
            car_tips=Setting.get("carrusel_tips", "1") == "1",
            car_escuchados=Setting.get("carrusel_escuchados", "1") == "1",
            car_destacados=Setting.get("carrusel_destacados", "1") == "1",
            car_nuevos=Setting.get("carrusel_nuevos", "1") == "1",
            total_resultados=total_resultados,
            pagina=pagina,
            total_paginas=total_paginas,
            sellos=sellos,
            sellos_destacados=sellos_destacados,
            generos=generos,
            tags_disponibles=tags_disponibles,
            formatos=formatos,
            paises=paises,
            decadas=decadas,
            sellos_count=sellos_count,
            precio_rango=precio_rango,
            filtro_q=q,
            filtro_sellos=sellos_sel,
            filtro_generos=genero_sel,
            filtro_tags=tags_sel,
            filtro_formatos=formato_sel,
            filtro_paises=pais_sel,
            filtro_decadas=decada_sel,
            filtro_orden=orden,
            filtro_con_sample=con_sample,
            filtro_precio_min=precio_min,
            filtro_precio_max=precio_max,
            filtros_activos=filtros_activos,
            **_public_context(),
        )

    @app.route("/api/track/<int:track_id>/play", methods=["POST"])
    def api_track_play(track_id: int):
        """Registra una reproduccion de sample (la llama el player al dar play).

        Incrementa el contador total del track y guarda el evento con fecha
        para el chart de 'lo mas escuchado del mes'. Publica, sin auth.
        """
        from models import Track, PlayEvent
        track = Track.query.get(track_id)
        if not track:
            return jsonify({"ok": False}), 404
        track.play_count = (track.play_count or 0) + 1
        db.session.add(PlayEvent(track_id=track.id, disc_id=track.disc_id))
        db.session.commit()
        return jsonify({"ok": True, "plays": track.play_count})

    @app.route("/disco/<int:disc_id>")
    def public_disc(disc_id: int):
        """Ficha publica de un disco."""
        disc = Disc.query.get_or_404(disc_id)
        # No mostrar discos que no esten disponibles publicamente
        # (excepcion: los pre-orders se ven siempre)
        if disc.estado not in ("Disponible", "Pre-order", "Reservado", "Vendido"):
            return render_template("public/not_found.html", **_public_context()), 404

        # "Similares a este titulo": prioriza mismo genero, completa con mismo
        # sello y luego otros disponibles. Asi siempre hay sugerencias relevantes.
        relacionados = []
        vistos = {disc.id}

        def _agregar(query, faltan):
            nuevos = []
            for d in query.limit(faltan * 3).all():
                if d.id not in vistos:
                    vistos.add(d.id)
                    nuevos.append(d)
                    if len(nuevos) >= faltan:
                        break
            return nuevos

        base = (
            Disc.query
            .filter(Disc.id != disc.id)
            .filter(Disc.estado == "Disponible")
            .filter(Disc.stock > 0)
        )

        # 1. Mismo genero (lo mas relevante para diggear)
        if disc.genero:
            relacionados += _agregar(
                base.filter(Disc.genero == disc.genero).order_by(Disc.fecha_ingreso.desc()),
                4 - len(relacionados),
            )
        # 2. Completar con mismo sello
        if len(relacionados) < 4 and disc.sello:
            relacionados += _agregar(
                base.filter(Disc.sello == disc.sello).order_by(Disc.fecha_ingreso.desc()),
                4 - len(relacionados),
            )
        # 3. Completar con otros recientes si aun faltan
        if len(relacionados) < 4:
            relacionados += _agregar(
                base.order_by(Disc.fecha_ingreso.desc()),
                4 - len(relacionados),
            )

        return render_template(
            "public/disc.html",
            disc=disc,
            relacionados=relacionados,
            **_public_context(),
        )

    @app.route("/reservar/<int:disc_id>", methods=["GET", "POST"])
    def public_reservar(disc_id: int):
        """Form de reserva."""
        disc = Disc.query.get_or_404(disc_id)
        # Los pre-orders se pueden reservar aunque no tengan stock todavia.
        # Los pre-orders se pueden reservar aunque no tengan stock todavia.
        if disc.estado == "Pre-order":
            pass  # permitido
        elif disc.estado != "Disponible" or (disc.stock or 0) <= 0:
            return render_template(
                "public/not_available.html",
                disc=disc,
                **_public_context(),
            ), 400

        if request.method == "POST":
            nombre = (request.form.get("nombre") or "").strip()[:120]
            whatsapp = (request.form.get("whatsapp") or "").strip()[:40]
            email = ((request.form.get("email") or "").strip()[:200]) or None
            nota = ((request.form.get("nota") or "").strip()[:1000]) or None

            errores = []
            if len(nombre) < 2:
                errores.append("Ingresa tu nombre.")
            if len(whatsapp) < 7:
                errores.append("Ingresa un WhatsApp valido (con codigo de pais).")
            if not email:
                errores.append("Ingresa tu email para enviarte la confirmacion.")
            elif "@" not in email:
                errores.append("El email no parece valido.")

            if errores:
                for e in errores:
                    flash(e, "error")
                return render_template(
                    "public/reservar.html",
                    disc=disc,
                    form={"nombre": nombre, "whatsapp": whatsapp, "email": email, "nota": nota},
                    **_public_context(),
                )

            # Crear la reserva (con item; el disco NO cambia de estado hasta confirmar)
            reserva = Reserva(
                nombre=nombre,
                whatsapp=whatsapp,
                email=email,
                nota=nota,
                estado="Pendiente",
                cliente_id=auth.cliente_actual_id(),
            )
            db.session.add(reserva)
            db.session.flush()

            item = ReservaItem(
                reserva_id=reserva.id,
                disc_id=disc.id,
                artista_snap=disc.artista,
                titulo_snap=disc.titulo,
                precio_uyu_snap=disc.precio_mostrar,
            )
            db.session.add(item)
            db.session.commit()

            # Mandar email al admin (opcional, si SMTP esta configurado)
            if mailer.is_configured():
                try:
                    mailer.send_reserva_notification(reserva, disc)
                except Exception:
                    pass

            # Mandar confirmacion al cliente (si dejo email)
            if reserva.email and mailer.can_send():
                try:
                    mailer.send_reserva_confirmacion_cliente(reserva, disc)
                except Exception:
                    pass

            return render_template(
                "public/reserva_ok.html",
                disc=disc,
                reserva=reserva,
                **_public_context(),
            )

        # Si el cliente esta logueado, precargar sus datos para no pedirlos de nuevo
        cliente = None
        if auth.cliente_actual_id():
            cliente = Cliente.query.get(auth.cliente_actual_id())

        return render_template(
            "public/reservar.html",
            disc=disc,
            form={
                "nombre": cliente.nombre if cliente else "",
                "email": cliente.email if cliente else "",
                "whatsapp": cliente.whatsapp if cliente else "",
            },
            cliente=cliente,
            **_public_context(),
        )

    @app.route("/sobre")
    def public_sobre():
        return render_template("public/sobre.html", **_public_context())

    @app.route("/legal")
    def public_legal():
        """Pagina unica con las 4 secciones legales (anclas: #terminos, #envios, #privacidad, #cookies)."""
        return render_template("public/legal.html", **_public_context())

    # ----------------------------------------------------------------------
    # CUENTAS DE CLIENTE (tienda publica)
    # ----------------------------------------------------------------------

    @app.route("/cuenta/registro", methods=["GET", "POST"])
    def cliente_registro():
        # Si ya esta logueado, mandarlo a su cuenta
        if auth.cliente_actual_id():
            return redirect(url_for("cliente_cuenta"))

        if request.method == "POST":
            nombre = (request.form.get("nombre") or "").strip()[:120]
            email = (request.form.get("email") or "").strip().lower()[:200]
            whatsapp = ((request.form.get("whatsapp") or "").strip()[:40]) or None
            password = request.form.get("password") or ""
            password2 = request.form.get("password2") or ""

            errores = []
            if len(nombre) < 2:
                errores.append("Ingresa tu nombre.")
            if "@" not in email or "." not in email:
                errores.append("Ingresa un email valido.")
            if len(password) < 6:
                errores.append("La contrasena debe tener al menos 6 caracteres.")
            if password != password2:
                errores.append("Las contrasenas no coinciden.")
            if email and Cliente.query.filter_by(email=email).first():
                errores.append("Ya existe una cuenta con ese email. Inicia sesion.")

            if errores:
                for e in errores:
                    flash(e, "error")
                return render_template(
                    "public/cliente_registro.html",
                    form={"nombre": nombre, "email": email, "whatsapp": whatsapp or ""},
                    **_public_context(),
                )

            cliente = Cliente(nombre=nombre, email=email, whatsapp=whatsapp)
            cliente.set_password(password)
            db.session.add(cliente)
            db.session.flush()

            # Adoptar reservas anonimas previas con el mismo WhatsApp
            if whatsapp:
                previas = Reserva.query.filter(
                    Reserva.cliente_id.is_(None),
                    Reserva.whatsapp == whatsapp,
                ).all()
                for r in previas:
                    r.cliente_id = cliente.id

            db.session.commit()
            auth.cliente_login(cliente.id)
            flash(f"Cuenta creada. Bienvenido, {cliente.nombre}!", "success")
            return redirect(url_for("cliente_cuenta"))

        return render_template("public/cliente_registro.html", form={}, **_public_context())

    @app.route("/cuenta/login", methods=["GET", "POST"], endpoint="cliente_login_page")
    def cliente_login_page():
        if auth.cliente_actual_id():
            return redirect(url_for("cliente_cuenta"))

        if request.method == "POST":
            ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
            import time as _time
            info = _cliente_login_attempts.get(ip, {"count": 0, "until": 0})
            if _time.time() < info["until"]:
                restante = int(info["until"] - _time.time())
                flash(f"Demasiados intentos. Espera {restante} segundos.", "error")
                return render_template("public/cliente_login.html", form={}, **_public_context()), 429
            email = (request.form.get("email") or "").strip().lower()
            password = request.form.get("password") or ""
            cliente = Cliente.query.filter_by(email=email).first()
            if not cliente or not cliente.check_password(password):
                info["count"] += 1
                if info["count"] >= 5:
                    info["until"] = _time.time() + min(60 * (info["count"] - 4), 600)
                _cliente_login_attempts[ip] = info
                _time.sleep(1)
                flash("Email o contrasena incorrectos.", "error")
                return render_template("public/cliente_login.html", form={"email": email}, **_public_context())

            _cliente_login_attempts.pop(ip, None)
            auth.cliente_login(cliente.id)
            flash(f"Hola de nuevo, {cliente.nombre}!", "success")
            next_url = request.args.get("next")
            # Solo rutas internas: evitar open redirect con "//sitio.com" o "/\sitio"
            if (next_url and next_url.startswith("/")
                    and not next_url.startswith("//")
                    and "\\" not in next_url):
                return redirect(next_url)
            return redirect(url_for("cliente_cuenta"))

        return render_template("public/cliente_login.html", form={}, **_public_context())

    @app.route("/cuenta/logout", methods=["POST"])
    def cliente_logout_route():
        auth.cliente_logout()
        flash("Cerraste sesion.", "success")
        return redirect(url_for("home"))

    @app.route("/cuenta")
    @auth.cliente_required
    def cliente_cuenta():
        from models import ClienteFavorito
        cliente = Cliente.query.get_or_404(auth.cliente_actual_id())
        reservas = Reserva.query.filter_by(cliente_id=cliente.id).order_by(Reserva.fecha.desc()).all()
        fav_ids = [
            f.disc_id for f in ClienteFavorito.query
            .filter_by(cliente_id=cliente.id)
            .order_by(ClienteFavorito.fecha.desc()).all()
        ]
        fav_by_id = {d.id: d for d in Disc.query.filter(Disc.id.in_(fav_ids)).all()} if fav_ids else {}
        favoritos = [fav_by_id[i] for i in fav_ids if i in fav_by_id]
        return render_template(
            "public/cliente_cuenta.html",
            cliente=cliente,
            reservas=reservas,
            favoritos=favoritos,
            **_public_context(),
        )

    @app.route("/favoritos")
    def public_favoritos():
        """Pagina de favoritos. Los IDs viven en localStorage del cliente;
        el JS pide los discos al endpoint /api/discos-por-id."""
        return render_template("public/favoritos.html", **_public_context())

    @app.route("/cuenta/api/favoritos/sync", methods=["POST"])
    def cliente_favoritos_sync():
        """Fusiona los favoritos de localStorage con los guardados en la cuenta y
        devuelve la lista unificada. Se llama al cargar la pagina si hay sesion de
        cliente, para que los favoritos viajen entre dispositivos."""
        from models import ClienteFavorito
        cid = auth.cliente_actual_id()
        if not cid:
            return jsonify({"ok": False, "logged_in": False, "ids": []})
        data = request.get_json(silent=True) or {}
        ids_local = {int(x) for x in (data.get("ids") or []) if str(x).isdigit()}
        existentes = {f.disc_id for f in ClienteFavorito.query.filter_by(cliente_id=cid).all()}
        nuevos = ids_local - existentes
        for i in nuevos:
            db.session.add(ClienteFavorito(cliente_id=cid, disc_id=i))
        if nuevos:
            db.session.commit()
        todos = sorted(existentes | ids_local)
        return jsonify({"ok": True, "logged_in": True, "ids": [str(i) for i in todos]})

    @app.route("/cuenta/api/favoritos/toggle", methods=["POST"])
    def cliente_favoritos_toggle():
        """Agrega o saca un favorito de la cuenta (cuando un cliente logueado
        toca el corazon). fav=true agrega, fav=false saca."""
        from models import ClienteFavorito
        cid = auth.cliente_actual_id()
        if not cid:
            return jsonify({"ok": False, "logged_in": False})
        data = request.get_json(silent=True) or {}
        try:
            disc_id = int(data.get("id"))
        except (TypeError, ValueError):
            return jsonify({"ok": False}), 400
        fav = bool(data.get("fav", True))
        existente = ClienteFavorito.query.filter_by(cliente_id=cid, disc_id=disc_id).first()
        if fav and not existente:
            db.session.add(ClienteFavorito(cliente_id=cid, disc_id=disc_id))
            db.session.commit()
        elif not fav and existente:
            db.session.delete(existente)
            db.session.commit()
        return jsonify({"ok": True, "fav": fav})

    @app.route("/api/discos-por-id")
    def api_discos_por_id():
        """Devuelve datos publicos de varios discos por ID (para wishlist/carrito).
        Uso: /api/discos-por-id?ids=1,5,9
        """
        ids_raw = (request.args.get("ids") or "").strip()
        if not ids_raw:
            return jsonify({"ok": True, "discos": []})
        ids = [int(x) for x in ids_raw.split(",") if x.strip().isdigit()][:100]
        if not ids:
            return jsonify({"ok": True, "discos": []})

        discs = Disc.query.filter(Disc.id.in_(ids)).all()
        # Mantener el orden pedido
        by_id = {d.id: d for d in discs}
        out = []
        for i in ids:
            d = by_id.get(i)
            if not d:
                continue
            # No exponer discos privados (coleccion personal / descartados)
            if d.estado in ("Apartado para mi", "Descartado") and not d.mostrar_agotado:
                continue
            cover = None
            if d.cover_local:
                cover = url_for("serve_cover", filename=d.cover_local)
            elif d.cover_url:
                cover = d.cover_url
            out.append({
                "id": d.id,
                "artista": d.artista,
                "titulo": d.titulo,
                "sello": d.sello,
                "anio": d.anio,
                "precio_uyu": d.precio_mostrar or 0,
                "cover": cover,
                "estado": d.estado,
                "disponible": not d.esta_agotado,
                "url": url_for("public_disc", disc_id=d.id),
                "tags": d.tags_list,
            })
        return jsonify({"ok": True, "discos": out})

    @app.route("/carrito")
    def public_carrito():
        """Pagina del carrito. Los IDs viven en localStorage; el JS los renderiza."""
        return render_template("public/carrito.html", **_public_context())

    @app.route("/checkout", methods=["GET", "POST"])
    def public_checkout():
        """Reserva de varios discos en una sola operacion.

        GET: muestra el form con el resumen (los discos vienen por ?ids=).
        POST: crea la reserva multi-disco.
        """
        if request.method == "POST":
            ids_raw = (request.form.get("ids") or "").strip()
            ids = [int(x) for x in ids_raw.split(",") if x.strip().isdigit()]
            ids = list(dict.fromkeys(ids))[:100]   # deduplicar preservando orden
            nombre = (request.form.get("nombre") or "").strip()[:120]
            whatsapp = (request.form.get("whatsapp") or "").strip()[:40]
            email = ((request.form.get("email") or "").strip()[:200]) or None
            nota = ((request.form.get("nota") or "").strip()[:1000]) or None

            errores = []
            if not ids:
                errores.append("El carrito esta vacio.")
            if len(nombre) < 2:
                errores.append("Ingresa tu nombre.")
            if len(whatsapp) < 7:
                errores.append("Ingresa un WhatsApp valido (con codigo de pais).")
            if not email:
                errores.append("Ingresa tu email para enviarte la confirmacion.")
            elif "@" not in email:
                errores.append("El email no parece valido.")

            # Transferencia: el comprobante es obligatorio
            if (request.form.get("metodo") == "transferencia"
                    and Setting.get("transferencia_activa", "0") == "1"):
                comp = request.files.get("comprobante")
                if not comp or not comp.filename:
                    errores.append("Adjunta el comprobante de la transferencia para confirmar.")
                elif (comp.mimetype or "").lower() not in (
                        "image/jpeg", "image/png", "image/webp", "image/gif", "application/pdf"):
                    errores.append("El comprobante debe ser una imagen o un PDF (max 12 MB).")
                else:
                    comp.stream.seek(0, os.SEEK_END)
                    _comp_size = comp.stream.tell()
                    comp.stream.seek(0)
                    if _comp_size > 12 * 1024 * 1024:
                        errores.append("El comprobante es muy grande (max 12 MB). Saca una foto mas liviana o mandalo en PDF.")

            # Traer discos y validar disponibilidad
            discs = Disc.query.filter(Disc.id.in_(ids)).all() if ids else []
            by_id = {d.id: d for d in discs}
            disponibles = [by_id[i] for i in ids if i in by_id
                           and not by_id[i].esta_agotado]

            if ids and not disponibles:
                errores.append("Ninguno de los discos del carrito esta disponible.")

            if errores:
                for e in errores:
                    flash(e, "error")
                return render_template(
                    "public/checkout.html",
                    ids=ids_raw,
                    form={"nombre": nombre, "whatsapp": whatsapp, "email": email, "nota": nota},
                    **_public_context(),
                )

            # Aviso si algun disco se agoto entre "agregar" y "confirmar"
            n_descartados = sum(1 for i in ids if i in by_id and by_id[i].esta_agotado)
            if n_descartados:
                flash(f"{n_descartados} disco(s) del carrito ya no estaban disponibles y se quitaron del pedido.", "info")

            # Crear la reserva (los discos NO cambian de estado hasta confirmar)
            reserva = Reserva(
                nombre=nombre,
                whatsapp=whatsapp,
                email=email,
                nota=nota,
                estado="Pendiente",
                cliente_id=auth.cliente_actual_id(),
            )
            db.session.add(reserva)
            db.session.flush()

            for d in disponibles:
                item = ReservaItem(
                    reserva_id=reserva.id,
                    disc_id=d.id,
                    artista_snap=d.artista,
                    titulo_snap=d.titulo,
                    precio_uyu_snap=d.precio_mostrar,
                )
                db.session.add(item)

            db.session.commit()

            # Metodo de pago elegido en el checkout
            metodo = request.form.get("metodo", "coordinar")
            if metodo == "transferencia" and Setting.get("transferencia_activa", "0") == "1":
                reserva.metodo_pago = "transferencia"
                _guardar_comprobante(reserva, request.files.get("comprobante"))
                db.session.commit()
            elif metodo == "mercadopago" and mp.activo():
                reserva.metodo_pago = "mercadopago"
                db.session.commit()
                url_pago = _mp_iniciar_pago(reserva, disponibles)
                if url_pago:
                    return redirect(url_pago)
                flash("No pudimos iniciar el pago online. Te dejamos la reserva para coordinar por mensaje.", "error")
            else:
                reserva.metodo_pago = "coordinar"
                db.session.commit()

            # Email al admin (best-effort, no rompe si falla) — con el comprobante adjunto si lo hay
            if mailer.is_configured():
                comp_path = None
                if reserva.comprobante_file:
                    comp_path = os.path.join(app.config["COMPROBANTES_DIR"], reserva.comprobante_file)
                try:
                    mailer.send_reserva_notification(reserva, disponibles[0], comprobante_path=comp_path)
                except Exception:
                    pass

            # Confirmacion al cliente (si dejo email)
            if reserva.email and mailer.can_send():
                try:
                    mailer.send_reserva_confirmacion_cliente_multi(reserva, disponibles)
                except Exception:
                    pass

            return render_template(
                "public/reserva_ok.html",
                reserva=reserva,
                multi=True,
                **_public_context(),
            )

        # GET
        ids_raw = (request.args.get("ids") or "").strip()
        return render_template(
            "public/checkout.html",
            ids=ids_raw,
            form={},
            **_public_context(),
        )
        content = (
            "User-agent: *\n"
            "Allow: /\n"
            "Disallow: /admin\n"
            "Disallow: /admin/\n"
            f"Sitemap: {url_for('sitemap_xml', _external=True)}\n"
        )
        return content, 200, {"Content-Type": "text/plain; charset=utf-8"}

    @app.route("/sitemap.xml")
    def sitemap_xml():
        """Sitemap con la home, sobre y todos los discos publicados."""
        from xml.sax.saxutils import escape
        from datetime import datetime as _dt
        urls = []
        # Home y sobre
        urls.append((url_for("home", _external=True), "1.0", None))
        urls.append((url_for("public_sobre", _external=True), "0.6", None))
        # Discos disponibles y reservados (siguen siendo paginas validas)
        discs = Disc.query.filter(Disc.estado.in_(["Disponible", "Reservado"])).all()
        for d in discs:
            lastmod = (d.fecha_modificacion or d.fecha_ingreso or _dt.utcnow()).strftime("%Y-%m-%d")
            urls.append((url_for("public_disc", disc_id=d.id, _external=True), "0.8", lastmod))

        body = ['<?xml version="1.0" encoding="UTF-8"?>',
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for loc, pri, lastmod in urls:
            entry = f"<url><loc>{escape(loc)}</loc>"
            if lastmod:
                entry += f"<lastmod>{lastmod}</lastmod>"
            entry += f"<priority>{pri}</priority></url>"
            body.append(entry)
        body.append("</urlset>")
        return "\n".join(body), 200, {"Content-Type": "application/xml; charset=utf-8"}

    # ----------------------------------------------------------------------
    # ADMIN: bandeja de reservas
    # ----------------------------------------------------------------------

    @app.route("/admin/reservas")
    @auth.admin_required
    def admin_reservas():
        filtro_estado = (request.args.get("estado") or "").strip()
        query = Reserva.query.order_by(Reserva.fecha.desc())
        if filtro_estado:
            query = query.filter(Reserva.estado == filtro_estado)
        reservas = query.all()

        # Contadores por estado
        contadores = {}
        for e in ESTADOS_RESERVA:
            contadores[e] = Reserva.query.filter_by(estado=e).count()

        return render_template(
            "admin_reservas.html",
            reservas=reservas,
            estados=ESTADOS_RESERVA,
            contadores=contadores,
            filtro_estado=filtro_estado,
            tc=Setting.get_tc(),
        )

    @app.route("/admin/reservas/<int:reserva_id>", methods=["GET", "POST"])
    @auth.admin_required
    def admin_reserva_detail(reserva_id: int):
        reserva = Reserva.query.get_or_404(reserva_id)

        if request.method == "POST":
            accion = request.form.get("accion")
            if accion == "estado":
                nuevo = (request.form.get("nuevo_estado") or "").strip()
                if nuevo not in ESTADOS_RESERVA:
                    flash(f"Estado invalido: {nuevo}", "error")
                else:
                    reserva.estado = nuevo
                    db.session.commit()

                    # Aplicar cambio de estado a TODOS los discos de la reserva
                    # Confirmada -> Reservado | Completada -> Vendido | Cancelada -> Disponible
                    estado_disco = {
                        "Confirmada": "Reservado",
                        "Completada": "Vendido",
                        "Cancelada": "Disponible",
                    }.get(nuevo)

                    if estado_disco:
                        cambios = 0
                        for item in reserva.items_efectivos:
                            disco = item.disco
                            if disco and disco.estado != estado_disco:
                                register_status_change(
                                    disco, estado_disco,
                                    nota=f"Reserva #{reserva.id} -> {nuevo}"
                                )
                                cambios += 1
                        if cambios:
                            db.session.commit()

                    flash(f"Reserva marcada como {nuevo}.", "success")

            elif accion == "delete":
                # Si algun disco quedo Reservado por esta reserva, devolverlo a Disponible
                for item in reserva.items_efectivos:
                    disco = item.disco
                    if disco and disco.estado == "Reservado":
                        register_status_change(
                            disco, "Disponible",
                            nota=f"Reserva #{reserva.id} eliminada"
                        )
                db.session.delete(reserva)
                db.session.commit()
                flash("Reserva eliminada.", "success")
                return redirect(url_for("admin_reservas"))

            return redirect(url_for("admin_reserva_detail", reserva_id=reserva.id))

        # Detectar conflictos: discos de esta reserva que ya esten Vendidos o Reservados
        # por otra via (para avisarte antes de confirmar)
        conflictos = []
        if reserva.estado == "Pendiente":
            for item in reserva.items_efectivos:
                disco = item.disco
                if disco and disco.estado in ("Vendido", "Reservado"):
                    conflictos.append(item)

        return render_template(
            "admin_reserva_detail.html",
            reserva=reserva,
            estados=ESTADOS_RESERVA,
            conflictos=conflictos,
            tc=Setting.get_tc(),
        )

    # ----------------------------------------------------------------------
    # ADMIN: settings publicos (extension de la pagina de ajustes)
    # ----------------------------------------------------------------------

    @app.route("/admin/settings/public", methods=["POST"])
    @auth.admin_required
    def settings_update_public():
        """Actualiza los textos publicos de la tienda."""
        keys = [
            "public_tagline",
            "public_anuncio_texto",
            "public_hero_title",
            "public_hero_subtitle",
            "public_banner_video_url",
            "public_about",
            "public_about_curaduria",
            "public_about_como_funciona",
            "public_envios",
            "public_instagram",
            "public_whatsapp",
            "public_grid_columnas",
            "public_grid_mobile",
            "public_discos_por_pagina",
            "public_carrusel_velocidad",
            "public_card_tamano",
            "analytics_snippet",
            "tip_label",
            "carrusel_preorder",
            "carrusel_tips",
            "carrusel_escuchados",
            "carrusel_destacados",
            "carrusel_nuevos",
            "mercadopago_activo",
            "mp_recargo_pct",
            "transferencia_activa",
            "transferencia_datos",
        ]
        carrusel_keys = ("carrusel_preorder", "carrusel_tips", "carrusel_escuchados",
                         "carrusel_destacados", "carrusel_nuevos")
        checkbox_keys = carrusel_keys + ("mercadopago_activo", "transferencia_activa")
        for key in keys:
            # Toggles/checkboxes: presente = "1", ausente = "0"
            if key in checkbox_keys:
                Setting.set(key, "1" if request.form.get(key) else "0")
                continue
            value = (request.form.get(key) or "").strip()
            # Recargo de Mercado Pago: entre 0 y 20 %
            if key == "mp_recargo_pct":
                try:
                    value = str(max(0.0, min(float(value or 7), 20.0)))
                except (ValueError, TypeError):
                    value = "7"
            # Validar discos por pagina: entre 4 y 48
            elif key == "public_discos_por_pagina":
                try:
                    n = int(value or 24)
                    value = str(max(4, min(n, 48)))
                except (ValueError, TypeError):
                    value = "24"
            # Validar velocidad carrusel: entre 3 y 20 segundos
            elif key == "public_carrusel_velocidad":
                try:
                    n = int(value or 7)
                    value = str(max(3, min(n, 20)))
                except (ValueError, TypeError):
                    value = "7"
            Setting.set(key, value)
        flash("Textos publicos actualizados.", "success")
        return redirect(url_for("settings_page"))

    @app.route("/admin/settings/legal", methods=["POST"])
    @auth.admin_required
    def settings_update_legal():
        """Actualiza los textos legales (terminos, envios, privacidad, cookies)."""
        keys = [
            "public_legal_terminos",
            "public_legal_envios",
            "public_legal_privacidad",
            "public_legal_cookies",
        ]
        for key in keys:
            value = (request.form.get(key) or "").strip()
            Setting.set(key, value)
        flash("Textos legales actualizados.", "success")
        return redirect(url_for("settings_page"))

    @app.route("/admin/settings/banner", methods=["POST"])
    @auth.admin_required
    def settings_upload_banner():
        """Sube una imagen de hero/banner para la home publica."""
        file = request.files.get("banner")
        if not file or not file.filename:
            flash("No se selecciono ninguna imagen.", "error")
            return redirect(url_for("settings_page"))

        media_type = (file.mimetype or "").lower()
        if media_type not in {"image/jpeg", "image/png", "image/webp"}:
            flash(f"Tipo no soportado: {media_type}. Usar JPG, PNG o WEBP.", "error")
            return redirect(url_for("settings_page"))

        ext_map = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
        ext = ext_map.get(media_type, ".jpg")

        import uuid as _uuid
        filename = f"banner_{_uuid.uuid4().hex[:8]}{ext}"
        covers_dir = app.config["COVERS_DIR"]
        os.makedirs(covers_dir, exist_ok=True)
        dest_path = os.path.join(covers_dir, filename)

        try:
            file.save(dest_path)
        except Exception as e:
            flash(f"Error al guardar archivo: {e}", "error")
            return redirect(url_for("settings_page"))

        # Guardar una ruta fija y simple. Guardamos solo el nombre del archivo
        # con un prefijo conocido; _banner_url_si_existe lo resuelve al servir.
        Setting.set("public_banner_url", f"/covers/{filename}")
        flash("Banner actualizado.", "success")
        return redirect(url_for("settings_page"))

    @app.route("/admin/settings/banner/clear", methods=["POST"])
    @auth.admin_required
    def settings_clear_banner():
        Setting.set("public_banner_url", "")
        flash("Banner eliminado.", "success")
        return redirect(url_for("settings_page"))

    # ----------------------------------------------------------------------
    # Login / Logout
    # ----------------------------------------------------------------------

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if not auth.auth_enabled():
            flash(
                "El login no esta configurado. Editar .env y agregar "
                "ADMIN_USER y ADMIN_PASS, despues reiniciar el servidor.",
                "info",
            )
            return redirect(url_for("dashboard"))

        if auth.is_logged_in():
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            # --- Rate limiting: frena fuerza bruta por IP ---
            ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
            info = _login_attempts.get(ip, {"count": 0, "until": 0})
            import time as _time
            if _time.time() < info["until"]:
                restante = int(info["until"] - _time.time())
                flash(f"Demasiados intentos. Espera {restante} segundos.", "error")
                return render_template("admin_login.html", next_url=request.form.get("next", "")), 429

            user = request.form.get("user", "").strip()
            password = request.form.get("password", "")
            if auth.check_credentials(user, password):
                _login_attempts.pop(ip, None)  # limpiar al loguear bien
                auth.login(user)
                flash("Bienvenido.", "success")
                next_url = request.args.get("next") or request.form.get("next")
                if next_url and next_url.startswith("/admin"):
                    return redirect(next_url)
                return redirect(url_for("dashboard"))
            else:
                # Registrar intento fallido; tras 5, bloquear 60s (y creciendo)
                info["count"] += 1
                if info["count"] >= 5:
                    info["until"] = _time.time() + min(60 * (info["count"] - 4), 600)
                _login_attempts[ip] = info
                _time.sleep(1)  # frenar bots aun antes del bloqueo
                flash("Usuario o contrasenia incorrectos.", "error")

        next_url = request.args.get("next", "")
        return render_template("admin_login.html", next_url=next_url)

    @app.route("/admin/logout", methods=["POST", "GET"])
    def admin_logout():
        auth.logout()
        flash("Sesion cerrada.", "success")
        return redirect(url_for("admin_login"))

    # ----------------------------------------------------------------------
    # Redirects para URLs viejas (por si alguien tiene bookmarks)
    # ----------------------------------------------------------------------

    @app.route("/discs/<int:disc_id>")
    def _legacy_disc_detail(disc_id: int):
        return redirect(url_for("disc_detail", disc_id=disc_id), code=301)

    @app.route("/discs/<int:disc_id>/edit")
    def _legacy_disc_edit(disc_id: int):
        return redirect(url_for("disc_edit", disc_id=disc_id), code=301)

    @app.route("/resumen")
    def _legacy_resumen():
        return redirect(url_for("resumen"), code=301)

    @app.route("/settings")
    def _legacy_settings():
        return redirect(url_for("settings_page"), code=301)


app = create_app()


if __name__ == "__main__":
    app.run(
        host=config.FLASK_HOST,
        port=config.FLASK_PORT,
        debug=config.FLASK_DEBUG,
    )
