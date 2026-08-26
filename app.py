"""Arcano Stock - Aplicacion Flask principal."""
import csv
import io
import os
import re as _re_email
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
    AUDIO_ESTADOS,
    ESTADOS_RESERVA,
    CONDICIONES_DISCO,
    now_uy,
    today_uy,
    fire_sale_config,
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
_reserva_attempts: dict = {}  # ip -> [timestamps] para limitar reservas (anti-spam / mail-bombing)


def _reserva_supera_limite(ip, limite=12, ventana=3600):
    """True si esta IP ya hizo demasiadas reservas en la ventana (segundos).
    Frena bots que inunden la casilla del admin o usen el SMTP para spamear."""
    import time as _t
    ahora = _t.time()
    marcas = [t for t in _reserva_attempts.get(ip, []) if ahora - t < ventana]
    _reserva_attempts[ip] = marcas
    return len(marcas) >= limite


def _reserva_registrar(ip):
    """Registra una reserva concretada desde esta IP (para el rate-limit)."""
    import time as _t
    _reserva_attempts.setdefault(ip, []).append(_t.time())


_webhook_hits: dict = {}  # ip -> [timestamps] para limitar el webhook de MP


def _webhook_supera_limite(ip, limite=120, ventana=60):
    """True si esta IP golpeo el webhook de MP demasiadas veces en la ventana.
    Cada hit puede disparar una llamada saliente a la API de MP, asi que sin
    limite un flood amplifica. El limite es holgado: MP notifica desde pocas IPs
    y no llega a esos numeros en operacion normal."""
    import time as _t
    ahora = _t.time()
    marcas = [t for t in _webhook_hits.get(ip, []) if ahora - t < ventana]
    marcas.append(ahora)
    _webhook_hits[ip] = marcas
    return len(marcas) > limite


_play_dedup: dict = {}  # (ip, track_id) -> ultimo timestamp


def _play_ya_contado(ip, track_id, ventana=20):
    """True si esta IP ya conto una escucha de este track hace < ventana segundos.
    Evita que el contador (y el chart 'lo mas escuchado') se infle con clicks
    repetidos, reintentos del player o un bot que golpee el endpoint publico."""
    import time as _t
    ahora = _t.time()
    # Poda barata para que el dict no crezca sin limite.
    if len(_play_dedup) > 5000:
        for k, ts in list(_play_dedup.items()):
            if ahora - ts > 3600:
                _play_dedup.pop(k, None)
    clave = (ip, track_id)
    ultimo = _play_dedup.get(clave, 0)
    if ahora - ultimo < ventana:
        return True
    _play_dedup[clave] = ahora
    return False


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
        # Redimensionar: una portada no necesita mas de ~1000px de lado. Antes se
        # guardaba a resolucion completa (fotos de 500-600 KB) y el celular bajaba la
        # imagen entera para una card de ~300px. thumbnail() respeta la proporcion y
        # solo achica (nunca agranda). Solo afecta covers NUEVAS.
        img.thumbnail((1000, 1000), Image.LANCZOS)
        webp_path = base + ".webp"
        img.save(webp_path, "WEBP", quality=82, method=6)
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
    # Techo GLOBAL de tamano de request. Alto porque "Desde video" sube VARIOS
    # videos juntos. Es solo el limite duro de ultima instancia; el guard de
    # _limitar_tamano_por_endpoint() aplica un limite MUCHO mas chico a todo lo
    # que no sea el subir-video (asi no se puede DoSear el login con 400 MB).
    app.config["MAX_CONTENT_LENGTH"] = 400 * 1024 * 1024  # 400 MB (techo duro)
    # Endpoints que legitimamente suben archivos grandes (videos).
    _ENDPOINTS_UPLOAD_GRANDE = {"disc_new_from_video", "disc_video_review"}
    _LIMITE_NORMAL = 60 * 1024 * 1024  # 60 MB para el resto (covers, samples, galeria)

    @app.before_request
    def _limitar_tamano_por_endpoint():
        # Rechaza temprano (por el header Content-Length, sin leer el cuerpo) los
        # requests desmesurados a endpoints que no son el de subir video.
        if request.endpoint in _ENDPOINTS_UPLOAD_GRANDE:
            return
        cl = request.content_length
        if cl is not None and cl > _LIMITE_NORMAL:
            return "Archivo demasiado grande.", 413

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

    # Cache larga para estaticos (CSS/JS): el cache-busting ?v=<mtime> ya fuerza el
    # refresco cuando cambia el archivo, asi que el navegador no revalida en cada carga.
    # (El HTML se sirve con no-cache aparte, no lo afecta.)
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 31536000

    # Ruta que sirve las portadas desde COVERS_DIR (para que funcionen tanto en
    # local como cuando viven en el disco persistente fuera de /static).
    from flask import send_from_directory

    @app.route("/covers/<path:filename>")
    def serve_cover(filename):
        # Cache larga: las portadas son inmutables por nombre (cover_<hash>).
        return send_from_directory(app.config["COVERS_DIR"], filename, max_age=31536000)

    # ---- App instalable del admin (PWA) ----
    @app.route("/admin/manifest.webmanifest")
    def admin_manifest():
        """Manifest de la 'app' del admin: hace que se pueda instalar en el celular
        con icono propio y a pantalla completa (standalone)."""
        import json as _json
        from flask import Response as _Resp
        data = {
            "name": "Arcano Admin",
            "short_name": "Arcano",
            "description": "Gestión de Arcano Record Store",
            "start_url": "/admin/",
            "scope": "/admin/",
            "display": "standalone",
            "orientation": "portrait",
            "background_color": "#0a0a0c",
            "theme_color": "#0a0a0c",
            "icons": [
                {"src": url_for("static", filename="icon-192.png"), "sizes": "192x192", "type": "image/png"},
                {"src": url_for("static", filename="icon-512.png"), "sizes": "512x512", "type": "image/png"},
                {"src": url_for("static", filename="icon-maskable-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
            ],
        }
        return _Resp(_json.dumps(data), mimetype="application/manifest+json")

    @app.route("/admin/sw.js")
    def admin_service_worker():
        """Service worker (scope /admin/): cachea el 'cascaron' para que la app abra
        rapido y muestre lo ultimo visto aunque no haya senal. Los datos en vivo
        siguen necesitando conexion."""
        from flask import Response as _Resp
        sw = """
const CACHE = 'arcano-admin-v1';
self.addEventListener('install', function(e){
  e.waitUntil(caches.open(CACHE).then(function(c){ return c.addAll(['/static/css/style.css']); }).catch(function(){}));
  self.skipWaiting();
});
self.addEventListener('activate', function(e){
  e.waitUntil(caches.keys().then(function(ks){ return Promise.all(ks.map(function(k){ if(k!==CACHE) return caches.delete(k); })); }));
  self.clients.claim();
});
self.addEventListener('fetch', function(e){
  var req = e.request;
  if (req.method !== 'GET') return;
  var url = new URL(req.url);
  if (url.origin !== location.origin) return;
  // Estaticos y portadas: primero cache, actualiza en segundo plano.
  if (url.pathname.indexOf('/static/') === 0 || url.pathname.indexOf('/covers/') === 0) {
    e.respondWith(caches.match(req).then(function(hit){
      var net = fetch(req).then(function(res){ var copy = res.clone(); caches.open(CACHE).then(function(c){ c.put(req, copy); }); return res; }).catch(function(){ return hit; });
      return hit || net;
    }));
    return;
  }
  // Paginas del admin: primero la red; si no hay senal, lo ultimo cacheado.
  if (url.pathname.indexOf('/admin/') === 0) {
    e.respondWith(fetch(req).then(function(res){
      var copy = res.clone(); caches.open(CACHE).then(function(c){ c.put(req, copy); }); return res;
    }).catch(function(){ return caches.match(req); }));
    return;
  }
});
"""
        resp = _Resp(sw, mimetype="application/javascript")
        resp.headers["Service-Worker-Allowed"] = "/admin/"
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    # Carpeta de samples de audio/video subidos a mano. En produccion tiene que
    # vivir en el DISCO PERSISTENTE o los archivos se borran en cada deploy.
    # Prioridad: SAMPLES_PATH explicito > derivar del disco de covers (mismo disco
    # persistente en Render, aunque no este SAMPLES_PATH) > static/samples (local).
    samples_path = os.environ.get("SAMPLES_PATH", "").strip()
    if not samples_path and covers_path:
        samples_path = os.path.join(os.path.dirname(covers_path.rstrip("/\\")), "samples")
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
            return send_from_directory(d, filename, max_age=31536000)
        return send_from_directory(os.path.join(app.static_folder, "samples"), filename, max_age=31536000)

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
    _backfill_ventas_sin_cliente(app)
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
            "now": now_uy(),
            "url_pagina": url_pagina,
            "sort_url": sort_url,
            "cliente_actual": cliente_actual,
            "todos_los_pedidos": _todos_los_pedidos(),
            # TC EUR y USD disponibles en TODAS las paginas (el header del admin los muestra)
            "tc": Setting.get_tc(),
            "tc_usd": Setting.get_tc_usd(),
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
    "audio_estado": Disc.audio_estado,
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

    pedido = (args.get("pedido") or "").strip()
    if pedido and pedido != "todos":
        if pedido == "sin_pedido":
            q = q.filter(Disc.pedido_id.is_(None))
        else:
            try:
                q = q.filter(Disc.pedido_id == int(pedido))
            except (ValueError, TypeError):
                pass

    audio = (args.get("audio") or "").strip()
    if audio and audio != "todos":
        if audio == "sin_definir":
            q = q.filter(or_(Disc.audio_estado.is_(None), Disc.audio_estado == ""))
        else:
            q = q.filter(Disc.audio_estado == audio)

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
        # Cantidad: para un disco Vendido son las copias vendidas (stock ya es 0);
        # para el resto, el stock. Antes usaba (stock or 1) y contaba 1 sola copia en
        # los vendidos multi-copia (subcontaba facturado/ganancia).
        cant = d.copias_facturadas if d.estado == "Vendido" else (d.stock or 1)
        # Costo por ORIGEN: los discos de la coleccion personal no le costaron al
        # negocio (aporte), asi que su costo cuenta 0. Mismo criterio que Contabilidad
        # (metrics._costo_negocio); antes se usaba d.ganancia_uyu, que restaba el costo
        # tambien a los personales y daba una "Ganancia realizada" distinta a Contabilidad.
        costo_neg = 0.0 if (d.origen or "arcano") == "personal" else (d.costo_final_uyu or 0)
        gan = ((d.precio_mostrar or d.precio_uyu or 0) - costo_neg)
        suma_costo_eur += (d.costo_eur or 0) * cant
        suma_envio_eur += (d.envio_eur or 0) * cant
        suma_costo_final_uyu += (d.costo_final_uyu or 0) * cant
        # precio_mostrar (con oferta) para que el pie cuadre: precio - costo = ganancia
        # (la ganancia ya usa el precio de oferta).
        suma_precio_uyu += (d.precio_mostrar or d.precio_uyu or 0) * cant
        suma_ganancia_uyu += gan * cant
        stock_total += d.stock or 0
        if d.estado == "Disponible":
            ganancia_potencial_uyu += gan * cant
        elif d.estado == "Vendido":
            ganancia_realizada_uyu += gan * cant

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


# Reglas de envio (checkout). Montevideo: $50 con 1-2 discos, GRATIS con 3+.
# Interior: $0 en la web (lo cobra la agencia). Retiro: $0.
ENVIO_MVD_COSTO = 50
ENVIO_MVD_GRATIS_DESDE = 3


def _calcular_envio_uyu(entrega_tipo, n_discos):
    """Costo del envio en UYU segun la zona y la cantidad de discos."""
    if entrega_tipo == "montevideo":
        return 0 if (n_discos or 0) >= ENVIO_MVD_GRATIS_DESDE else ENVIO_MVD_COSTO
    return 0  # retiro / interior / desconocido -> sin costo en la web


def _ga_purchase_items(reserva):
    """Lista de items para el evento 'purchase' de GA4/Meta (ecommerce).
    Usa los snapshots de la reserva asi funciona aunque el disco ya no exista."""
    items = []
    try:
        for it in reserva.items_efectivos:
            nombre = f"{it.artista_snap or ''} - {it.titulo_snap or ''}".strip(" -")
            items.append({
                "item_id": str(it.disc_id or ""),
                "item_name": nombre or "Disco",
                "price": round(it.precio_uyu_snap or 0, 2),
                "quantity": 1,
            })
    except Exception:
        pass
    return items


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

    ahora = now_uy()
    disc.estado = nuevo_estado

    if nuevo_estado == "Reservado" and not disc.fecha_reserva:
        disc.fecha_reserva = ahora
    if nuevo_estado == "Vendido":
        if not disc.fecha_venta:
            disc.fecha_venta = ahora
        # Si no se registraron copias vendidas, asumir al menos 1
        if not getattr(disc, "copias_vendidas", 0):
            disc.copias_vendidas = 1
        # Registrar el precio realmente cobrado (si no vino de una venta web con
        # snapshot, que lo setea aparte). Aca precio_mostrar todavia devuelve el
        # precio de lista/oferta porque precio_venta_uyu sigue en None.
        if getattr(disc, "precio_venta_uyu", None) is None:
            disc.precio_venta_uyu = disc.precio_mostrar
    else:
        # Al salir de Vendido, limpiar los datos de venta: si no, una re-venta futura
        # hereda la fecha vieja y la venta cae en el mes equivocado (todo filtra por
        # fecha_venta). Cubre el caso "lo marque Vendido por error y lo revierto".
        if disc.fecha_venta is not None:
            disc.fecha_venta = None
        if getattr(disc, "copias_vendidas", 0):
            disc.copias_vendidas = 0
        if getattr(disc, "precio_venta_uyu", None) is not None:
            disc.precio_venta_uyu = None

    evento = StatusHistory(
        disc_id=disc.id,
        estado=nuevo_estado,
        nota=nota,
        fecha=ahora,
    )
    db.session.add(evento)


def _norm_ig(ig):
    """Normaliza un usuario de Instagram: sin @, en minusculas."""
    return (ig or "").strip().lstrip("@").lower()[:100] or None


def _buscar_o_crear_cliente(nombre=None, whatsapp=None, email=None, instagram=None,
                            origen="web", crear=True):
    """Base de clientes: busca uno existente por email / whatsapp / instagram (en ese
    orden) y, si no hay, lo crea. Rellena los datos que le falten al que encuentre (sin
    pisar lo que ya tiene). Devuelve el Cliente, o None si no hay datos utiles."""
    from models import Cliente
    nombre = (nombre or "").strip()[:200] or None
    whatsapp = (whatsapp or "").strip()[:50] or None
    email_n = (email or "").strip().lower()[:200] or None
    ig = _norm_ig(instagram)
    if not (nombre or whatsapp or email_n or ig):
        return None
    cli = None
    if email_n:
        cli = Cliente.query.filter(db.func.lower(Cliente.email) == email_n).first()
    if not cli and whatsapp:
        cli = Cliente.query.filter(Cliente.whatsapp == whatsapp).first()
    if not cli and ig:
        cli = Cliente.query.filter(db.func.lower(Cliente.instagram) == ig).first()
    if cli:
        # Rellenar lo que falte, sin pisar lo que ya cargaste
        if nombre and not cli.nombre:
            cli.nombre = nombre
        if whatsapp and not cli.whatsapp:
            cli.whatsapp = whatsapp
        if email_n and not cli.email:
            cli.email = email_n
        if ig and not cli.instagram:
            cli.instagram = ig
        return cli
    if not crear:
        return None
    cli = Cliente(
        nombre=(nombre or (("@" + ig) if ig else None) or whatsapp or "Cliente"),
        whatsapp=whatsapp, email=email_n, instagram=ig, origen=origen,
        fecha_registro=now_uy(),
    )
    db.session.add(cli)
    db.session.flush()
    return cli


def _atribuir_compra(discs, nombre=None, whatsapp=None, email=None, instagram=None,
                     origen="web", cliente=None):
    """Ata la compra de `discs` a un cliente (lo busca/crea con los datos, o usa el
    `cliente` ya conocido). Setea disc.cliente_id (el comprador) en los que no lo tengan.
    Devuelve el Cliente (o None)."""
    cli = cliente
    if cli is None:
        cli = _buscar_o_crear_cliente(nombre, whatsapp, email, instagram, origen=origen)
    if cli:
        for d in discs:
            if not getattr(d, "cliente_id", None):
                d.cliente_id = cli.id
    return cli


def _backfill_ventas_sin_cliente(app):
    """Al arrancar (idempotente): re-ata a su cliente las ventas viejas que quedaron
    sueltas. (1) Reservas Pagada sin cliente -> busca/crea el cliente por sus datos
    (email/celular/IG) y la linkea. (2) Discos Vendido de reservas Pagada ya
    linkeadas que no tengan comprador -> les pone el comprador de la reserva. Nunca
    pisa lo que ya esta atado, asi que correrlo de nuevo no cambia nada."""
    try:
        with app.app_context():
            cambios = 0
            for r in Reserva.query.filter(Reserva.estado == "Pagada",
                                          Reserva.cliente_id.is_(None)).all():
                cli = _buscar_o_crear_cliente(
                    r.nombre, r.whatsapp, r.email,
                    origen=("web" if (r.canal or "web") == "web" else "manual"))
                if cli:
                    r.cliente_id = cli.id
                    cambios += 1
            db.session.flush()
            for r in Reserva.query.filter(Reserva.estado == "Pagada",
                                          Reserva.cliente_id.isnot(None)).all():
                for it in r.items_efectivos:
                    d = getattr(it, "disco", None)
                    if d and d.estado == "Vendido" and not getattr(d, "cliente_id", None):
                        d.cliente_id = r.cliente_id
                        cambios += 1
            # (3) Discos agrupados como UNA venta (mismo venta_grupo): si alguno del
            # grupo ya tiene comprador (o el grupo es de una reserva 'r<id>'), los
            # demas del grupo heredan ese comprador. Cubre "agrupe un disco a la venta
            # de un cliente" desde el panel de Ventas.
            from collections import defaultdict as _dd
            grupos = _dd(list)
            for d in Disc.query.filter(Disc.estado == "Vendido",
                                       Disc.venta_grupo.isnot(None)).all():
                grupos[d.venta_grupo].append(d)
            for grupo, ds in grupos.items():
                # Si el grupo mezcla DOS compradores distintos, no adivinar: se saltea
                # (un grupo deberia ser una sola transaccion = un solo comprador).
                _distintos = set(x.cliente_id for x in ds if getattr(x, "cliente_id", None))
                if len(_distintos) > 1:
                    continue
                cli = next(iter(_distintos), None)
                if not cli and grupo.startswith("r"):
                    try:
                        _rr = Reserva.query.get(int(grupo[1:]))
                        cli = _rr.cliente_id if _rr else None
                    except (ValueError, TypeError):
                        cli = None
                if cli:
                    for x in ds:
                        if not getattr(x, "cliente_id", None):
                            x.cliente_id = cli
                            cambios += 1
            if cambios:
                db.session.commit()
                print(f"Backfill: {cambios} venta(s)/reserva(s) atadas a su cliente")
    except Exception as e:
        try:
            db.session.rollback()
        except Exception:
            pass
        print(f"Backfill clientes: se omitio por un error: {e}")


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


def _client_ip():
    """IP del cliente para rate-limiting (anti-abuso). Detras del proxy de Render
    el ULTIMO valor de X-Forwarded-For es el que agrega el proxy de confianza (la
    IP real que vio Render). El PRIMERO lo puede falsificar el cliente mandando su
    propio header, asi que NO se usa: un atacante rotaria IPs falsas para saltarse
    el limite. Si no hay XFF, se usa remote_addr."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        partes = [p.strip() for p in xff.split(",") if p.strip()]
        if partes:
            return partes[-1]
    return request.remote_addr or "?"


_EMAIL_RE = _re_email.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _email_valido(email):
    """Valida un email de forma conservadora y, sobre todo, RECHAZA saltos de
    linea (\\r \\n): un email con un salto de linea permitiria inyectar headers
    (Bcc:, etc.) en el mail de confirmacion que se envia al cliente."""
    if not email or len(email) > 200:
        return False
    if "\n" in email or "\r" in email:
        return False
    return _EMAIL_RE.match(email) is not None


def _categorias_origen_lista():
    """Lista GESTIONADA de categorias de origen propias (caja). Guardada en el
    setting origen_categorias_lista. La primera vez se siembra con las que ya
    esten en uso, para no perder las existentes."""
    import json as _json
    raw = Setting.get("origen_categorias_lista", None)
    if raw is None:
        from models import MovimientoCaja as _MC
        used = sorted({
            (r[0] or "").strip() for r in
            db.session.query(_MC.origen_categoria).filter(_MC.origen_categoria.isnot(None)).distinct().all()
            if (r[0] or "").strip()
        })
        Setting.set("origen_categorias_lista", _json.dumps(used))
        return used
    try:
        return [c for c in _json.loads(raw) if isinstance(c, str) and c.strip()]
    except Exception:
        return []


def _categorias_origen_guardar(lista):
    import json as _json
    # unica, sin vacios, respetando el orden
    limpia = []
    for c in lista:
        c = (c or "").strip()[:60]
        if c and c not in limpia:
            limpia.append(c)
    Setting.set("origen_categorias_lista", _json.dumps(limpia))
    return limpia


def _categorias_origen_add(nombre):
    """Agrega una categoria a la lista gestionada (si no estaba). Devuelve la lista."""
    nombre = (nombre or "").strip()[:60]
    lista = _categorias_origen_lista()
    if nombre and nombre not in lista:
        lista = _categorias_origen_guardar(lista + [nombre])
    return lista


def _text_on_color(hexcolor):
    """Devuelve un color de texto legible (#0a0a0a o #fff) segun si el fondo dado
    es claro u oscuro. Para los tags de categoria con color elegido por Facundo."""
    try:
        h = (hexcolor or "").lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        return "#0a0a0a" if lum > 150 else "#ffffff"
    except Exception:
        return "#ffffff"


def _intercalar_tips(discos):
    """Reordena una lista de discos para carrusel de modo que los 'staff picks'
    (es_tip) queden REPARTIDOS entre los que no lo tienen, en vez de todos juntos
    al principio (si no, parece que todos son staff picks). El primero nunca es un
    staff pick mientras haya al menos un disco sin el flag."""
    discos = list(discos)
    tips = [d for d in discos if d.es_tip]
    otros = [d for d in discos if not d.es_tip]
    if not tips or not otros:
        return discos
    resultado = list(otros)
    n, k = len(otros), len(tips)
    for i, t in enumerate(tips):
        pos = round((i + 1) * n / (k + 1)) + i  # +i: la lista crece al insertar
        pos = max(1, min(pos, len(resultado)))   # nunca en la posicion 0 (primero)
        resultado.insert(pos, t)
    return resultado


def _archivos_de_disco(disc):
    """Junta los nombres de archivo (portada, galeria, samples) que usa un disco.
    Se llama ANTES de borrar el disco, para saber que archivos revisar despues."""
    covers, samples = set(), set()
    if disc.cover_local:
        covers.add(disc.cover_local)
    for img in disc.imagenes:
        if img.filename:
            covers.add(img.filename)
    for t in disc.tracks:
        if t.sample_local:
            samples.add(t.sample_local)
    return covers, samples


def _purgar_archivos_huerfanos(covers_dir, samples_dir, static_folder, covers, samples):
    """Borra del disco los archivos que ya no referencia NINGUN registro.
    Se llama DESPUES de commitear la eliminacion, para que las queries reflejen
    el estado final. Best-effort: cualquier error al borrar se ignora.

    OJO: al duplicar un disco se copia el mismo nombre de archivo, asi que un
    archivo puede seguir en uso por otro disco/track -> por eso se verifica que
    no quede ninguna referencia antes de borrarlo (si no, romperiamos la copia)."""
    from models import DiscImage, Track

    def _rm(path):
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    for fname in covers:
        if not fname:
            continue
        en_uso = (
            Disc.query.filter_by(cover_local=fname).first() is not None
            or DiscImage.query.filter_by(filename=fname).first() is not None
        )
        if en_uso:
            continue
        _rm(os.path.join(covers_dir, fname))

    for fname in samples:
        if not fname:
            continue
        if Track.query.filter_by(sample_local=fname).first() is not None:
            continue
        _rm(os.path.join(samples_dir, fname))
        # Samples viejos que hayan quedado en static/samples (fallback)
        _rm(os.path.join(static_folder, "samples", fname))


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
            "pedido": request.args.get("pedido", "todos"),
            "audio": request.args.get("audio", "todos"),
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
            audio_estados=AUDIO_ESTADOS,
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

    def _sweep_samples_huerfanos(max_horas=24):
        """Borra samples viejos (>max_horas) que ningun Track referencia: limpia los
        archivos que quedan de sesiones de 'desde video' abandonadas (no se completo la
        alta). Solo samples: los covers se dejan porque ahi tambien vive el banner/hero.
        Corre como mucho 1 vez al dia. Best-effort: cualquier error se ignora."""
        import time
        try:
            if Setting.get("last_sweep_samples") == today_uy().isoformat():
                return
            Setting.set("last_sweep_samples", today_uy().isoformat())
            samples_dir = app.config.get("SAMPLES_DIR")
            if not samples_dir or not os.path.isdir(samples_dir):
                return
            limite = time.time() - max_horas * 3600
            for fname in os.listdir(samples_dir):
                path = os.path.join(samples_dir, fname)
                try:
                    if not os.path.isfile(path) or os.path.getmtime(path) > limite:
                        continue  # muy reciente: puede ser una sesion en curso
                    if Track.query.filter_by(sample_local=fname).first() is not None:
                        continue  # en uso
                    os.remove(path)
                except Exception:
                    pass
        except Exception:
            pass

    @app.route("/admin/discs/new/from-video", methods=["GET", "POST"])
    @auth.admin_required
    def disc_new_from_video():
        """Sube uno o varios videos del disco (los de Instagram). Cada video =
        un track y queda como su sample. De la placa saca los datos del disco."""
        api_key_set = bool(config.ANTHROPIC_API_KEY)
        _sweep_samples_huerfanos()  # limpieza diaria de samples de sesiones abandonadas
        if request.method == "POST":
            if not api_key_set:
                flash("La API key de Anthropic no esta configurada.", "error")
                return redirect(url_for("disc_new_from_video"))
            files = [f for f in request.files.getlist("videos") if f and f.filename]
            if not files:
                flash("Subi al menos un video.", "error")
                return redirect(url_for("disc_new_from_video"))

            import video as _video
            import uuid as _uuid
            samples_dir = app.config["SAMPLES_DIR"]
            os.makedirs(samples_dir, exist_ok=True)

            disc_fields = {}
            tracks = []
            total_cost = 0.0
            errores = []
            for f in files:
                mtype = (f.mimetype or "").lower()
                if mtype not in _video.VIDEO_EXT_MAP:
                    errores.append(f"{f.filename}: tipo no soportado ({mtype})")
                    continue
                # Chequear tamano ANTES de guardar (evita archivos gigantes)
                try:
                    f.stream.seek(0, os.SEEK_END)
                    fsize = f.stream.tell()
                    f.stream.seek(0)
                except (OSError, ValueError):
                    fsize = 0
                if fsize and fsize > _video.MAX_VIDEO_BYTES:
                    errores.append(f"{f.filename}: pesa demasiado ({fsize // 1024 // 1024} MB, max 50 MB)")
                    continue
                ext = _video.VIDEO_EXT_MAP[mtype]
                fname = f"sample_{_uuid.uuid4().hex[:12]}{ext}"
                dest_path = os.path.join(samples_dir, fname)
                try:
                    f.save(dest_path)  # se guarda directo a disco, sin pasar por la RAM
                except Exception as e:
                    errores.append(f"{f.filename}: no se pudo guardar ({e})")
                    continue
                # Respaldo desde el nombre del archivo (suele venir "Artista - Track")
                fn_artista, fn_track = _video.split_artist_track(
                    _video.clean_track_name_from_filename(f.filename))
                track_artista, track_titulo = fn_artista, fn_track
                frames, ferr = _video.extract_frames_from_path(dest_path)
                if ferr:
                    errores.append(f"{f.filename}: {ferr}")
                else:
                    res = vision.extract_from_video_frames(frames, filename=f.filename)
                    if res.cost_usd:
                        total_cost += res.cost_usd
                    if res.ok:
                        d = res.data
                        # Datos del disco (solo lo que aparece escrito): primer no-nulo
                        for k in ("sello", "anio", "genero", "catalogo", "precio_uyu"):
                            if not disc_fields.get(k) and d.get(k):
                                disc_fields[k] = d.get(k)
                        # El video muestra el artista y el nombre del track (NO el EP)
                        if d.get("artista"):
                            track_artista = d.get("artista")
                        if d.get("track"):
                            track_titulo = d.get("track")
                    else:
                        errores.append(f"{f.filename}: {res.error}")
                tracks.append({
                    "artista": track_artista or "",
                    "titulo": track_titulo or "",
                    "sample_local": fname,
                    "archivo": f.filename,
                })

            if total_cost > 0:
                _track_api_cost(total_cost)
                db.session.commit()

            if not tracks:
                flash("No se pudo procesar ningun video. " + " · ".join(errores[:3]), "error")
                return redirect(url_for("disc_new_from_video"))

            # Artista del disco: el comun a todos los tracks, o "Various" si difieren.
            _arts = [t["artista"] for t in tracks if t.get("artista")]
            if _arts:
                disc_fields["artista"] = _arts[0] if len(set(_arts)) == 1 else "Various"
            disc_fields["formato"] = "Vinyl"  # siempre vinilo
            # El titulo del EP no esta en el video: queda vacio para que lo ponga Facundo.

            session["video_prefill"] = disc_fields
            session["video_tracks"] = tracks
            session["video_info"] = {"cost_usd": round(total_cost, 4), "errores": errores}
            flash(f"{len(tracks)} video(s) procesado(s). Costo: ${total_cost:.4f} USD. Revisá y creá la ficha.", "success")
            return redirect(url_for("disc_video_review"))

        return render_template("disc_from_video.html", api_key_set=api_key_set)

    @app.route("/admin/discs/new/from-video/review", methods=["GET", "POST"])
    @auth.admin_required
    def disc_video_review():
        prefill = session.get("video_prefill") or {}
        tracks = session.get("video_tracks") or []
        if not tracks:
            flash("No hay datos de video para revisar. Subí los videos primero.", "error")
            return redirect(url_for("disc_new_from_video"))

        if request.method == "POST":
            from datetime import datetime as _dt
            def _s(k):
                v = (request.form.get(k) or "").strip()
                return v or None
            def _i(k):
                try:
                    return int(request.form.get(k))
                except (TypeError, ValueError):
                    return None

            # El titulo del EP NO es obligatorio (no sale del video). Si va vacio,
            # se pone un placeholder que Facundo edita despues en la ficha
            # (la columna del disco no admite null).
            titulo = _s("titulo") or "Sin título"
            precio = _i("precio_uyu") or 0
            disc = Disc(
                estado="Disponible", stock=1, origen="arcano",
                artista=_s("artista") or "Various", titulo=titulo, sello=_s("sello"),
                anio=_i("anio"), formato=_s("formato") or "Vinyl", genero=_s("genero"),
                tags=_s("catalogo"), precio_uyu=float(precio),
                fecha_ingreso=now_uy(),
            )
            db.session.add(disc)
            db.session.flush()
            db.session.add(StatusHistory(disc_id=disc.id, estado=disc.estado, nota="Ingreso al stock (desde video)"))

            # Solo aceptar sample_local que vengan de la sesion (anti-manipulacion)
            validos = {t["sample_local"] for t in tracks}
            n = _i("track_count") or 0
            creados = 0
            tl_lines = []  # tambien armamos el tracklist en TEXTO
            for idx in range(n):
                tit = (request.form.get(f"track_titulo_{idx}") or "").strip() or f"Track {idx + 1}"
                art = (request.form.get(f"track_artista_{idx}") or "").strip() or None
                sample = (request.form.get(f"track_sample_{idx}") or "").strip()
                sample = sample if sample in validos else None
                db.session.add(Track(
                    disc_id=disc.id, posicion=idx, titulo=tit[:300], artista=art,
                    sample_local=sample,
                ))
                creados += 1
                tl_lines.append(f"{idx + 1}. " + (f"{art} - {tit}" if art else tit))
            # Cargar los nombres tambien en el tracklist de texto
            if tl_lines:
                disc.tracklist = "\n".join(tl_lines)
            db.session.commit()
            session.pop("video_prefill", None)
            session.pop("video_tracks", None)
            session.pop("video_info", None)
            flash(f"Ficha creada con {creados} track(s) y sus samples. Agregale la portada si querés.", "success")
            return redirect(url_for("disc_edit", disc_id=disc.id) + "#tracks-section")

        return render_template(
            "disc_video_review.html",
            prefill=prefill, tracks=tracks,
            info=session.get("video_info") or {},
            tc=Setting.get_tc(),
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

            # Buscar portada/anio en Deejay.de EN PARALELO con un tope de tiempo total.
            # Antes era secuencial dentro del request (3 requests de red x disco); una
            # factura grande podia pasar el timeout de Render (120s) -> 502 y se perdia
            # todo el analisis (que ya se pago con IA). Ahora concurrente y con deadline:
            # las que no lleguen quedan sin portada (se agregan a mano despues).
            import time as _time  # noqa: F401
            from concurrent.futures import ThreadPoolExecutor, as_completed
            _cover_res = {}
            _pairs = [(_i, (d.get("artista") or "").strip(), (d.get("titulo") or "").strip())
                      for _i, d in enumerate(discos)]
            _pairs = [(i, a, t) for (i, a, t) in _pairs if a and t]
            if _pairs:
                _ex = ThreadPoolExecutor(max_workers=6)
                _futs = {_ex.submit(scraper.find_cover_and_year, a, t, covers_dir): i for (i, a, t) in _pairs}
                try:
                    for _f in as_completed(_futs, timeout=40):
                        try:
                            _cover_res[_futs[_f]] = _f.result()
                        except Exception:
                            pass
                except Exception:
                    pass  # se acabo el presupuesto de tiempo: seguimos con lo que haya
                _ex.shutdown(wait=False, cancel_futures=True)

            # Detectar duplicados existentes + armar los items
            items = []
            portadas_encontradas = 0
            for _idx, d in enumerate(discos):
                artista = (d.get("artista") or "").strip()
                titulo = (d.get("titulo") or "").strip()
                existente = None
                if artista and titulo:
                    existente = Disc.query.filter(
                        db.func.lower(Disc.artista) == artista.lower(),
                        db.func.lower(Disc.titulo) == titulo.lower(),
                    ).first()

                # Portada + anio: ya buscados en paralelo arriba
                cover_local_auto = None
                anio_auto = d.get("anio")
                found = _cover_res.get(_idx)
                if found:
                    if found.get("cover_local"):
                        cover_local_auto = found["cover_local"]
                        portadas_encontradas += 1
                    if not anio_auto and found.get("anio"):
                        anio_auto = found["anio"]

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
                # Los discos de una factura estan viajando desde Europa: por defecto
                # entran como Pre-order (no vendibles), NO como Disponible. El estado
                # viejo "En camino" ya no existe.
                estado = _str_or_none(request.form.get(prefix + "estado")) or "Pre-order"
                stock_nuevo = _parse_int(request.form.get(prefix + "stock"), 1) or 1
                duplicado_id = _parse_int(request.form.get(prefix + "duplicado_id"))
                # Portada (ya buscada al analizar, o subida manualmente en la revision)
                final_cover_local = _str_or_none(request.form.get(prefix + "cover_local"))

                if estado not in ESTADOS_DISCO:
                    estado = "Pre-order"

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
                        ahora = now_uy()
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
            covers, samples = set(), set()
            for d in discs:
                c, s = _archivos_de_disco(d)
                covers |= c
                samples |= s
            for d in discs:
                db.session.delete(d)
            db.session.commit()
            _purgar_archivos_huerfanos(
                app.config["COVERS_DIR"], app.config["SAMPLES_DIR"],
                app.static_folder, covers, samples,
            )
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
            campos_bool = ("destacado", "en_nuevos", "cartel_ultima", "mostrar_agotado", "es_preorder", "es_tip", "en_fire_sale")
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
                    "es_preorder": "Carrusel Pre-order", "en_fire_sale": "Fire Sale",
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
                ahora = now_uy()
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
        covers, samples = _archivos_de_disco(disc)
        db.session.delete(disc)
        db.session.commit()
        _purgar_archivos_huerfanos(
            app.config["COVERS_DIR"], app.config["SAMPLES_DIR"],
            app.static_folder, covers, samples,
        )
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
        nuevo.fecha_ingreso = now_uy()
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

        elif campo in ("destacado", "en_nuevos", "cartel_ultima", "mostrar_agotado", "es_preorder", "es_tip", "en_fire_sale"):
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

        elif campo in ("sello", "artista", "titulo", "genero", "formato"):
            nuevo = (valor or "").strip()
            # artista y titulo no pueden quedar vacios
            if campo in ("artista", "titulo") and not nuevo:
                return jsonify({"ok": False, "error": f"El {campo} no puede quedar vacio"}), 400
            setattr(disc, campo, nuevo or None)
            db.session.commit()
            return jsonify({"ok": True, "campo": campo, "valor": getattr(disc, campo) or ""})

        elif campo == "audio_estado":
            nuevo = (valor or "").strip()
            if nuevo and nuevo not in AUDIO_ESTADOS:
                return jsonify({"ok": False, "error": "Estado de audio invalido"}), 400
            disc.audio_estado = nuevo or None
            db.session.commit()
            return jsonify({"ok": True, "campo": campo, "valor": disc.audio_estado or ""})

        elif campo == "recalcular_tc":
            # Actualiza el TC del disco al TC global (EUR->UYU) actual y recalcula el costo.
            tc_actual = Setting.get_tc()
            disc.recalcular_costo_final(tc_actual)
            db.session.commit()
            return jsonify({
                "ok": True,
                "campo": campo,
                "tc_usado": disc.tc_usado,
                "costo_final_uyu": round(disc.costo_final_uyu or 0),
                "ganancia_uyu": round(disc.ganancia_uyu or 0),
                "margen_pct": round(disc.margen_pct or 0, 1),
            })

        elif campo == "metodo_pago_venta":
            nuevo = (valor or "").strip().lower()
            if nuevo and nuevo not in ("mercadopago", "transferencia", "efectivo", "otro"):
                return jsonify({"ok": False, "error": "Metodo invalido"}), 400
            disc.metodo_pago_venta = nuevo or None
            db.session.commit()
            return jsonify({"ok": True, "campo": campo, "valor": disc.metodo_pago_venta or ""})

        elif campo == "canal_venta":
            nuevo = (valor or "").strip().lower()
            if nuevo and nuevo not in ("web", "instagram", "presencial", "otro"):
                return jsonify({"ok": False, "error": "Canal invalido"}), 400
            disc.canal_venta = nuevo or None
            db.session.commit()
            return jsonify({"ok": True, "campo": campo, "valor": disc.canal_venta or ""})

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
        ahora = now_uy()
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

    def _nuevo_venta_grupo():
        """Id incremental para una venta manual agrupada ('g1', 'g2', ...)."""
        n = 0
        try:
            n = int(Setting.get("venta_grupo_seq", "0") or 0)
        except (ValueError, TypeError):
            n = 0
        n += 1
        Setting.set("venta_grupo_seq", str(n))
        return f"g{n}"

    _PERIODOS_VENTAS = [
        ("hoy", "Hoy"), ("7dias", "7 dias"), ("mes_actual", "Este mes"),
        ("mes_pasado", "Mes pasado"), ("anio", "Este ano"), ("todo", "Todo"),
    ]
    _CANALES_VENTA = [("web", "Web"), ("instagram", "Instagram"),
                      ("presencial", "Presencial"), ("otro", "Otro"), (None, "Sin definir")]
    _METODOS_VENTA = [("mercadopago", "Mercado Pago"), ("transferencia", "Transferencia"),
                      ("efectivo", "Efectivo"), ("otro", "Otro"), (None, "Sin definir")]

    def _ventas_maps():
        """Mapas desde las reservas: discos web, metodo y cliente por disco."""
        web_ids, metodo_res, cliente_res, reserva_discs = set(), {}, {}, {}
        try:
            for r in Reserva.query.all():
                met = ("mercadopago" if (r.pagado or r.mp_status == "approved" or r.metodo_pago == "mercadopago")
                       else ("transferencia" if r.metodo_pago == "transferencia" else None))
                for it in r.items_efectivos:
                    if it.disc_id:
                        web_ids.add(it.disc_id)
                        reserva_discs.setdefault(r.id, []).append(it.disc_id)
                        if met:
                            metodo_res.setdefault(it.disc_id, met)
                        cliente_res.setdefault(it.disc_id, {"nombre": r.nombre, "reserva_id": r.id})
        except Exception:
            pass
        return web_ids, metodo_res, cliente_res, reserva_discs

    @app.route("/admin/ventas")
    @auth.admin_required
    def ventas_panel():
        """Panel de ventas profesional: KPIs con comparacion vs periodo anterior,
        desglose por canal y metodo, tendencia diaria y lista por transaccion."""
        from datetime import datetime as _dt, timedelta as _td
        periodo = request.args.get("periodo", "mes_actual")
        desde = request.args.get("desde") or None
        hasta = request.args.get("hasta") or None
        f_canal = (request.args.get("canal") or "").strip()
        f_metodo = (request.args.get("metodo") or "").strip()
        f_q = (request.args.get("q") or "").strip()

        inicio, fin = metrics._rango_ventas(periodo, desde, hasta)
        inicio_dt = _dt(inicio.year, inicio.month, inicio.day)
        fin_dt = _dt(fin.year, fin.month, fin.day)
        dur = fin_dt - inicio_dt
        inicio_prev_dt, fin_prev_dt = inicio_dt - dur, inicio_dt

        web_ids, metodo_res, cliente_res, reserva_discs = _ventas_maps()

        def canal_de(d):
            return d.canal_venta or ("web" if d.id in web_ids else None)

        def metodo_de(d):
            return d.metodo_pago_venta or metodo_res.get(d.id)

        vendidos = (
            Disc.query.filter(Disc.estado == "Vendido")
            .filter(Disc.fecha_venta >= inicio_dt, Disc.fecha_venta < fin_dt)
            .order_by(Disc.fecha_venta.desc(), Disc.id.desc()).all()
        )
        # Backfill venta_grupo por reserva (2+ discos), idempotente.
        by_id = {d.id: d for d in vendidos}
        cambios = False
        for rid, dids in reserva_discs.items():
            vend = [by_id[i] for i in dids if i in by_id]
            if len(vend) >= 2:
                for d in vend:
                    if not d.venta_grupo:
                        d.venta_grupo = f"r{rid}"
                        cambios = True
        if cambios:
            db.session.commit()

        kpis = metrics.kpis_de_discos(vendidos)
        vendidos_prev = (
            Disc.query.filter(Disc.estado == "Vendido")
            .filter(Disc.fecha_venta >= inicio_prev_dt, Disc.fecha_venta < fin_prev_dt).all()
        )
        kpis_prev = metrics.kpis_de_discos(vendidos_prev)

        def _delta(cur, prev):
            return round((cur - prev) / prev * 100) if prev else None
        comp = {k: _delta(kpis[k], kpis_prev[k]) for k in ("ventas", "facturado", "ganancia")}

        def _agrupar(keyfn, defs):
            acc = {}
            for d in vendidos:
                k = keyfn(d)
                a = acc.setdefault(k, {"discos": 0, "monto": 0.0})
                a["discos"] += d.copias_facturadas
                a["monto"] += (d.precio_mostrar or d.precio_uyu or 0) * d.copias_facturadas
            total = sum(a["monto"] for a in acc.values()) or 1
            out = [{"key": k or "sin", "label": label, "discos": acc[k]["discos"],
                    "monto": round(acc[k]["monto"], 2), "pct": round(acc[k]["monto"] / total * 100)}
                   for k, label in defs if k in acc]
            return sorted(out, key=lambda x: x["monto"], reverse=True)
        por_canal = _agrupar(canal_de, _CANALES_VENTA)
        por_metodo = _agrupar(metodo_de, _METODOS_VENTA)

        # Tendencia: facturado por dia (ultimos N dias del periodo, sin dibujar futuro).
        ndias = max(7, min(dur.days, 30))
        base_fin = min(fin_dt.date(), today_uy() + _td(days=1))
        dias = [base_fin - _td(days=i + 1) for i in range(ndias)][::-1]
        tmap = {dd: 0.0 for dd in dias}
        for d in vendidos:
            fv = d.fecha_venta.date() if d.fecha_venta else None
            if fv in tmap:
                tmap[fv] += (d.precio_mostrar or d.precio_uyu or 0) * d.copias_facturadas
        trend = [{"label": dd.strftime("%d/%m"), "monto": round(tmap[dd])} for dd in dias]
        trend_max = max((t["monto"] for t in trend), default=0) or 1

        def _top(attr):
            acc = {}
            for d in vendidos:
                k = (getattr(d, attr) or "").strip()
                if k:
                    acc[k] = acc.get(k, 0) + (d.precio_mostrar or d.precio_uyu or 0) * d.copias_facturadas
            if not acc:
                return None
            k = max(acc, key=acc.get)
            return {"nombre": k, "monto": round(acc[k])}
        top_sello, top_artista = _top("sello"), _top("artista")

        # Lista filtrada (canal / metodo / buscador)
        ql = f_q.lower()

        def _pasa(d):
            if f_canal and (canal_de(d) or "sin") != f_canal:
                return False
            if f_metodo and (metodo_de(d) or "sin") != f_metodo:
                return False
            if ql:
                cli = (cliente_res.get(d.id) or {}).get("nombre", "")
                blob = f"{d.artista or ''} {d.titulo or ''} {d.sello or ''} {cli}".lower()
                if ql not in blob:
                    return False
            return True
        lista = [d for d in vendidos if _pasa(d)]

        if request.args.get("export") == "csv":
            return _ventas_csv(lista, canal_de, metodo_de, cliente_res)

        # 1) Agrupar por TRANSACCION primero (misma reserva/grupo = una venta), aunque
        #    sus discos se hayan vendido en dias distintos. La fecha de la venta = la
        #    de su ultima entrega. Asi no se parte una venta en varios dias.
        trans_map, orden = {}, []
        for d in lista:
            key = d.venta_grupo or ("s%d" % d.id)
            if key not in trans_map:
                trans_map[key] = {"grupo": d.venta_grupo, "discos": [], "subtotal": 0.0,
                                  "ganancia": 0.0, "cliente": None, "fecha": None}
                orden.append(key)
            t = trans_map[key]
            t["discos"].append(d)
            t["subtotal"] += (d.precio_mostrar or d.precio_uyu or 0) * d.copias_facturadas
            # Ganancia con costo por origen (personal = 0), MISMO criterio que el KPI de
            # arriba (kpis_de_discos) y que Contabilidad, para que la suma de las filas
            # coincida con el total del panel.
            t["ganancia"] += ((d.precio_mostrar or d.precio_uyu or 0) - metrics._costo_negocio(d)) * d.copias_facturadas
            if not t["cliente"] and cliente_res.get(d.id):
                t["cliente"] = cliente_res.get(d.id)
            fv = d.fecha_venta.date() if d.fecha_venta else None
            if fv and (t["fecha"] is None or fv > t["fecha"]):
                t["fecha"] = fv
        from datetime import date as _date
        transacciones = []
        for k in orden:
            t = trans_map[k]
            fechas = {d.fecha_venta.date() for d in t["discos"] if d.fecha_venta}
            t["multi"] = len(t["discos"]) > 1
            t["multidia"] = len(fechas) > 1
            transacciones.append(t)
        transacciones.sort(key=lambda t: (t["fecha"] or _date.min), reverse=True)

        # 2) Ubicar cada transaccion en el dia de su fecha (la ultima entrega).
        grupos, actual = [], None
        for t in transacciones:
            if actual is None or actual["fecha"] != t["fecha"]:
                actual = {"fecha": t["fecha"], "transacciones": [], "subtotal": 0.0,
                          "n_ventas": 0, "n_discos": 0}
                grupos.append(actual)
            actual["transacciones"].append(t)
            actual["subtotal"] += t["subtotal"]
            actual["n_ventas"] += 1
            actual["n_discos"] += len(t["discos"])

        dias_venta = {d.id: max((d.fecha_venta - d.fecha_ingreso).days, 0)
                      for d in lista if d.fecha_ingreso and d.fecha_venta}
        periodo_label = dict(_PERIODOS_VENTAS).get(periodo, "Este mes")

        return render_template(
            "admin_ventas.html",
            periodo=periodo, periodo_label=periodo_label, periodos=_PERIODOS_VENTAS,
            desde=desde or "", hasta=hasta or "",
            kpis=kpis, comp=comp,
            por_canal=por_canal, por_metodo=por_metodo,
            trend=trend, trend_max=trend_max,
            top_sello=top_sello, top_artista=top_artista,
            grupos=grupos,
            metodo_por_disc=metodo_res, disc_ids_web=web_ids,
            cliente_por_disc=cliente_res, dias_venta=dias_venta,
            canales=_CANALES_VENTA, metodos=_METODOS_VENTA,
            filtros={"canal": f_canal, "metodo": f_metodo, "q": f_q},
            hoy=today_uy(),
        )

    def _ventas_csv(lista, canal_de, metodo_de, cliente_res):
        """Exporta la lista de ventas filtrada como CSV."""
        import csv as _csv
        import io as _io
        buf = _io.StringIO()
        w = _csv.writer(buf)
        w.writerow(["Fecha venta", "Artista", "Titulo", "Sello", "Origen",
                    "Canal", "Metodo", "Cliente", "Precio UYU", "Ganancia UYU", "Grupo"])
        _canal_lbl = dict(_CANALES_VENTA)
        _met_lbl = dict(_METODOS_VENTA)
        for d in lista:
            cli = (cliente_res.get(d.id) or {}).get("nombre", "")
            w.writerow([
                d.fecha_venta.strftime("%Y-%m-%d") if d.fecha_venta else "",
                d.artista or "", d.titulo or "", d.sello or "",
                "personal" if d.origen == "personal" else "arcano",
                _canal_lbl.get(canal_de(d), ""), _met_lbl.get(metodo_de(d), ""),
                cli, round(d.precio_mostrar or d.precio_uyu or 0),
                round((d.ganancia_uyu or 0) * d.copias_facturadas),
                d.venta_grupo or "",
            ])
        from flask import Response as _Resp
        return _Resp(buf.getvalue(), mimetype="text/csv",
                     headers={"Content-Disposition": "attachment; filename=ventas_arcano.csv"})

    @app.route("/admin/ventas/agrupar", methods=["POST"])
    @auth.admin_required
    def ventas_agrupar():
        """Marca varios discos vendidos como UNA sola venta (comparten venta_grupo).
        Si alguno ya estaba en un grupo, se suman a ese grupo."""
        ids = [int(x) for x in (request.form.get("ids") or "").split(",") if x.strip().isdigit()]
        discs = Disc.query.filter(Disc.id.in_(ids), Disc.estado == "Vendido").all() if ids else []
        if len(discs) < 2:
            flash("Elegí al menos 2 discos vendidos para marcarlos como una sola venta.", "warning")
            return redirect(url_for("ventas_panel"))
        grupo = next((d.venta_grupo for d in discs if d.venta_grupo), None) or _nuevo_venta_grupo()
        # El comprador del grupo: el de algun disco que ya lo tenga, o el de la reserva
        # si el grupo es 'r<id>'. Al agrupar, los discos sin comprador lo heredan, asi
        # el panel de Clientes (que atribuye por disco) suma la venta al cliente correcto.
        # Si mezclas discos de DOS compradores distintos, no adivinamos (no se propaga).
        _distintos = set(d.cliente_id for d in discs if getattr(d, "cliente_id", None))
        cli_id = next(iter(_distintos), None) if len(_distintos) <= 1 else None
        if not cli_id and len(_distintos) <= 1 and grupo.startswith("r"):
            try:
                _r = Reserva.query.get(int(grupo[1:]))
                cli_id = _r.cliente_id if _r else None
            except (ValueError, TypeError):
                cli_id = None
        for d in discs:
            d.venta_grupo = grupo
            if cli_id and not getattr(d, "cliente_id", None):
                d.cliente_id = cli_id
        db.session.commit()
        flash(f"Listo: {len(discs)} discos quedaron como una sola venta.", "success")
        return redirect(url_for("ventas_panel"))

    @app.route("/admin/ventas/desagrupar", methods=["POST"])
    @auth.admin_required
    def ventas_desagrupar():
        """Deshace una agrupacion: cada disco del grupo vuelve a contar por separado."""
        grupo = (request.form.get("grupo") or "").strip()
        if grupo:
            Disc.query.filter_by(venta_grupo=grupo).update({"venta_grupo": None})
            db.session.commit()
            flash("Venta desagrupada: cada disco cuenta por separado.", "success")
        return redirect(url_for("ventas_panel"))

    @app.route("/admin/ventas/cargar", methods=["GET", "POST"])
    @auth.admin_required
    def ventas_cargar():
        """Carga una venta manual (tipica de Instagram) en UN paso: elegis discos y
        se marcan Vendidos + canal + metodo + agrupados como una sola venta."""
        if request.method == "POST":
            ids = [int(x) for x in request.form.getlist("disc_ids") if str(x).isdigit()]
            canal = (request.form.get("canal") or "instagram").strip().lower()
            metodo = (request.form.get("metodo") or "").strip().lower()
            fecha_str = (request.form.get("fecha") or "").strip()
            if canal not in ("web", "instagram", "presencial", "otro"):
                canal = "instagram"
            if metodo and metodo not in ("mercadopago", "transferencia", "efectivo", "otro"):
                metodo = None
            try:
                fecha_venta = datetime.strptime(fecha_str, "%Y-%m-%d") if fecha_str else now_uy()
            except ValueError:
                fecha_venta = now_uy()
            # Cliente OPCIONAL (por si todavia no lo tenés cargado)
            cli_nombre = (request.form.get("cliente_nombre") or "").strip() or None
            cli_wa = (request.form.get("cliente_whatsapp") or "").strip() or None
            cli_ig = (request.form.get("cliente_instagram") or "").strip() or None
            cli_email = (request.form.get("cliente_email") or "").strip() or None
            discs = Disc.query.filter(Disc.id.in_(ids)).all() if ids else []
            if not discs:
                flash("Elegí al menos un disco para cargar la venta.", "warning")
                return redirect(url_for("ventas_cargar"))
            grupo = _nuevo_venta_grupo() if len(discs) > 1 else None
            for d in discs:
                register_status_change(d, "Vendido", nota="Venta manual cargada")
                d.fecha_venta = fecha_venta
                d.stock = max(0, (d.stock or 1) - 1)
                d.canal_venta = canal
                if metodo:
                    d.metodo_pago_venta = metodo
                if grupo:
                    d.venta_grupo = grupo
            # Atar la compra a un cliente (si cargaste sus datos)
            _atribuir_compra(discs, cli_nombre, cli_wa, cli_email, cli_ig,
                             origen=("web" if canal == "web" else "manual"))
            db.session.commit()
            n = len(discs)
            flash(f"Venta cargada: {n} disco{'s' if n != 1 else ''}" + (" (como una sola venta)." if grupo else "."), "success")
            return redirect(url_for("ventas_panel"))

        # GET: discos que todavia se pueden vender
        disponibles = (
            Disc.query
            .filter(Disc.estado.in_(["Disponible", "Reservado", "Pre-order", "Apartado para mi"]))
            .order_by(Disc.artista.asc(), Disc.titulo.asc()).all()
        )
        return render_template(
            "admin_venta_cargar.html",
            discos=disponibles,
            hoy=today_uy().strftime("%Y-%m-%d"),
        )

    @app.route("/admin/contabilidad")
    @auth.admin_required
    def contabilidad():
        from models import Egreso, EgresoRecurrente, CAT_RETIRO_PERSONAL
        periodo = request.args.get("periodo", "mes_actual")
        desde = request.args.get("desde")
        hasta = request.args.get("hasta")
        data = metrics.get_all_metrics()
        conta = metrics.contabilidad(periodo, desde, hasta)
        # Lista de egresos del periodo para mostrar. Los retiros personales van aparte:
        # no son gasto operativo (no bajan la ganancia), se descuentan del capital.
        inicio, fin = metrics._rango_periodo(periodo, desde, hasta)
        egresos_all = (
            Egreso.query
            .filter(Egreso.fecha >= inicio, Egreso.fecha < fin)
            .order_by(Egreso.fecha.desc())
            .all()
        )
        egresos = [e for e in egresos_all if e.categoria != CAT_RETIRO_PERSONAL]
        retiros = [e for e in egresos_all if e.categoria == CAT_RETIRO_PERSONAL]
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
            retiros=retiros,
            recurrentes=recurrentes,
            categorias=Setting.get_categorias_egreso(),
            cat_retiro=CAT_RETIRO_PERSONAL,
            monedas=["UYU", "USD", "EUR"],
            por_cobrar=por_cobrar,
            ingresos_egresos_6m=ingresos_egresos_6m,
            pedidos_lista=pedidos_lista,
            recuperacion=metrics.recuperacion_inversion(),
            stock_frenado=metrics.stock_frenado(),
            arcano_vs_personal=metrics.arcano_vs_personal_6m(),
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
            fecha = _dt.strptime(fecha_str, "%Y-%m-%d").date() if fecha_str else today_uy()
        except ValueError:
            fecha = today_uy()

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

    @app.route("/admin/contabilidad/egreso/<int:egreso_id>/editar", methods=["POST"])
    @auth.admin_required
    def egreso_editar(egreso_id):
        """Edita TODOS los campos de un gasto: fecha, categoria, descripcion, monto,
        moneda y el monto real en UYU. Si el campo 'En pesos' se deja vacio, se
        recalcula solo desde el monto original y el tipo de cambio."""
        from models import Egreso
        from datetime import datetime as _dt
        e = Egreso.query.get_or_404(egreso_id)

        fecha_str = (request.form.get("fecha") or "").strip()
        if fecha_str:
            try:
                e.fecha = _dt.strptime(fecha_str, "%Y-%m-%d").date()
            except ValueError:
                pass

        cat = (request.form.get("categoria") or "").strip()
        nueva_cat = (request.form.get("categoria_nueva") or "").strip()
        if nueva_cat:
            Setting.add_categoria_egreso(nueva_cat)
            e.categoria = nueva_cat
        elif cat:
            e.categoria = cat

        e.descripcion = (request.form.get("descripcion") or "").strip() or None

        try:
            monto = float(request.form.get("monto") or e.monto)
        except (ValueError, TypeError):
            monto = e.monto
        if monto < 0:
            monto = e.monto
        moneda = (request.form.get("moneda") or e.moneda).strip().upper()
        if moneda not in ("UYU", "USD", "EUR"):
            moneda = e.moneda
        e.monto = monto
        e.moneda = moneda

        # Monto en pesos: si viene un valor, se respeta; si viene vacio, se recalcula.
        raw = (request.form.get("monto_uyu") or "").strip().replace(".", "").replace(",", ".")
        if raw:
            try:
                val = float(raw)
                if val >= 0:
                    e.monto_uyu = val
            except (ValueError, TypeError):
                pass
        else:
            monto_uyu, tc = _convertir_a_uyu(monto, moneda)
            e.monto_uyu = monto_uyu
            e.tc_usado = tc

        db.session.commit()
        flash("Gasto actualizado.", "success")
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
        hoy = today_uy()
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
        """Exporta TODA la contabilidad del periodo a Excel (multi-hoja con graficos)."""
        import informe_excel
        from flask import send_file
        periodo = request.args.get("periodo", "mes_actual")
        desde = request.args.get("desde")
        hasta = request.args.get("hasta")
        _sincronizar_caja()    # que la caja del informe coincida con la pantalla
        _rc = _resumen_caja()  # mismo numero que la pantalla de Caja
        buf = informe_excel.generar_excel(
            periodo, desde, hasta,
            caja_disponible=_rc.get("disponible"), mercaderia_uyu=_rc.get("mercaderia_uyu"))
        filename = f"arcano_contabilidad_{periodo}.xlsx"
        return send_file(
            buf, as_attachment=True, download_name=filename,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.route("/admin/contabilidad/informe-pdf")
    @auth.admin_required
    def contabilidad_informe_pdf():
        """Genera el informe PDF de contabilidad del periodo elegido."""
        import informe
        from flask import Response
        periodo = request.args.get("periodo", "mes_actual")
        desde = request.args.get("desde") or None
        hasta = request.args.get("hasta") or None
        _sincronizar_caja()    # que la caja del informe coincida con la pantalla
        _rc = _resumen_caja()  # mismo numero que la pantalla de Caja
        buf = informe.generar_informe_pdf(
            periodo, desde, hasta,
            caja_disponible=_rc.get("disponible"), mercaderia_uyu=_rc.get("mercaderia_uyu"))
        filename = f"arcano_informe_{periodo}.pdf"
        return Response(
            buf.getvalue(),
            mimetype="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @app.route("/admin/informe-ejecutivo.pdf")
    @auth.admin_required
    def informe_ejecutivo_pdf():
        """PDF ejecutivo con TODA la gestion: resumen + Ventas + Contabilidad + Caja +
        Pedidos + Radar, con tarjetas, graficos y descripciones. Diseno de presentacion."""
        import informe_ejecutivo
        from flask import Response
        periodo = request.args.get("periodo", "mes_actual")
        desde = request.args.get("desde") or None
        hasta = request.args.get("hasta") or None
        _sincronizar_caja()         # que la caja del informe coincida con la pantalla
        resumen = _resumen_caja()  # foto de la caja a hoy
        buf = informe_ejecutivo.generar_pdf_ejecutivo(periodo, desde, hasta, resumen)
        stamp = datetime.utcnow().strftime("%Y%m%d")
        filename = f"arcano_informe_ejecutivo_{stamp}.pdf"
        return Response(
            buf.getvalue(),
            mimetype="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    # ---- CAJA / BANCO (libro de movimientos de plata) ----
    def _parse_monto_caja(raw):
        """Convierte un texto de monto a float, en formato es-UY (punto=miles, coma=decimal).
        Ej: '12.345' -> 12345 · '1.234.567' -> 1234567 · '1.500,50' -> 1500.5 · '1500,50' -> 1500.5
        Ojo con el caso ambiguo de UN solo punto sin coma: si tiene 3 digitos despues es
        separador de miles ('12.345' = 12345); si no, se toma como decimal ('1500.50' = 1500.5)."""
        s = (raw or "").strip().replace(" ", "").replace("$", "")
        if not s:
            return 0.0
        if "," in s:
            # coma = decimal; los puntos son miles
            s = s.replace(".", "").replace(",", ".")
        elif s.count(".") > 1:
            # varios puntos sin coma: todos son miles (1.234.567)
            s = s.replace(".", "")
        elif "." in s:
            ent, _, dec = s.partition(".")
            if len(dec) == 3 and dec.isdigit():
                # 12.345 -> separador de miles es-UY
                s = ent + dec
            # si no (1500.50, 0.5): se deja como decimal
        try:
            return float(s)
        except ValueError:
            return 0.0

    def _sincronizar_caja():
        """Sincroniza los movimientos automaticos de caja (ventas, pedidos, gastos):
        crea los que falten, ACTUALIZA los que cambiaron en su origen (ej: se edito
        el precio de un disco o el costo de un pedido) y borra los que ya no aplican.
        Idempotente. Los movimientos que Facundo edito a mano (manual_override=True)
        NO se pisan. Devuelve (creados, actualizados, borrados)."""
        from models import MovimientoCaja, Disc, Pedido, Egreso, Reserva
        from datetime import datetime as _dt

        # Movimientos automaticos ya existentes, indexados por (origen, origen_id)
        existentes = {}
        for m in MovimientoCaja.query.filter(MovimientoCaja.origen != "manual").all():
            existentes[(m.origen, m.origen_id)] = m

        creados = 0
        actualizados = 0
        vigentes = set()  # claves auto que siguen siendo validas

        def _difiere(a, b):
            return abs((a or 0) - (b or 0)) > 0.005

        # --- Mapa disco -> metodo de pago, desde las reservas ---
        metodo_por_disc = {}
        try:
            for r in Reserva.query.all():
                met = None
                if r.pagado or (r.mp_status == "approved") or (r.metodo_pago == "mercadopago"):
                    met = "mercadopago"
                elif r.metodo_pago == "transferencia":
                    met = "transferencia"
                # 'coordinar' u otro -> lo deja sin especificar para que Facundo lo defina
                if not met:
                    continue
                for it in r.items_efectivos:
                    if it.disc_id and it.disc_id not in metodo_por_disc:
                        metodo_por_disc[it.disc_id] = met
        except Exception:
            pass

        # --- VENTAS (ingresos): discos Vendido con fecha ---
        vendidos = (
            Disc.query
            .filter(Disc.estado == "Vendido")
            .filter(Disc.fecha_venta.isnot(None))
            .all()
        )
        for d in vendidos:
            key = ("venta", d.id)
            vigentes.add(key)
            monto = round((d.precio_mostrar or d.precio_uyu or 0) * d.copias_facturadas, 2)
            concepto = (f"Venta: {d.artista or ''} - {d.titulo or ''}".strip(" -") or "Venta")[:300]
            fecha = d.fecha_venta.date() if hasattr(d.fecha_venta, "date") else d.fecha_venta
            # El metodo de la venta lo manda el disco (editable en el panel de Ventas);
            # si no esta seteado, se infiere de la reserva web.
            metodo = d.metodo_pago_venta or metodo_por_disc.get(d.id)
            mov = existentes.get(key)
            if mov is None:
                db.session.add(MovimientoCaja(
                    fecha=fecha, tipo="ingreso", monto_uyu=monto,
                    concepto=concepto, metodo=metodo,
                    categoria=None, origen="venta", origen_id=d.id,
                ))
                creados += 1
            else:
                if not mov.manual_override:
                    # El origen manda: si cambio el precio/fecha, actualizamos la caja.
                    if _difiere(mov.monto_uyu, monto) or mov.fecha != fecha or mov.tipo != "ingreso":
                        mov.monto_uyu = monto
                        mov.fecha = fecha
                        mov.tipo = "ingreso"
                        mov.concepto = concepto
                        actualizados += 1
                # El metodo de una venta lo manda SIEMPRE el disco (fuente unica),
                # aun con manual_override: asi la caja y el panel de Ventas quedan
                # sincronizados en ambos sentidos.
                if metodo and mov.metodo != metodo:
                    mov.metodo = metodo
                    actualizados += 1

        # --- PEDIDOS (egresos): compras de discos ---
        for p in Pedido.query.all():
            key = ("pedido", p.id)
            vigentes.add(key)
            numero = (p.numero or "").strip()
            concepto = (f"Pedido {numero}".strip() if numero else "Compra de discos")
            if p.proveedor:
                concepto += f" ({p.proveedor})"
            concepto = concepto[:300]
            fecha = p.fecha or (p.creado.date() if p.creado else today_uy())
            monto = round(p.costo_total_uyu or 0, 2)
            mov = existentes.get(key)
            if mov is None:
                db.session.add(MovimientoCaja(
                    fecha=fecha, tipo="egreso", monto_uyu=monto,
                    concepto=concepto, metodo=None, categoria="Pedido",
                    origen="pedido", origen_id=p.id,
                ))
                creados += 1
            elif not mov.manual_override:
                if _difiere(mov.monto_uyu, monto) or mov.fecha != fecha or mov.tipo != "egreso":
                    mov.monto_uyu = monto
                    mov.fecha = fecha
                    mov.tipo = "egreso"
                    mov.concepto = concepto
                    actualizados += 1

        # --- GASTOS (egresos): egresos por categoria ---
        for e in Egreso.query.all():
            key = ("gasto", e.id)
            vigentes.add(key)
            concepto = ((e.descripcion or "").strip() or e.categoria or "Gasto")[:300]
            monto = round(e.monto_uyu or 0, 2)
            mov = existentes.get(key)
            if mov is None:
                db.session.add(MovimientoCaja(
                    fecha=e.fecha, tipo="egreso", monto_uyu=monto,
                    concepto=concepto, metodo=None, categoria=e.categoria,
                    origen="gasto", origen_id=e.id,
                ))
                creados += 1
            elif not mov.manual_override:
                if (_difiere(mov.monto_uyu, monto) or mov.fecha != e.fecha
                        or mov.categoria != e.categoria):
                    mov.monto_uyu = monto
                    mov.fecha = e.fecha
                    mov.categoria = e.categoria
                    mov.concepto = concepto
                    actualizados += 1

        # --- ENVIOS (ingresos): el $50 del envio cobrado, cuando la venta se concreta ---
        # Entra a la caja recien cuando hay plata de verdad: MP pagado o reserva
        # Pagada. Antes de eso NO cuenta (todavia no entro).
        for r in Reserva.query.filter(Reserva.envio_costo_uyu > 0).all():
            monto = round(r.envio_costo_uyu or 0, 2)
            if monto <= 0:
                continue
            if not (bool(r.pagado) or r.estado == "Pagada"):
                continue  # todavia no entro la plata; la limpieza saca un mov viejo si lo hubiera
            key = ("envio", r.id)
            vigentes.add(key)
            # Metodo REAL de la reserva (no derivar de 'pagado', que ahora es True para
            # toda venta Pagada y etiquetaba el envio como MP aunque fuera transferencia).
            metodo = ("mercadopago" if r.metodo_pago == "mercadopago"
                      else ("transferencia" if r.metodo_pago == "transferencia"
                      else ("efectivo" if r.metodo_pago == "efectivo" else None)))
            _fbase = r.fecha_actualizacion or r.fecha
            fecha = _fbase.date() if hasattr(_fbase, "date") else _fbase
            concepto = f"Envio (pedido #{r.id})"[:300]
            mov = existentes.get(key)
            if mov is None:
                db.session.add(MovimientoCaja(
                    fecha=fecha, tipo="ingreso", monto_uyu=monto,
                    concepto=concepto, metodo=metodo, categoria="Envio",
                    origen="envio", origen_id=r.id,
                ))
                creados += 1
            elif not mov.manual_override:
                if _difiere(mov.monto_uyu, monto) or mov.tipo != "ingreso" or mov.categoria != "Envio":
                    mov.monto_uyu = monto
                    mov.tipo = "ingreso"
                    mov.categoria = "Envio"
                    mov.concepto = concepto
                    actualizados += 1
                if metodo and not mov.metodo:
                    mov.metodo = metodo
                    actualizados += 1

        # --- LIMPIEZA: auto-movimientos cuyo origen ya no existe/aplica ---
        borrados = 0
        for clave, mov in existentes.items():
            if clave not in vigentes:
                db.session.delete(mov)
                borrados += 1

        if creados or borrados or actualizados:
            db.session.commit()
        return creados, actualizados, borrados

    def _resumen_caja(mes=None):
        """Arma el resumen de la caja: saldo actual, totales, ingresos por metodo
        y la lista de movimientos (mas reciente primero) con saldo corriente.

        Si se pasa `mes` ('YYYY-MM'), la LISTA de movimientos y el desglose se
        filtran a ese mes; los cuadros de arriba (saldo, MP, mercaderia) siguen
        siendo al dia."""
        from models import MovimientoCaja, CAT_RETIRO_PERSONAL
        movs = (
            MovimientoCaja.query
            .order_by(MovimientoCaja.fecha.asc(), MovimientoCaja.id.asc())
            .all()
        )
        # Meses con movimientos (para el selector) + determinacion del filtro
        _nombres_mes = ["", "Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
        _meses_set = sorted({(m.fecha.year, m.fecha.month) for m in movs if m.fecha}, reverse=True)
        meses_disponibles = [{"val": "%04d-%02d" % (y, mm), "label": "%s %d" % (_nombres_mes[mm], y)} for (y, mm) in _meses_set]
        fy = fm = None
        if mes:
            try:
                fy, fm = int(mes[:4]), int(mes[5:7])
            except (ValueError, TypeError):
                fy = fm = None
        mes_activo = fy is not None and fm is not None
        try:
            saldo_inicial = float(Setting.get("caja_saldo_inicial") or 0)
        except (ValueError, TypeError):
            saldo_inicial = 0.0

        # Los adelantos de MP son un PUENTE, no plata nueva: el adelanto y la venta por
        # MP son la MISMA plata (vos adelantas lo que MP te va a pagar). Por eso el
        # adelanto NO cuenta como ingreso en el saldo ni en la lista (si contara, esa
        # plata quedaria contada dos veces: la venta + el adelanto). En cambio, adelantar
        # "mueve" esa venta de Mercado Pago al banco (baja mp_pendiente). Se muestran
        # aparte en "Adelantos a recuperar". Por eso el saldo de la lista = el numero grande.
        movs_dinero = [m for m in movs if not (m.adelanto_mp or "")]

        total_ingresos = sum(m.monto_uyu or 0 for m in movs_dinero if m.tipo == "ingreso")
        total_egresos = sum(m.monto_uyu or 0 for m in movs_dinero if m.tipo == "egreso")
        saldo_actual = saldo_inicial + total_ingresos - total_egresos

        # Capital aportado por Facundo (plata propia, no es facturacion). total_aportes
        # incluye los adelantos pendientes y se usa para separar ventas de aportes.
        total_aportes = sum(m.monto_uyu or 0 for m in movs_dinero if m.tipo == "ingreso" and m.es_aporte)
        # Para MOSTRAR "Capital que pusiste": solo el capital PERMANENTE, sin los
        # adelantos de MP (que son plata temporal que Facundo recupera y ya se
        # muestran aparte en "Adelantos a recuperar"). Asi el capital no salta al adelantar.
        capital_aportado = sum(
            m.monto_uyu or 0 for m in movs_dinero
            if m.tipo == "ingreso" and m.es_aporte and not (m.adelanto_mp or "")
        )
        # Retiros personales: plata que Facundo saca para si (categoria especial en los
        # gastos). Afecta el capital al REVES que un aporte: lo resta. Ya sale de la caja
        # como cualquier egreso (baja el saldo), pero ademas baja el "Capital que pusiste".
        total_retiros_personales = sum(
            m.monto_uyu or 0 for m in movs_dinero
            if m.tipo == "egreso" and m.origen == "gasto" and m.categoria == CAT_RETIRO_PERSONAL
        )
        capital_permanente = capital_aportado - total_retiros_personales
        # Ventas cobradas = ingresos que NO son aporte de capital
        total_ventas = total_ingresos - total_aportes

        # Ventas cobradas agrupadas por metodo (excluye aportes de capital)
        por_metodo = {"mercadopago": 0.0, "transferencia": 0.0, "efectivo": 0.0, "otro": 0.0, "sin": 0.0}
        for m in movs_dinero:
            if m.tipo != "ingreso" or m.es_aporte:
                continue
            k = m.metodo if m.metodo in ("mercadopago", "transferencia", "efectivo", "otro") else "sin"
            por_metodo[k] += (m.monto_uyu or 0)

        # Plata todavia retenida en Mercado Pago = ventas por MP de los ultimos 30 dias
        # MENOS lo que ya adelantaste (esas ventas ya las tenes en el banco via tu
        # adelanto) y lo que MP ya pago (adelantos saldados). Cuando adelantaste todo,
        # queda 0: no hay nada de MP que no tengas ya en el banco.
        from datetime import datetime as _dt2, timedelta as _td
        limite_mp = today_uy() - _td(days=30)
        mp_bruto = sum(
            (m.monto_uyu or 0) for m in movs
            if m.tipo == "ingreso" and not m.es_aporte
            and m.metodo == "mercadopago" and m.fecha and m.fecha > limite_mp
        )
        adelanto_todos = sum(m.monto_uyu or 0 for m in movs if (m.adelanto_mp or ""))
        mp_pendiente = max(0.0, mp_bruto - adelanto_todos)
        # Adelantos de MP: aportes que Facundo hizo para tener la liquidez de MP
        # antes de que MP pague. Los 'pendiente' todavia esperan el pago de MP.
        adelantos_pendientes = [m for m in movs if (m.adelanto_mp or "") == "pendiente"]
        adelanto_pendiente_total = sum(m.monto_uyu or 0 for m in adelantos_pendientes)
        # Lo que hay en MP y todavia no adelantaste (para no adelantar dos veces)
        por_adelantar = max(0.0, mp_pendiente - adelanto_pendiente_total)
        # PLATA EN LA MANO HOY = saldo - lo que todavia esta retenido en Mercado Pago.
        # La venta por MP NO se cuenta como plata en mano hasta que MP la deposita
        # (modelo confirmado por Facundo). El adelanto que pusiste ya esta en el saldo
        # como tu plata; NO se suma de nuevo (eso era contar dos veces la misma plata).
        # Ej: saldo 12.728 - MP 3.900 = 8.828 en la mano.
        disponible = saldo_actual - mp_pendiente

        # Origen del disco (arcano / personal) de cada movimiento de venta
        from models import Disc
        venta_ids = [m.origen_id for m in movs if m.origen == "venta" and m.origen_id]
        disc_origen = {}
        if venta_ids:
            for d in Disc.query.filter(Disc.id.in_(venta_ids)).all():
                disc_origen[d.id] = (d.origen or "arcano")

        # Plata metida en mercaderia: costo de los discos de Arcano (no personales)
        # comprados y todavia no vendidos. Misma definicion que la contabilidad
        # (Disponibles + Pre-order con pedido asignado). No es plata en caja: es
        # plata convertida en stock.
        mercaderia_discos = (
            Disc.query
            .filter(
                (Disc.estado == "Disponible")
                | ((Disc.estado == "Pre-order") & (Disc.pedido_id.isnot(None)))
            )
            .filter((Disc.origen != "personal") | (Disc.origen.is_(None)))
            .all()
        )
        mercaderia_uyu = sum((d.costo_final_uyu or 0) * (d.stock or 1) for d in mercaderia_discos)
        mercaderia_count = len(mercaderia_discos)

        # Saldo corriente sobre los movimientos de plata (los adelantos SALDADOS ya
        # se cerraron y no cuentan; se ocultan de la lista).
        saldo = saldo_inicial
        filas = []
        for m in movs_dinero:
            saldo += (m.monto_uyu or 0) * (1 if m.tipo == "ingreso" else -1)
            dorigen = disc_origen.get(m.origen_id) if m.origen == "venta" else None
            filas.append({"mov": m, "saldo": round(saldo, 2), "disc_origen": dorigen})
        # Filtro por mes: solo afecta la lista y el desglose (el saldo corriente de
        # cada fila sigue siendo el real acumulado hasta ese movimiento).
        if mes_activo:
            filas = [f for f in filas if f["mov"].fecha and f["mov"].fecha.year == fy and f["mov"].fecha.month == fm]
        movs_mostrados = [f["mov"] for f in filas]
        filas.reverse()

        mes_entro = sum(m.monto_uyu or 0 for m in movs_mostrados if m.tipo == "ingreso")
        mes_salio = sum(m.monto_uyu or 0 for m in movs_mostrados if m.tipo == "egreso")

        # Cantidad de movimientos por tipo (sobre lo que se muestra)
        grupos = {}
        for m in movs_mostrados:
            if m.origen == "venta":
                key, label, orden = "venta", "Ventas", 0
            elif m.origen == "envio":
                key, label, orden = "envio", "Envios", 0.5
            elif m.origen == "pedido":
                key, label, orden = "pedido", "Pedidos", 1
            elif m.tipo == "ingreso" and m.es_aporte:
                key, label, orden = "aporte", "Aportes de capital", 2
            elif m.origen == "gasto" and m.categoria == CAT_RETIRO_PERSONAL:
                key, label, orden = "retiro", "Retiros personales", 2.5
            elif m.origen == "gasto":
                cat = m.categoria or "Otros"
                key, label, orden = "gasto:" + cat, "Gasto: " + cat, 3
            else:
                key, label, orden = "manual", "Manuales (aportes/retiros/ajustes)", 4
            g = grupos.get(key)
            if not g:
                g = {"label": label, "count": 0, "total": 0.0, "tipo": m.tipo, "orden": orden}
                grupos[key] = g
            g["count"] += 1
            g["total"] += (m.monto_uyu or 0)
        desglose_tipos = [
            {"label": g["label"], "count": g["count"], "total": round(g["total"], 2), "tipo": g["tipo"]}
            for g in sorted(grupos.values(), key=lambda x: (x["orden"], -x["count"]))
        ]

        # --- Desgloses para "click en el numero" del header (sobre los totales AL DIA,
        # no el mes filtrado). Cada tarjeta muestra de donde sale su numero. ---
        from collections import OrderedDict as _OD

        def _desglosar(movs_list, bucketfn):
            acc = _OD()
            for m in movs_list:
                b = bucketfn(m)
                acc[b] = acc.get(b, 0) + (m.monto_uyu or 0)
            return [{"label": k, "monto": round(v)} for k, v in acc.items() if abs(v) > 0.5]

        def _buck_ing(m):
            if m.origen == "venta":
                return "Ventas de discos"
            if m.origen == "envio":
                return "Envios cobrados"
            if m.es_aporte:
                return "Aportes de capital (tu plata)"
            return "Otros ingresos que cargaste a mano"

        def _buck_egr(m):
            if m.origen == "pedido":
                return "Compras a Deejay (pedidos)"
            if m.origen == "gasto":
                if m.categoria == CAT_RETIRO_PERSONAL:
                    return "Retiros personales (tu plata)"
                return "Gastos (publicidad, hosting, etc.)"
            return "Retiros / ajustes que cargaste a mano"

        ingresos_desglose = _desglosar([m for m in movs_dinero if m.tipo == "ingreso"], _buck_ing)
        egresos_desglose = _desglosar([m for m in movs_dinero if m.tipo == "egreso"], _buck_egr)
        capital_desglose = [
            {"label": (m.concepto or "Aporte de capital"), "monto": round(m.monto_uyu or 0)}
            for m in movs_dinero if (m.tipo == "ingreso" and m.es_aporte)
        ]
        # Los retiros personales restan del capital (inverso a un aporte).
        capital_desglose += [
            {"label": "Retiro: " + (m.concepto or "personal"), "monto": -round(m.monto_uyu or 0)}
            for m in movs_dinero
            if m.tipo == "egreso" and m.origen == "gasto" and m.categoria == CAT_RETIRO_PERSONAL
        ]
        saldo_desglose = [
            {"label": "Saldo inicial", "monto": round(saldo_inicial)},
            {"label": "Entro (ingresos)", "monto": round(total_ingresos)},
            {"label": "Salio (egresos)", "monto": -round(total_egresos)},
        ]

        return {
            "saldo_inicial": round(saldo_inicial, 2),
            "total_ingresos": round(total_ingresos, 2),
            "total_egresos": round(total_egresos, 2),
            "total_aportes": round(total_aportes, 2),
            "capital_permanente": round(capital_permanente, 2),
            "ingresos_desglose": ingresos_desglose,
            "egresos_desglose": egresos_desglose,
            "capital_desglose": capital_desglose,
            "saldo_desglose": saldo_desglose,
            "total_ventas": round(total_ventas, 2),
            "saldo_actual": round(saldo_actual, 2),
            "mp_pendiente": round(mp_pendiente, 2),
            "disponible": round(disponible, 2),
            "mercaderia_uyu": round(mercaderia_uyu, 2),
            "mercaderia_count": mercaderia_count,
            "por_adelantar": round(por_adelantar, 2),
            "adelantos_pendientes": adelantos_pendientes,
            "adelanto_pendiente_total": round(adelanto_pendiente_total, 2),
            "por_metodo": {k: round(v, 2) for k, v in por_metodo.items()},
            "desglose_tipos": desglose_tipos,
            "filas": filas,
            "count": len(movs_mostrados),
            "meses_disponibles": meses_disponibles,
            "mes_filtro": ("%04d-%02d" % (fy, fm)) if mes_activo else "",
            "mes_activo": mes_activo,
            "mes_entro": round(mes_entro, 2),
            "mes_salio": round(mes_salio, 2),
        }

    @app.route("/admin/caja")
    @auth.admin_required
    def caja():
        from models import METODOS_PAGO_CAJA
        from datetime import datetime as _dt
        _sincronizar_caja()  # trae ventas/pedidos/gastos nuevos (silencioso)
        from models import MovimientoCaja as _MC
        mes_param = request.args.get("mes")
        if mes_param is None:
            # Default: el mes mas reciente con movimientos (no 'Todos', que con cientos
            # de movimientos es una lista enorme y lenta de dibujar). El usuario elige
            # 'Todos' cuando quiere ver la historia completa.
            _ult = _MC.query.order_by(_MC.fecha.desc()).first()
            mes = _ult.fecha.strftime("%Y-%m") if (_ult and _ult.fecha) else None
        elif mes_param == "todos":
            mes = None
        else:
            mes = mes_param or None
        resumen = _resumen_caja(mes)
        # Lista GESTIONADA de categorias de origen propias (agregar/eliminar)
        categorias_origen = _categorias_origen_lista()
        # Colores elegidos (clave -> hex). La clave es la categoria propia (si el
        # manual la tiene) o el origen fijo ('manual'/'venta'/'pedido'/'gasto').
        import json as _json
        try:
            colores_map = _json.loads(Setting.get("origen_cat_colores", "") or "{}")
        except Exception:
            colores_map = {}
        _def_origen = {"manual": "#a78bfa", "venta": "#34d399", "pedido": "#60a5fa", "gasto": "#f87171"}
        origen_estilos = {}
        for k in list(_def_origen.keys()) + list(categorias_origen):
            bg = colores_map.get(k)
            origen_estilos[k] = {"bg": bg, "fg": _text_on_color(bg) if bg else None}
        # Etiquetas (nombres) personalizadas de los origenes fijos
        try:
            labels_map = _json.loads(Setting.get("origen_labels", "") or "{}")
        except Exception:
            labels_map = {}
        _def_labels = {"manual": "Manual", "venta": "Venta", "pedido": "Pedido", "gasto": "Gasto"}
        # Los 4 origenes fijos (nombre editable + color; NO se borran, son automaticos)
        origen_fijos = []
        for k in ("manual", "venta", "pedido", "gasto"):
            col = colores_map.get(k) or _def_origen[k]
            origen_fijos.append({
                "clave": k, "label": labels_map.get(k) or _def_labels[k],
                "color": col, "fg": _text_on_color(col),
            })
        # Las categorias propias (color + eliminar)
        origen_propias = []
        for cat in categorias_origen:
            col = colores_map.get(cat) or "#14b8a6"
            origen_propias.append({"clave": cat, "label": cat, "color": col, "fg": _text_on_color(col)})
        return render_template(
            "caja.html",
            resumen=resumen,
            metodos=METODOS_PAGO_CAJA,
            categorias=Setting.get_categorias_egreso(),
            categorias_origen=categorias_origen,
            origen_estilos=origen_estilos,
            origen_fijos=origen_fijos,
            origen_propias=origen_propias,
            labels_map=labels_map,
            hoy=today_uy().isoformat(),
        )

    @app.route("/admin/caja/origen-fijo", methods=["POST"])
    @auth.admin_required
    def caja_origen_fijo():
        """Renombra (label) y/o recolorea un origen fijo (manual/venta/pedido/gasto).
        No se pueden eliminar: son automaticos (se regeneran desde ventas/pedidos/gastos)."""
        import json as _json
        import re as _re
        clave = (request.form.get("clave") or "").strip()
        if clave not in ("manual", "venta", "pedido", "gasto"):
            return redirect(url_for("caja"))
        # Nombre personalizado
        label = (request.form.get("label") or "").strip()[:40]
        try:
            labels = _json.loads(Setting.get("origen_labels", "") or "{}")
        except Exception:
            labels = {}
        _defs = {"manual": "Manual", "venta": "Venta", "pedido": "Pedido", "gasto": "Gasto"}
        if label and label != _defs[clave]:
            labels[clave] = label
        else:
            labels.pop(clave, None)  # vacio o el default -> volver al nombre estandar
        Setting.set("origen_labels", _json.dumps(labels))
        # Color (opcional)
        color = (request.form.get("color") or "").strip()
        if color:
            try:
                m = _json.loads(Setting.get("origen_cat_colores", "") or "{}")
            except Exception:
                m = {}
            if _re.match(r"^#[0-9a-fA-F]{6}$", color):
                m[clave] = color.lower()
            Setting.set("origen_cat_colores", _json.dumps(m))
        return redirect(url_for("caja"))

    @app.route("/admin/caja/categoria-origen/agregar", methods=["POST"])
    @auth.admin_required
    def caja_origen_categoria_agregar():
        """Crea una categoria de origen propia (opcionalmente con color)."""
        import json as _json
        import re as _re
        nombre = (request.form.get("nombre") or "").strip()[:60]
        if nombre:
            _categorias_origen_add(nombre)
            color = (request.form.get("color") or "").strip()
            if _re.match(r"^#[0-9a-fA-F]{6}$", color):
                try:
                    m = _json.loads(Setting.get("origen_cat_colores", "") or "{}")
                except Exception:
                    m = {}
                m[nombre] = color.lower()
                Setting.set("origen_cat_colores", _json.dumps(m))
            flash(f"Categoría de origen agregada: {nombre}", "success")
        return redirect(url_for("caja"))

    @app.route("/admin/caja/categoria-origen/eliminar", methods=["POST"])
    @auth.admin_required
    def caja_origen_categoria_eliminar():
        """Elimina una categoria propia: la saca de la lista, de los colores y la
        limpia de los movimientos que la usaban (como en Notion)."""
        import json as _json
        from models import MovimientoCaja as _MC
        nombre = (request.form.get("nombre") or "").strip()[:60]
        if nombre:
            lista = [c for c in _categorias_origen_lista() if c != nombre]
            _categorias_origen_guardar(lista)
            try:
                m = _json.loads(Setting.get("origen_cat_colores", "") or "{}")
                if nombre in m:
                    del m[nombre]
                    Setting.set("origen_cat_colores", _json.dumps(m))
            except Exception:
                pass
            # Limpiar los movimientos que la usaban (quedan como Manual)
            _MC.query.filter_by(origen_categoria=nombre).update({"origen_categoria": None})
            db.session.commit()
            flash(f"Categoría eliminada: {nombre}", "info")
        return redirect(url_for("caja"))

    @app.route("/admin/caja/origen-color", methods=["POST"])
    @auth.admin_required
    def caja_origen_color():
        """Guarda (o borra) el color elegido para una categoria de origen."""
        import json as _json
        import re as _re
        cat = (request.form.get("categoria") or "").strip()[:60]
        color = (request.form.get("color") or "").strip()
        if not cat:
            return redirect(url_for("caja"))
        try:
            m = _json.loads(Setting.get("origen_cat_colores", "") or "{}")
        except Exception:
            m = {}
        if _re.match(r"^#[0-9a-fA-F]{6}$", color):
            m[cat] = color.lower()
        else:
            m.pop(cat, None)
        Setting.set("origen_cat_colores", _json.dumps(m))
        return redirect(url_for("caja"))

    @app.route("/admin/caja/movimiento", methods=["POST"])
    @auth.admin_required
    def caja_movimiento_agregar():
        from models import MovimientoCaja
        from datetime import datetime as _dt
        tipo = (request.form.get("tipo") or "ingreso").strip()
        if tipo not in ("ingreso", "egreso"):
            tipo = "ingreso"
        monto = _parse_monto_caja(request.form.get("monto"))
        concepto = (request.form.get("concepto") or "").strip()
        metodo = (request.form.get("metodo") or "").strip() or None
        categoria = (request.form.get("categoria") or "").strip() or None
        fecha_str = request.form.get("fecha") or ""
        try:
            fecha = _dt.strptime(fecha_str, "%Y-%m-%d").date() if fecha_str else today_uy()
        except ValueError:
            fecha = today_uy()
        # Aporte de capital: solo aplica a ingresos
        es_aporte = bool(request.form.get("es_aporte")) and tipo == "ingreso"
        if monto <= 0:
            flash("El monto debe ser mayor a 0.", "error")
            return redirect(url_for("caja"))
        concepto_final = concepto or ("Aporte de capital" if es_aporte else ("Aporte" if tipo == "ingreso" else "Retiro"))
        origen_categoria = (request.form.get("origen_categoria") or "").strip()[:60] or None
        if origen_categoria:
            _categorias_origen_add(origen_categoria)  # crear la categoria si es nueva
        mov = MovimientoCaja(
            fecha=fecha, tipo=tipo, monto_uyu=round(monto, 2),
            concepto=concepto_final[:300],
            metodo=(metodo if tipo == "ingreso" else None),
            categoria=(categoria if tipo == "egreso" else None),
            es_aporte=es_aporte,
            origen="manual", origen_id=None,
            origen_categoria=origen_categoria,
        )
        db.session.add(mov)
        db.session.commit()
        flash(f"Movimiento agregado: {'+' if tipo == 'ingreso' else '-'} ${monto:,.0f} UYU.", "success")
        return redirect(url_for("caja"))

    @app.route("/admin/caja/movimiento/<int:mov_id>/editar", methods=["POST"])
    @auth.admin_required
    def caja_movimiento_editar(mov_id):
        from models import MovimientoCaja
        from datetime import datetime as _dt
        m = MovimientoCaja.query.get_or_404(mov_id)
        tipo = (request.form.get("tipo") or m.tipo).strip()
        if tipo in ("ingreso", "egreso"):
            m.tipo = tipo
        monto = _parse_monto_caja(request.form.get("monto"))
        if monto > 0:
            m.monto_uyu = round(monto, 2)
        concepto = (request.form.get("concepto") or "").strip()
        if concepto:
            m.concepto = concepto[:300]
        metodo = (request.form.get("metodo") or "").strip()
        m.metodo = (metodo or None) if m.tipo == "ingreso" else None
        categoria = (request.form.get("categoria") or "").strip()
        m.categoria = (categoria or None) if m.tipo == "egreso" else None
        # Aporte de capital: solo para ingresos
        m.es_aporte = bool(request.form.get("es_aporte")) and m.tipo == "ingreso"
        fecha_str = request.form.get("fecha") or ""
        if fecha_str:
            try:
                m.fecha = _dt.strptime(fecha_str, "%Y-%m-%d").date()
            except ValueError:
                pass
        # Si es un movimiento automatico, al editarlo a mano queda "tomado" por
        # Facundo: la sincronizacion ya no lo pisa. (Para volver a auto: borralo,
        # se regenera solo desde el origen en la proxima sincronizacion.)
        if not m.es_manual:
            m.manual_override = True
        db.session.commit()
        flash("Movimiento actualizado.", "success")
        return redirect(url_for("caja"))

    @app.route("/admin/caja/movimiento/<int:mov_id>/quick-update", methods=["POST"])
    @auth.admin_required
    def caja_movimiento_quick_update(mov_id):
        """Edicion inline de un solo campo desde la tabla (AJAX). Devuelve JSON
        con los totales recalculados para refrescar la vista."""
        from models import MovimientoCaja
        from datetime import datetime as _dt
        m = MovimientoCaja.query.get_or_404(mov_id)
        data = request.get_json(silent=True) or {}
        campo = (data.get("campo") or "").strip()
        valor = data.get("valor")
        if campo == "fecha":
            try:
                m.fecha = _dt.strptime((valor or "").strip(), "%Y-%m-%d").date()
            except (ValueError, TypeError, AttributeError):
                return jsonify({"ok": False, "error": "Fecha invalida"}), 400
        elif campo == "tipo":
            if valor in ("ingreso", "egreso"):
                m.tipo = valor
                if m.tipo == "ingreso":
                    m.categoria = None
                else:
                    m.metodo = None
                    m.es_aporte = False
            else:
                return jsonify({"ok": False, "error": "Tipo invalido"}), 400
        elif campo == "monto":
            monto = _parse_monto_caja(str(valor if valor is not None else ""))
            if monto <= 0:
                return jsonify({"ok": False, "error": "El monto debe ser mayor a 0"}), 400
            m.monto_uyu = round(monto, 2)
        elif campo == "concepto":
            m.concepto = (str(valor or "").strip())[:300] or None
        elif campo == "metodo":
            v = str(valor or "").strip()
            v = v if v in ("mercadopago", "transferencia", "efectivo", "otro") else None
            m.metodo = v
            # Si el movimiento es una VENTA, el metodo es del disco (fuente unica):
            # lo escribimos ahi para que el panel de Ventas muestre lo mismo.
            if m.origen == "venta" and m.origen_id:
                from models import Disc as _Disc
                _d = _Disc.query.get(m.origen_id)
                if _d:
                    _d.metodo_pago_venta = v
        elif campo == "categoria":
            m.categoria = (str(valor or "").strip()) or None
        elif campo == "origen_categoria":
            # Categoria de origen libre: solo para manuales (no tocar los automaticos
            # Venta/Pedido/Gasto, que los maneja el sync).
            if not m.es_manual:
                return jsonify({"ok": False, "error": "El origen de un movimiento automatico no se cambia (lo maneja el sistema)."}), 400
            m.origen_categoria = (str(valor or "").strip())[:60] or None
            if m.origen_categoria:
                _categorias_origen_add(m.origen_categoria)  # crear si es nueva (Notion-style)
        else:
            return jsonify({"ok": False, "error": "Campo desconocido"}), 400
        # Editar a mano un movimiento automatico lo "toma": deja de sincronizarse.
        # EXCEPCION: cambiar el metodo de una venta NO lo congela, porque el metodo
        # viaja por el disco (se sincroniza solo con el panel de Ventas).
        if not m.es_manual and not (campo == "metodo" and m.origen == "venta"):
            m.manual_override = True
        db.session.commit()
        resumen = _resumen_caja()
        return jsonify({
            "ok": True,
            "saldo_actual": resumen["saldo_actual"],
            "total_ingresos": resumen["total_ingresos"],
            "total_egresos": resumen["total_egresos"],
        })

    @app.route("/admin/caja/movimiento/<int:mov_id>/eliminar", methods=["POST"])
    @auth.admin_required
    def caja_movimiento_eliminar(mov_id):
        from models import MovimientoCaja
        m = MovimientoCaja.query.get_or_404(mov_id)
        origen = m.origen or "manual"
        db.session.delete(m)
        db.session.commit()
        if origen != "manual":
            flash(
                "Movimiento quitado. Ojo: como venia de una venta/pedido/gasto, si ese "
                "origen sigue existiendo se va a volver a cargar solo. Para sacarlo del "
                "todo, edita o borra el disco/pedido/gasto de origen.",
                "warning",
            )
        else:
            flash("Movimiento eliminado.", "success")
        return redirect(url_for("caja"))

    @app.route("/admin/caja/movimiento/<int:mov_id>/marcar-aporte", methods=["POST"])
    @auth.admin_required
    def caja_marcar_aporte(mov_id):
        """Marca que un egreso (pedido/gasto/retiro) lo pago Facundo con su plata:
        crea un aporte de capital (ingreso) por el mismo monto y fecha. Asi la caja
        refleja la inversion sin perder el gasto. Idempotente por (monto, fecha)."""
        from models import MovimientoCaja
        m = MovimientoCaja.query.get_or_404(mov_id)
        if m.tipo != "egreso":
            flash("El aporte de capital se marca sobre un gasto (algo que salió de la caja).", "error")
            return redirect(url_for("caja"))
        monto = round(m.monto_uyu or 0, 2)
        if monto <= 0:
            flash("Ese gasto no tiene monto.", "error")
            return redirect(url_for("caja"))
        # Evitar duplicar: si ya hay un aporte por el mismo monto y fecha, no crear otro
        ya = (
            MovimientoCaja.query
            .filter_by(tipo="ingreso", es_aporte=True, fecha=m.fecha, monto_uyu=monto)
            .first()
        )
        if ya:
            flash("Ese aporte de capital ya estaba registrado.", "warning")
            return redirect(url_for("caja"))
        etiqueta = (m.concepto or "gasto").strip()
        aporte = MovimientoCaja(
            fecha=m.fecha, tipo="ingreso", monto_uyu=monto,
            concepto=f"Aporte de capital (pagaste: {etiqueta})"[:300],
            metodo=None, categoria=None, es_aporte=True,
            origen="manual", origen_id=None,
        )
        db.session.add(aporte)
        db.session.commit()
        flash(f"Registrado tu aporte de capital: + ${monto:,.0f} UYU. Tu gasto queda como está.", "success")
        return redirect(url_for("caja"))

    @app.route("/admin/caja/adelantar-mp", methods=["POST"])
    @auth.admin_required
    def caja_adelantar_mp():
        """Adelanta con plata propia lo que Mercado Pago todavia no libero: crea un
        aporte de capital por el monto pendiente en MP que aun no se adelanto, y lo
        marca como 'adelanto pendiente' para recordar el retiro cuando MP pague."""
        from models import MovimientoCaja
        from datetime import datetime as _dt
        resumen = _resumen_caja()
        monto = round(resumen["por_adelantar"], 2)
        if monto <= 0:
            flash("No hay nada nuevo para adelantar de Mercado Pago.", "warning")
            return redirect(url_for("caja"))
        mov = MovimientoCaja(
            fecha=today_uy(), tipo="ingreso", monto_uyu=monto,
            concepto="Adelanto de Mercado Pago (tu plata; lo recuperás cuando MP pague)",
            metodo=None, categoria=None, es_aporte=True, adelanto_mp="pendiente",
            origen="manual", origen_id=None,
        )
        db.session.add(mov)
        db.session.commit()
        flash(f"Adelantaste ${monto:,.0f} de tu plata. Cuando MP te pague, tocá «MP ya me pagó» para registrar el retiro.", "success")
        return redirect(url_for("caja"))

    @app.route("/admin/caja/movimiento/<int:mov_id>/saldar-adelanto", methods=["POST"])
    @auth.admin_required
    def caja_saldar_adelanto(mov_id):
        """MP ya libero el dinero del adelanto. NO crea retiro ni cambia el numero de
        la caja: al saldar, el adelanto (tu plata que ya estaba contada) se cierra y la
        venta que estaba en MP pasa al banco — se compensan, el saldo NO cambia. Solo
        marca el adelanto como saldado (sale de 'Adelantos a recuperar')."""
        from models import MovimientoCaja
        m = MovimientoCaja.query.get_or_404(mov_id)
        if (m.adelanto_mp or "") != "pendiente":
            flash("Ese adelanto ya estaba saldado.", "warning")
            return redirect(url_for("caja"))
        monto = round(m.monto_uyu or 0, 2)
        m.adelanto_mp = "saldado"
        db.session.commit()
        flash(f"Listo, tu adelanto de ${monto:,.0f} quedó cerrado (MP te lo pagó). El saldo no cambia: recuperaste tu plata y la venta pasó al banco.", "success")
        return redirect(url_for("caja"))

    @app.route("/admin/caja/saldo-inicial", methods=["POST"])
    @auth.admin_required
    def caja_saldo_inicial():
        monto = _parse_monto_caja(request.form.get("saldo_inicial"))
        Setting.set("caja_saldo_inicial", str(round(monto, 2)))
        flash("Saldo inicial actualizado.", "success")
        return redirect(url_for("caja"))

    @app.route("/admin/caja/cuadrar", methods=["POST"])
    @auth.admin_required
    def caja_cuadrar():
        """Cuadra la caja a un numero: Facundo escribe el saldo REAL de su banco y el
        sistema crea UN solo ajuste manual por la diferencia, para que 'Plata en caja
        ahora' quede exacta. Evita tener que hacer la resta a mano cada vez que la caja
        se despega del banco (plata que entro y no se cargo, redondeos, etc.)."""
        from models import MovimientoCaja
        from datetime import datetime as _dt
        objetivo = round(_parse_monto_caja(request.form.get("objetivo")), 2)
        if objetivo < 0:
            flash("El saldo del banco no puede ser negativo.", "error")
            return redirect(url_for("caja"))
        actual = _resumen_caja()["disponible"]  # el numero grande de arriba
        diff = round(objetivo - actual, 2)
        if abs(diff) < 1:
            flash(f"La caja ya marca ${objetivo:,.0f}. No hace falta ningún ajuste.", "success")
            return redirect(url_for("caja"))
        tipo = "ingreso" if diff > 0 else "egreso"
        monto = abs(diff)
        mov = MovimientoCaja(
            fecha=today_uy(), tipo=tipo, monto_uyu=monto,
            concepto="Ajuste de caja (cuadre a saldo real del banco)",
            metodo=None, categoria=None, es_aporte=False,
            origen="manual", origen_id=None,
        )
        db.session.add(mov)
        db.session.commit()
        signo = "+" if tipo == "ingreso" else "−"
        flash(f"Caja cuadrada a ${objetivo:,.0f}. Se creó un ajuste de {signo}${monto:,.0f} (podés verlo/borrarlo en la lista de abajo).", "success")
        return redirect(url_for("caja"))

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
            fecha = _dt.strptime(fecha_str, "%Y-%m-%d").date() if fecha_str else today_uy()
        except ValueError:
            fecha = today_uy()
        notas = (request.form.get("notas") or "").strip()

        def _opt_float(name):
            r = (request.form.get(name) or "").strip().replace(",", ".")
            try:
                return max(0.0, float(r)) if r != "" else None
            except (ValueError, TypeError):
                return None

        ped = Pedido(
            numero=numero, fecha=fecha, cant_discos=cant,
            costo_eur=costo_eur, costo_usd=costo_usd, proveedor=proveedor, notas=notas,
            envio_eur=_opt_float("envio_eur"), servicio_eur=_opt_float("servicio_eur"),
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
        raw = (request.form.get("costo_uyu_manual") or "").strip()
        if raw == "":
            # Vacio => volver al calculo automatico
            ped.costo_uyu_manual = None
            db.session.commit()
            flash("Costo del pedido: vuelve al calculo automatico.", "success")
        elif not any(c.isdigit() for c in raw):
            # Texto sin ningun numero ("abc", pegado mal): NO guardar 0, rechazar.
            flash("Monto invalido.", "error")
        else:
            val = _parse_monto_caja(raw)  # es-UY: puntos=miles, coma=decimal
            if val < 0:
                flash("Monto invalido.", "error")
            else:
                ped.costo_uyu_manual = val
                db.session.commit()
                flash(f"Costo real del pedido guardado: {val:,.0f} UYU.".replace(",", "."), "success")
        return redirect(url_for("pedidos"))

    @app.route("/admin/pedidos/<int:pedido_id>/editar", methods=["POST"])
    @auth.admin_required
    def pedido_editar(pedido_id):
        """Edita todos los campos de un pedido (por si se cargo algo mal)."""
        from models import Pedido, PROVEEDORES_PEDIDO
        from datetime import datetime as _dt
        ped = Pedido.query.get_or_404(pedido_id)

        ped.numero = (request.form.get("numero") or "").strip() or None

        prov = (request.form.get("proveedor") or "").strip()
        if prov in PROVEEDORES_PEDIDO:
            ped.proveedor = prov

        try:
            ped.cant_discos = max(0, int(request.form.get("cant_discos") or 0))
        except (ValueError, TypeError):
            pass
        try:
            ped.costo_eur = max(0.0, float(str(request.form.get("costo_eur") or 0).replace(",", ".")))
        except (ValueError, TypeError):
            pass
        try:
            ped.costo_usd = max(0.0, float(str(request.form.get("costo_usd") or 0).replace(",", ".")))
        except (ValueError, TypeError):
            pass

        fecha_str = (request.form.get("fecha") or "").strip()
        if fecha_str:
            try:
                ped.fecha = _dt.strptime(fecha_str, "%Y-%m-%d").date()
            except ValueError:
                pass

        # Costo real UYU (manual): vacio => None (vuelve al calculo automatico).
        # Mismo parseo es-UY que el boton rapido (_parse_monto_caja): puntos=miles,
        # coma=decimal. Antes esta ruta tomaba "12.345" como 12,3 (1000x de error).
        raw = (request.form.get("costo_uyu_manual") or "").strip()
        if raw == "":
            ped.costo_uyu_manual = None
        elif any(c.isdigit() for c in raw):
            v = _parse_monto_caja(raw)
            ped.costo_uyu_manual = v if v >= 0 else None
        # texto sin numeros: se ignora (no pisa el costo con 0)

        # Costos informativos (envio y servicio de la web, en EUR): no entran en ningun
        # calculo, solo se guardan. Vacio => None.
        for _campo in ("envio_eur", "servicio_eur"):
            _raw = (request.form.get(_campo) or "").strip().replace(",", ".")
            if _raw == "":
                setattr(ped, _campo, None)
            else:
                try:
                    setattr(ped, _campo, max(0.0, float(_raw)))
                except (ValueError, TypeError):
                    pass

        ped.notas = (request.form.get("notas") or "").strip() or None

        db.session.commit()
        flash(f"Pedido {ped.numero or ped.id} actualizado.", "success")
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
            tc_usd=Setting.get_tc_usd(),
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
            meta_pixel_id=Setting.get("meta_pixel_id", ""),
            tip_label=Setting.get("tip_label", "Staff Picks"),
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
            # Fire Sale
            fire_sale_activa=Setting.get("fire_sale_activa", "0") == "1",
            fire_sale_pct=Setting.get("fire_sale_pct", "10"),
            fire_sale_alcance=Setting.get("fire_sale_alcance", "todo"),
            fire_sale_fin=Setting.get("fire_sale_fin", ""),
            fire_sale_mensaje=Setting.get("fire_sale_mensaje", ""),
            fire_sale_cfg=fire_sale_config(),
            fire_sale_discos_sel=Disc.query.filter_by(en_fire_sale=True).count(),
            clientes_con_email=Cliente.query.filter(Cliente.email.isnot(None), Cliente.email != "").count(),
            # Lealtad
            lealtad_activa=Setting.get("lealtad_activa", "0") == "1",
            lealtad_umbral1=Setting.get("lealtad_umbral1", "15000"),
            lealtad_umbral2=Setting.get("lealtad_umbral2", "30000"),
            lealtad_premio1=Setting.get("lealtad_premio1", "Un vale de regalo"),
            lealtad_premio2=Setting.get("lealtad_premio2", "Un disco sorpresa de regalo"),
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

    @app.route("/admin/mp/test", methods=["POST"])
    @auth.admin_required
    def mp_test_conexion():
        """Prueba que el token de Mercado Pago sea valido (sin cobrar nada)."""
        ok, mensaje = mp.probar_credencial()
        return jsonify({"ok": ok, "mensaje": mensaje})

    @app.route("/admin/settings/tc", methods=["POST"])
    @auth.admin_required
    def settings_update_tc():
        nuevo_tc = _parse_float(request.form.get("tc_eur_uyu"), default=0.0)
        if not (0 < nuevo_tc <= 10000):
            flash("El tipo de cambio del euro debe ser un numero entre 0 y 10000.", "error")
            return redirect(url_for("settings_page"))
        nuevo_tc_usd = _parse_float(request.form.get("tc_usd_uyu"), default=0.0)
        if not (0 < nuevo_tc_usd <= 10000):
            flash("El tipo de cambio del dolar debe ser un numero entre 0 y 10000.", "error")
            return redirect(url_for("settings_page"))

        anterior = Setting.get_tc()
        anterior_usd = Setting.get_tc_usd()
        Setting.set("tc_eur_uyu", str(nuevo_tc))
        Setting.set("tc_usd_uyu", str(nuevo_tc_usd))
        flash(
            f"Tipos de cambio actualizados. 1 EUR: {anterior} -> {nuevo_tc} UYU | "
            f"1 USD: {anterior_usd} -> {nuevo_tc_usd} UYU. "
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
        _fs = fire_sale_config()
        return {
            "fire_sale": _fs,
            "fire_sale_mensaje": Setting.get("fire_sale_mensaje", "") or "",
            "tagline": Setting.get("public_tagline", ""),
            "about": Setting.get("public_about", ""),
            "about_curaduria": Setting.get("public_about_curaduria", ""),
            "about_como_funciona": Setting.get("public_about_como_funciona", ""),
            "envios": Setting.get("public_envios", ""),
            "instagram": Setting.get("public_instagram", ""),
            "whatsapp": Setting.get("public_whatsapp", ""),
            "analytics_snippet": Setting.get("analytics_snippet", ""),
            "meta_pixel_id": Setting.get("meta_pixel_id", ""),
            "tip_label": Setting.get("tip_label", "Staff Picks"),
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

    def _precio_a_cobrar(disc):
        """Precio real a cobrar por un disco: el de Fire Sale si esta vigente para
        el, si no el precio normal (precio_mostrar)."""
        if disc.en_fire_sale_activo:
            return disc.precio_fire_sale
        return disc.precio_mostrar

    def _fire_sale_bloquea(discs):
        """True si en la lista hay algun disco en Fire Sale y el visitante NO tiene
        cuenta iniciada (los discos en Fire Sale son exclusivos para clientes)."""
        if auth.cliente_actual_id():
            return False
        return any(getattr(d, "en_fire_sale_activo", False) for d in discs)

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
        # Usar el precio REAL a cobrar (Fire Sale u oferta si aplica) para cobrar lo
        # mismo que ve el cliente en el checkout/mail, y descartar los sin precio
        # (un unit_price 0 rompe la preferencia).
        discos_cobrables = [d for d in discos if (_precio_a_cobrar(d) or 0) > 0]
        subtotal = sum((_precio_a_cobrar(d) or 0) for d in discos_cobrables)
        if subtotal <= 0:
            return None
        recargo = mp.calcular_recargo(subtotal)
        items = [
            {"title": f"{d.artista} - {d.titulo}", "quantity": 1,
             "unit_price": float(_precio_a_cobrar(d) or 0)}
            for d in discos_cobrables
        ]
        if recargo > 0:
            items.append({
                "title": f"Recargo pago online ({mp.recargo_pct():.0f}%)",
                "quantity": 1, "unit_price": float(recargo),
            })
        envio = round(reserva.envio_costo_uyu or 0, 2)
        if envio > 0:
            items.append({
                "title": "Envio en Montevideo",
                "quantity": 1, "unit_price": float(envio),
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
        reserva.total_cobrado_uyu = subtotal + recargo + envio
        reserva.mp_status = "pending"
        db.session.commit()
        return mp.url_checkout(pref)

    def _mp_completar_reserva(reserva, payment_id=None, status="approved", amount=None):
        """Marca la reserva como pagada/completada y sus discos como Vendido.
        A prueba de carreras: un UPDATE condicional atomico 'reclama' la reserva.
        Si otra llamada (webhook + retorno, o dos webhooks) ya la reclamo, corta
        aca para no vender dos veces ni mandar dos mails."""
        # Defensa en profundidad: el monto que pago MP debe cubrir el total de la
        # reserva. En el flujo normal el monto lo fija la preferencia del server, asi
        # que esto solo actua si llega un pago por menos (bug/preferencia reusada).
        try:
            _total_esperado = float(reserva.total_cobrado_uyu or 0)
        except (TypeError, ValueError):
            _total_esperado = 0
        if amount is not None and _total_esperado > 0 and float(amount) < _total_esperado * 0.99:
            reserva.mp_status = status
            reserva.mp_payment_id = str(payment_id) if payment_id else reserva.mp_payment_id
            _nota = "[ATENCION: pago MP de $%.0f por debajo del total $%.0f. Revisar antes de entregar.]" % (float(amount), _total_esperado)
            reserva.nota = ((reserva.nota + " ") if reserva.nota else "") + _nota
            db.session.commit()
            return
        reclamada = (
            db.session.query(Reserva)
            .filter(Reserva.id == reserva.id, Reserva.pagado == False)  # noqa: E712
            .update(
                {
                    "pagado": True,
                    "estado": "Pagada",
                    "mp_status": status,
                    "mp_payment_id": str(payment_id) if payment_id else reserva.mp_payment_id,
                },
                synchronize_session=False,
            )
        )
        db.session.commit()
        if not reclamada:
            return  # otra llamada ya completo esta reserva
        ahora = now_uy()
        # Cliente: asegurar su ficha en la base y linkear la reserva (venta web pagada)
        from models import Cliente as _Cliente
        cli_res = _Cliente.query.get(reserva.cliente_id) if reserva.cliente_id else None
        if cli_res is None:
            cli_res = _buscar_o_crear_cliente(reserva.nombre, reserva.whatsapp, reserva.email,
                                              origen="web")
            if cli_res and not reserva.cliente_id:
                reserva.cliente_id = cli_res.id
        conflicto = []  # discos que YA estaban Vendidos por otra via (posible doble cobro)
        for item in reserva.items_efectivos:
            d = Disc.query.get(item.disc_id) if getattr(item, "disc_id", None) else None
            if not d:
                continue
            if d.estado == "Vendido" and (d.venta_grupo or "") != f"r{reserva.id}":
                # Ya vendido por otra via (otro cliente por MP, o venta manual/IG)
                conflicto.append(d)
                continue
            if d.estado != "Vendido":
                d.estado = "Vendido"
                if not d.fecha_venta:
                    d.fecha_venta = ahora
                if not getattr(d, "copias_vendidas", 0):
                    d.copias_vendidas = 1
                d.stock = max(0, (d.stock or 1) - 1)
                # Guardar el precio realmente cobrado (el snapshot = Fire Sale/oferta
                # si aplico) para que caja y contabilidad cuenten lo que entro de verdad.
                if getattr(item, "precio_uyu_snap", None):
                    d.precio_venta_uyu = item.precio_uyu_snap
                # Venta web pagada por MP: registrar canal y metodo (editables luego)
                if not d.canal_venta:
                    d.canal_venta = "web"
                if not d.metodo_pago_venta:
                    d.metodo_pago_venta = "mercadopago"
                # Agrupar como UNA venta (todos los discos de esta reserva)
                if not d.venta_grupo:
                    d.venta_grupo = f"r{reserva.id}"
                if cli_res and not d.cliente_id:
                    d.cliente_id = cli_res.id
        if conflicto:
            # Cobraste por MP un disco que YA se habia vendido por otra via. Dejar
            # constancia en la reserva para que la revises/reembolses.
            _nombres = ", ".join((("%s - %s" % (d.artista or "", d.titulo or "")).strip(" -")) for d in conflicto)
            _nota = "[ATENCION: pago por MP de disco(s) ya vendido(s) por otra via: %s. Revisar/reembolsar.]" % _nombres
            reserva.nota = ((reserva.nota + " ") if reserva.nota else "") + _nota
        db.session.commit()
        # Si hubo conflicto (disco ya vendido): avisar al admin y NO mandar "gracias por
        # tu compra" al cliente (primero hay que resolver el reembolso).
        if conflicto:
            if mailer.is_configured():
                try:
                    mailer.send_reserva_notification(reserva, conflicto[0])
                except Exception:
                    pass
            return
        # Email de confirmacion (best-effort): pago por Mercado Pago -> "gracias por tu compra"
        if reserva.email and mailer.can_send():
            try:
                discos = [Disc.query.get(i.disc_id) for i in reserva.items_efectivos]
                mailer.send_compra_confirmacion_cliente(
                    reserva, [d for d in discos if d], metodo="mercadopago")
            except Exception:
                pass
        # Aviso al admin (Facundo): entro un pago por Mercado Pago. Con MP el checkout
        # redirige antes de llegar al aviso, asi que lo mandamos recien al confirmarse.
        if mailer.is_configured() and reserva.items_efectivos:
            try:
                _disc0 = Disc.query.get(reserva.items_efectivos[0].disc_id)
                if _disc0:
                    mailer.send_reserva_notification(reserva, _disc0)
            except Exception:
                pass

    @app.route("/mp/webhook", methods=["POST", "GET"])
    def mp_webhook():
        """Mercado Pago avisa aca cuando cambia el estado de un pago. Confirmamos
        contra la API de MP y, si esta aprobado, completamos la reserva."""
        # Rate-limit anti-flood: devolvemos 200 para no gatillar reintentos de MP,
        # pero salteamos el procesamiento (y la llamada saliente a la API).
        if _webhook_supera_limite(_client_ip()):
            return "", 200
        payment_id = (request.args.get("data.id") or request.args.get("id") or "")
        if not payment_id:
            data = request.get_json(silent=True) or {}
            payment_id = str((data.get("data") or {}).get("id") or data.get("id") or "")
        if not payment_id:
            return "", 200  # nada que procesar
        # Validar la firma del webhook (si hay MP_WEBHOOK_SECRET configurado).
        # None = no hay secreto -> se sigue igual (no rompe si aun no se cargo).
        firma = mp.firma_webhook_valida(
            request.headers.get("x-signature"),
            request.headers.get("x-request-id"),
            request.args.get("data.id") or payment_id,
        )
        if firma is False:
            return "firma invalida", 403
        pago = mp.obtener_pago(payment_id)
        if not pago:
            return "", 200
        estado = pago.get("status")  # approved / pending / rejected
        ref = pago.get("external_reference")
        reserva = Reserva.query.get(int(ref)) if ref and str(ref).isdigit() else None
        if reserva:
            if estado == "approved":
                _mp_completar_reserva(reserva, payment_id=payment_id, status=estado,
                                      amount=pago.get("transaction_amount"))
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
                _mp_completar_reserva(reserva, payment_id=payment_id, status="approved",
                                      amount=pago.get("transaction_amount"))
        if not estado_mp and reserva:
            estado_mp = reserva.mp_status
        # Medir la compra en el pixel/GA SOLO si el pago esta confirmado del lado
        # del servidor (reserva.pagado, que exige verificacion contra la API de MP)
        # y UNA sola vez por reserva (bandera en sesion). Asi no se mide un
        # ?status=approved falso pasado por la URL, ni se duplica al refrescar.
        medir_compra = False
        if reserva and reserva.pagado:
            medidas = session.get("compras_medidas") or []
            if reserva.id not in medidas:
                medir_compra = True
                session["compras_medidas"] = (medidas + [reserva.id])[-20:]
        return render_template(
            "public/mp_retorno.html",
            reserva=reserva,
            estado_mp=estado_mp,
            medir_compra=medir_compra,
            purchase_items=_ga_purchase_items(reserva) if reserva else [],
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

            # Intercalar los staff picks (es_tip) para que no queden todos juntos
            # al principio y parezca que todos los discos lo son.
            destacados = _intercalar_tips(destacados)
            nuevos_ingresos = _intercalar_tips(nuevos_ingresos)

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
        # Dedup anti-inflado: la misma IP no cuenta el mismo track dos veces
        # dentro de la ventana. Devolvemos ok igual (el player no necesita saberlo).
        if _play_ya_contado(_client_ip(), track_id):
            return jsonify({"ok": True, "plays": track.play_count or 0, "dedup": True})
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
            # Anti-spam: mismo limite por IP que el checkout
            ip_reserva = _client_ip()
            if _reserva_supera_limite(ip_reserva):
                flash("Recibimos muchas reservas desde tu conexion en poco tiempo. Espera un rato o escribinos por Instagram/WhatsApp.", "error")
                return render_template("public/reservar.html", disc=disc, form={}, **_public_context()), 429

            nombre_pila = (request.form.get("nombre") or "").strip()[:60]
            apellido = (request.form.get("apellido") or "").strip()[:60]
            nombre = (nombre_pila + " " + apellido).strip()[:120]
            whatsapp = (request.form.get("whatsapp") or "").strip()[:40]
            email = ((request.form.get("email") or "").strip()[:200]) or None
            nota = ((request.form.get("nota") or "").strip()[:1000]) or None

            errores = []
            if len(nombre_pila) < 2:
                errores.append("Ingresa tu nombre.")
            if len(apellido) < 2:
                errores.append("Ingresa tu apellido.")
            if sum(c.isdigit() for c in whatsapp) < 7:
                errores.append("Ingresa un celular valido (ej: 099 123 456).")
            if not email:
                errores.append("Ingresa tu email para enviarte la confirmacion.")
            elif not _email_valido(email):
                errores.append("El email no parece valido.")

            # Fire Sale: exclusivo para clientes con cuenta
            if _fire_sale_bloquea([disc]):
                errores.append("Este disco esta en Fire Sale: es exclusivo para clientes con cuenta. Inicia sesion o crea tu cuenta para reservarlo.")

            if errores:
                for e in errores:
                    flash(e, "error")
                return render_template(
                    "public/reservar.html",
                    disc=disc,
                    form={"nombre": nombre_pila, "apellido": apellido, "whatsapp": whatsapp,
                          "email": email, "nota": nota},
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
                precio_uyu_snap=_precio_a_cobrar(disc),
            )
            db.session.add(item)
            db.session.commit()
            _reserva_registrar(ip_reserva)

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

            # PRG (Post/Redirect/Get): en vez de renderizar aca, guardamos el id en
            # la sesion y redirigimos. Asi, si el cliente refresca la pagina de
            # confirmacion, NO se reenvia el POST (evita reservas duplicadas).
            session["ultima_reserva_ok"] = reserva.id
            session["ultima_reserva_multi"] = False
            return redirect(url_for("reserva_confirmada"))

        # Si el cliente esta logueado, precargar sus datos para no pedirlos de nuevo
        cliente = None
        if auth.cliente_actual_id():
            cliente = Cliente.query.get(auth.cliente_actual_id())

        _cn = (cliente.nombre if cliente else "") or ""
        _cnp = _cn.split(" ", 1)
        return render_template(
            "public/reservar.html",
            disc=disc,
            form={
                "nombre": _cnp[0] if _cnp else "",
                "apellido": _cnp[1] if len(_cnp) > 1 else "",
                "email": cliente.email if cliente else "",
                "whatsapp": cliente.whatsapp if cliente else "",
            },
            cliente=cliente,
            **_public_context(),
        )

    @app.route("/reserva-confirmada")
    def reserva_confirmada():
        """Pagina de confirmacion (destino del PRG de reservar/checkout).
        Lee la reserva desde la sesion (no por la URL, asi no se puede espiar la
        reserva de otro cambiando un id). Se consume: al refrescar vuelve al home."""
        rid = session.pop("ultima_reserva_ok", None)
        multi = session.pop("ultima_reserva_multi", False)
        if not rid:
            return redirect(url_for("home"))
        reserva = Reserva.query.get(rid)
        if not reserva:
            return redirect(url_for("home"))
        disc = None
        if not multi:
            items = reserva.items_efectivos
            if items:
                disc = Disc.query.get(items[0].disc_id)
            # Si el disco ya no existe, usamos el layout multi (que se apoya en los
            # snapshots guardados en el item y no necesita el objeto Disc).
            if disc is None:
                multi = True
        # Medir 'purchase' SOLO en compras reales (transferencia con comprobante).
        # "Reservar y coordinar" es un lead, no una venta: no debe inflar los ingresos.
        # (El pago por Mercado Pago se mide aparte en /mp/retorno cuando se confirma.)
        medir_compra = (reserva.metodo_pago == "transferencia")
        return render_template(
            "public/reserva_ok.html",
            reserva=reserva,
            disc=disc,
            multi=multi,
            medir_compra=medir_compra,
            purchase_items=_ga_purchase_items(reserva),
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
            if not _email_valido(email):
                errores.append("Ingresa un email valido.")
            if len(password) < 6:
                errores.append("La contrasena debe tener al menos 6 caracteres.")
            if password != password2:
                errores.append("Las contrasenas no coinciden.")
            _existente = Cliente.query.filter_by(email=email).first() if email else None
            # Solo bloquea si ya hay una cuenta REAL (con contrasena). Si la ficha existe
            # pero sin contrasena (se creo sola por una compra sin cuenta), se adopta abajo.
            if _existente and _existente.password_hash:
                errores.append("Ya existe una cuenta con ese email. Inicia sesion.")

            if errores:
                for e in errores:
                    flash(e, "error")
                return render_template(
                    "public/cliente_registro.html",
                    form={"nombre": nombre, "email": email, "whatsapp": whatsapp or ""},
                    **_public_context(),
                )

            if _existente and not _existente.password_hash:
                # Ficha creada sola por una compra sin cuenta: la ADOPTA (le pone la
                # contrasena) en vez de rechazar. Antes quedaba trabada: no podia
                # registrarse ("ya existe") ni iniciar sesion (no tenia contrasena).
                cliente = _existente
                if not cliente.nombre:
                    cliente.nombre = nombre
                if whatsapp and not cliente.whatsapp:
                    cliente.whatsapp = whatsapp
                cliente.set_password(password)
            else:
                cliente = Cliente(nombre=nombre, email=email, whatsapp=whatsapp)
                cliente.set_password(password)
                db.session.add(cliente)
            db.session.flush()

            # Adoptar reservas anonimas previas hechas con el MISMO EMAIL de esta cuenta.
            # Antes se adoptaban por WhatsApp: como el numero se puede conocer sin ser el
            # dueno (una historia, un contacto), permitia registrarse con el celu ajeno y
            # ver las reservas de esa persona. El email es la identidad de la cuenta.
            previas = Reserva.query.filter(
                Reserva.cliente_id.is_(None),
                Reserva.email == email,
            ).all()
            for r in previas:
                r.cliente_id = cliente.id

            db.session.commit()
            auth.cliente_login(cliente.id)
            flash(f"Cuenta creada. Bienvenido, {cliente.nombre}!", "success")
            return redirect(url_for("cliente_cuenta"))

        return render_template("public/cliente_registro.html",
                               form={"email": (request.args.get("email") or "").strip()[:200]},
                               **_public_context())

    @app.route("/cuenta/login", methods=["GET", "POST"], endpoint="cliente_login_page")
    def cliente_login_page():
        if auth.cliente_actual_id():
            return redirect(url_for("cliente_cuenta"))

        if request.method == "POST":
            ip = _client_ip()
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
        # Programa de lealtad: barra de progreso hacia los premios
        _total = sum(e["precio"] for e in _compras_de_cliente(cliente))
        lealtad = _lealtad_estado(cliente, _total)
        return render_template(
            "public/cliente_cuenta.html",
            cliente=cliente,
            reservas=reservas,
            favoritos=favoritos,
            lealtad=lealtad,
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
            try:
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
                    "precio_uyu": _precio_a_cobrar(d) or 0,
                    "en_fire_sale": bool(d.en_fire_sale_activo),
                    "fire_sale_pct": d.fire_sale_pct,
                    "cover": cover,
                    "estado": d.estado,
                    "disponible": not d.esta_agotado,
                    "url": url_for("public_disc", disc_id=d.id),
                    "tags": d.tags_list,
                })
            except Exception:
                # Un disco con datos raros no debe romper todo el carrito
                continue
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

            # Anti-spam: frenar avalanchas de reservas desde una misma IP
            ip_reserva = _client_ip()
            if _reserva_supera_limite(ip_reserva):
                flash("Recibimos muchas reservas desde tu conexion en poco tiempo. Espera un rato o escribinos por Instagram/WhatsApp.", "error")
                return render_template("public/checkout.html", ids=ids_raw, form={}, **_public_context()), 429

            nombre_pila = (request.form.get("nombre") or "").strip()[:60]
            apellido = (request.form.get("apellido") or "").strip()[:60]
            nombre = (nombre_pila + " " + apellido).strip()[:120]
            whatsapp = (request.form.get("whatsapp") or "").strip()[:40]
            email = ((request.form.get("email") or "").strip()[:200]) or None
            nota = ((request.form.get("nota") or "").strip()[:1000]) or None
            password_cuenta = request.form.get("password") or ""

            errores = []
            if not ids:
                errores.append("El carrito esta vacio.")
            if len(nombre_pila) < 2:
                errores.append("Ingresa tu nombre.")
            if len(apellido) < 2:
                errores.append("Ingresa tu apellido.")
            if sum(c.isdigit() for c in whatsapp) < 7:
                errores.append("Ingresa un celular valido (ej: 099 123 456).")
            if not email:
                errores.append("Ingresa tu email para enviarte la confirmacion.")
            elif not _email_valido(email):
                errores.append("El email no parece valido.")

            # Cuenta opcional: si dejo una contrasena (y no esta logueado), validamos el largo
            crear_cuenta = bool(password_cuenta) and not auth.cliente_actual_id()
            if crear_cuenta and len(password_cuenta) < 6:
                errores.append("La contrasena para crear tu cuenta debe tener al menos 6 caracteres, o dejala vacia.")

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

            # Fire Sale: los discos con descuento son exclusivos para clientes con
            # cuenta. Si el visitante no inicio sesion, no puede comprarlos.
            if _fire_sale_bloquea(disponibles):
                errores.append("Hay discos en Fire Sale en tu carrito: son exclusivos para clientes con cuenta. Inicia sesion o crea tu cuenta para comprarlos.")

            # --- Entrega / envio: SOLO aplica en COMPRA (transferencia o Mercado Pago).
            # Si el cliente "reserva y coordina", el envio no se toca (se arregla por WhatsApp).
            _metodo_form = request.form.get("metodo", "coordinar")
            es_compra = (
                (_metodo_form == "transferencia" and Setting.get("transferencia_activa", "0") == "1")
                or (_metodo_form == "mercadopago" and mp.activo())
            )
            entrega_tipo = (request.form.get("entrega") or "").strip().lower()
            envio_direccion = (request.form.get("envio_direccion") or "").strip()[:400] or None
            envio_agencia = (request.form.get("envio_agencia") or "").strip()[:200] or None
            if es_compra:
                if entrega_tipo not in ("retiro", "montevideo", "interior"):
                    entrega_tipo = "retiro"  # por defecto: retiro, sin costo ni direccion
                if entrega_tipo in ("montevideo", "interior") and not envio_direccion:
                    errores.append("Para el envio necesitamos la direccion de entrega.")
                if entrega_tipo == "interior" and not envio_agencia:
                    errores.append("Para el envio al interior indicanos la agencia (DAC, Agencia Central, etc.).")
            else:
                entrega_tipo = None
                envio_direccion = None
                envio_agencia = None
            envio_costo = _calcular_envio_uyu(entrega_tipo, len(disponibles)) if es_compra else 0

            if errores:
                for e in errores:
                    flash(e, "error")
                return render_template(
                    "public/checkout.html",
                    ids=ids_raw,
                    form={"nombre": nombre_pila, "apellido": apellido, "whatsapp": whatsapp,
                          "email": email, "nota": nota,
                          "entrega": entrega_tipo, "envio_direccion": envio_direccion,
                          "envio_agencia": envio_agencia},
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

            # Entrega / envio (solo compras; en "coordinar" queda todo en null / 0)
            reserva.entrega_tipo = entrega_tipo
            reserva.envio_costo_uyu = envio_costo
            reserva.envio_direccion = envio_direccion
            reserva.envio_agencia = envio_agencia

            for d in disponibles:
                item = ReservaItem(
                    reserva_id=reserva.id,
                    disc_id=d.id,
                    artista_snap=d.artista,
                    titulo_snap=d.titulo,
                    precio_uyu_snap=_precio_a_cobrar(d),
                )
                db.session.add(item)

            db.session.commit()
            _reserva_registrar(ip_reserva)

            # Auto-cuenta: si dejo una contrasena y el email esta libre, le creamos la cuenta
            # (asi puede seguir sus pedidos sin registrarse aparte). Best-effort: nunca rompe la reserva.
            if crear_cuenta and len(password_cuenta) >= 6 and email:
                email_norm = email.strip().lower()
                existente = Cliente.query.filter_by(email=email_norm).first()
                if existente and existente.password_hash:
                    flash("Ya tenias una cuenta con ese email. Inicia sesion para ver tus pedidos.", "info")
                else:
                    try:
                        if existente and not existente.password_hash:
                            # Adoptar la ficha creada sola por una compra previa sin cuenta
                            # (antes quedaba trabada: no podia registrarse ni loguear).
                            nuevo_cliente = existente
                            if not nuevo_cliente.nombre:
                                nuevo_cliente.nombre = nombre
                            if whatsapp and not nuevo_cliente.whatsapp:
                                nuevo_cliente.whatsapp = whatsapp
                            nuevo_cliente.set_password(password_cuenta)
                        else:
                            nuevo_cliente = Cliente(nombre=nombre, email=email_norm, whatsapp=whatsapp or None)
                            nuevo_cliente.set_password(password_cuenta)
                            db.session.add(nuevo_cliente)
                        db.session.flush()
                        # Adoptar esta reserva + otras anonimas con el MISMO EMAIL de la
                        # cuenta (no por WhatsApp: el numero se puede conocer sin ser el
                        # dueno y permitia ver reservas ajenas). Comparacion sin distinguir
                        # mayusculas para no perder las que se guardaron con otra caja.
                        reserva.cliente_id = nuevo_cliente.id
                        previas = Reserva.query.filter(
                            Reserva.cliente_id.is_(None),
                            db.func.lower(Reserva.email) == (email_norm or "").lower(),
                        ).all()
                        for r in previas:
                            r.cliente_id = nuevo_cliente.id
                        db.session.commit()
                        auth.cliente_login(nuevo_cliente.id)
                        flash(f"Te creamos una cuenta, {nuevo_cliente.nombre}. Vas a poder seguir tus pedidos.", "success")
                    except Exception:
                        db.session.rollback()

            # Metodo de pago elegido en el checkout
            metodo = request.form.get("metodo", "coordinar")
            if metodo == "transferencia" and Setting.get("transferencia_activa", "0") == "1":
                reserva.metodo_pago = "transferencia"
                _guardar_comprobante(reserva, request.files.get("comprobante"))
                # El cliente subio comprobante: apartamos los discos (Reservado)
                # automaticamente para que nadie mas los compre. NO se marcan
                # Vendido ni entran a la caja hasta que Facundo confirme que la
                # plata llego (marca la reserva Pagada). El comprobante es una
                # foto del cliente, no una confirmacion real del banco.
                for d in disponibles:
                    if d.estado == "Disponible":
                        register_status_change(
                            d, "Reservado",
                            nota=f"Reserva #{reserva.id} - transferencia (comprobante subido)")
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

            # Confirmacion al cliente (si dejo email). Transferencia = ya pago ->
            # "gracias por tu compra". Coordinar = reserva a coordinar el pago.
            if reserva.email and mailer.can_send():
                try:
                    if reserva.metodo_pago == "transferencia":
                        mailer.send_compra_confirmacion_cliente(reserva, disponibles, metodo="transferencia")
                    else:
                        mailer.send_reserva_confirmacion_cliente_multi(reserva, disponibles)
                except Exception:
                    pass

            # PRG: guardamos el id en sesion y redirigimos a la pagina de
            # confirmacion. Refrescar no reenvia el POST (no duplica la reserva).
            session["ultima_reserva_ok"] = reserva.id
            session["ultima_reserva_multi"] = True
            return redirect(url_for("reserva_confirmada"))

        # GET
        ids_raw = (request.args.get("ids") or "").strip()
        return render_template(
            "public/checkout.html",
            ids=ids_raw,
            form={},
            **_public_context(),
        )

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
        f_q = (request.args.get("q") or "").strip()
        query = Reserva.query.order_by(Reserva.fecha.desc())
        if filtro_estado:
            query = query.filter(Reserva.estado == filtro_estado)
        reservas = query.all()

        # Filtro por cliente (nombre / whatsapp / email / instagram de la reserva)
        if f_q:
            ql = f_q.lower()
            reservas = [r for r in reservas if ql in " ".join(filter(None, [
                (r.nombre or "").lower(), (r.whatsapp or "").lower(),
                (r.email or "").lower(),
                ((r.cliente.instagram or "") if r.cliente else "").lower(),
            ]))]

        # Armar "unidades": las reservas con el mismo grupo se muestran juntas como un
        # solo pedido; las sueltas quedan cada una por su lado. Se conserva el orden por
        # fecha (la lista ya viene desc); el grupo toma la fecha de su reserva mas nueva.
        from collections import OrderedDict as _OD
        vistos = _OD()
        for r in reservas:
            key = r.grupo or f"s{r.id}"
            vistos.setdefault(key, []).append(r)
        unidades = []
        for key, rs in vistos.items():
            es_grupo = bool(rs[0].grupo) and len(rs) > 1
            unidades.append({
                "es_grupo": es_grupo,
                "grupo": rs[0].grupo if es_grupo else None,
                "reservas": rs,
                "total": sum((r.total_uyu or 0) + (r.envio_costo_uyu or 0) for r in rs),
                "fecha": max((r.fecha for r in rs), default=None),
                "cliente": rs[0].nombre,
                "n_discos": sum(len(r.items_efectivos) for r in rs),
                "entregado": all(r.entregado for r in rs),
            })
        unidades.sort(key=lambda u: (u["fecha"] or datetime.min), reverse=True)

        # Contadores por estado (siempre sobre el total, no sobre el filtro)
        contadores = {}
        for e in ESTADOS_RESERVA:
            contadores[e] = Reserva.query.filter_by(estado=e).count()

        return render_template(
            "admin_reservas.html",
            unidades=unidades,
            estados=ESTADOS_RESERVA,
            contadores=contadores,
            filtro_estado=filtro_estado,
            f_q=f_q,
            tc=Setting.get_tc(),
        )

    def _nuevo_reserva_grupo():
        """Id incremental para un pedido agrupado de reservas ('rg1', 'rg2', ...)."""
        try:
            n = int(Setting.get("reserva_grupo_seq", "0") or 0)
        except (ValueError, TypeError):
            n = 0
        n += 1
        Setting.set("reserva_grupo_seq", str(n))
        return f"rg{n}"

    @app.route("/admin/reservas/agrupar", methods=["POST"])
    @auth.admin_required
    def reservas_agrupar():
        """Junta 2+ reservas en un solo pedido (comparten 'grupo'). Tipico: unificar
        varias reservas del mismo cliente en un unico envio. Si alguna ya estaba en un
        grupo, todas se suman a ese grupo."""
        ids = [int(x) for x in (request.form.get("ids") or "").split(",") if x.strip().isdigit()]
        reservas = Reserva.query.filter(Reserva.id.in_(ids)).all() if ids else []
        if len(reservas) < 2:
            flash("Elegí al menos 2 reservas para unirlas en un pedido.", "warning")
            return redirect(url_for("admin_reservas"))
        grupo = next((r.grupo for r in reservas if r.grupo), None) or _nuevo_reserva_grupo()
        for r in reservas:
            r.grupo = grupo
        db.session.commit()
        flash(f"Listo: {len(reservas)} reservas quedaron unidas en un pedido.", "success")
        return redirect(url_for("admin_reservas"))

    @app.route("/admin/reservas/desagrupar", methods=["POST"])
    @auth.admin_required
    def reservas_desagrupar():
        """Deshace la agrupacion: cada reserva del grupo vuelve a contar por separado."""
        grupo = (request.form.get("grupo") or "").strip()
        if grupo:
            Reserva.query.filter_by(grupo=grupo).update({"grupo": None})
            db.session.commit()
            flash("Pedido separado: cada reserva vuelve a ir por su cuenta.", "success")
        return redirect(url_for("admin_reservas"))

    _CANALES_RESERVA = [("web", "Web"), ("instagram", "Instagram"),
                        ("presencial", "Presencial"), ("otro", "Otro")]
    _METODOS_RESERVA = [("", "A coordinar"), ("transferencia", "Transferencia"),
                        ("mercadopago", "Mercado Pago"), ("efectivo", "Efectivo")]

    @app.route("/admin/reservas/nueva", methods=["GET", "POST"])
    @auth.admin_required
    def admin_reserva_nueva():
        """Alta MANUAL de una reserva (ej: un pedido que te llego por Instagram). Se
        llenan los mismos campos que la reserva web + el canal, y segun el estado elegido
        el/los discos quedan Reservado (Pendiente) o Vendido (Pagada)."""
        if request.method == "POST":
            nombre = (request.form.get("nombre") or "").strip()[:200]
            whatsapp = (request.form.get("whatsapp") or "").strip()[:50]
            email = (request.form.get("email") or "").strip()[:200] or None
            instagram = _norm_ig(request.form.get("instagram"))
            nota = (request.form.get("nota") or "").strip() or None
            canal = (request.form.get("canal") or "instagram").strip()
            if canal not in ("web", "instagram", "presencial", "otro"):
                canal = "instagram"
            estado = (request.form.get("estado") or "Pendiente").strip()
            if estado not in ESTADOS_RESERVA:
                estado = "Pendiente"
            metodo = (request.form.get("metodo_pago") or "").strip() or None
            entrega_tipo = (request.form.get("entrega_tipo") or "").strip() or None
            if entrega_tipo not in ("retiro", "montevideo", "interior"):
                entrega_tipo = None
            envio_direccion = (request.form.get("envio_direccion") or "").strip() or None
            envio_agencia = (request.form.get("envio_agencia") or "").strip() or None
            disc_ids = [int(x) for x in request.form.getlist("disc_ids") if str(x).isdigit()]

            if not nombre:
                flash("Poné al menos el nombre del cliente.", "error")
                return redirect(url_for("admin_reserva_nueva"))
            discs = Disc.query.filter(Disc.id.in_(disc_ids)).all() if disc_ids else []
            if not discs:
                flash("Elegí al menos un disco para la reserva.", "error")
                return redirect(url_for("admin_reserva_nueva"))

            envio_costo = _calcular_envio_uyu(entrega_tipo, len(discs)) if entrega_tipo else 0.0
            total_discos = sum((d.precio_mostrar or d.precio_uyu or 0) for d in discs)

            reserva = Reserva(
                nombre=nombre, whatsapp=whatsapp, email=email, nota=nota,
                canal=canal, estado=estado, metodo_pago=metodo,
                entrega_tipo=entrega_tipo, envio_costo_uyu=envio_costo,
                envio_direccion=envio_direccion, envio_agencia=envio_agencia,
                fecha=now_uy(), pagado=(estado == "Pagada"),
                total_cobrado_uyu=round(total_discos + envio_costo, 2),
            )
            db.session.add(reserva)
            db.session.flush()
            for d in discs:
                db.session.add(ReservaItem(
                    reserva_id=reserva.id, disc_id=d.id,
                    artista_snap=d.artista, titulo_snap=d.titulo,
                    precio_uyu_snap=(d.precio_mostrar or d.precio_uyu or 0),
                ))
            # Cliente: si elegiste uno ya cargado (selector), usá ese directo; si no,
            # buscá/creá por los datos escritos.
            cli = None
            _cid = request.form.get("cliente_id")
            if _cid and str(_cid).isdigit():
                from models import Cliente as _Cli
                cli = _Cli.query.get(int(_cid))
            if cli is None:
                cli = _buscar_o_crear_cliente(nombre, whatsapp, email, instagram,
                                              origen=("web" if canal == "web" else "manual"))
            if cli:
                reserva.cliente_id = cli.id
            db.session.commit()

            # Aplicar el estado elegido a los discos (misma logica que el detalle):
            # Pendiente -> Reservado (apartado) | Pagada -> Vendido | Cancelada -> Disponible.
            estado_disco = {"Pendiente": "Reservado", "Pagada": "Vendido",
                            "Cancelada": "Disponible"}.get(estado)
            if estado_disco:
                grupo = f"r{reserva.id}"
                for d in discs:
                    # Ojo: NO bajar de Vendido un disco que ya vendiste a mano (no lo
                    # "des-vendemos" ni le pisamos la fecha_venta original).
                    if d.estado != estado_disco and d.estado != "Vendido":
                        register_status_change(d, estado_disco,
                                               nota=f"Reserva manual #{reserva.id} -> {estado}")
                    # Si la reserva es una venta (Pagada), registrar canal/metodo/grupo
                    # aunque el disco ya estuviera Vendido (lo marcaste antes de la reserva).
                    if estado == "Pagada":
                        if not d.canal_venta:
                            d.canal_venta = canal
                        if metodo and not d.metodo_pago_venta:
                            d.metodo_pago_venta = metodo
                        if len(discs) > 1 and not d.venta_grupo:
                            d.venta_grupo = grupo
                        if cli and not d.cliente_id:
                            d.cliente_id = cli.id
                db.session.commit()

            flash(f"Reserva manual #{reserva.id} creada.", "success")
            return redirect(url_for("admin_reserva_detail", reserva_id=reserva.id))

        # GET: formulario con el picker de discos. Incluye los VENDIDOS a proposito:
        # el flujo de Facundo es marcar el disco Vendido a mano al vender por IG y despues
        # crear la reserva como registro. Solo se excluyen los descartados.
        discs = (
            Disc.query
            .filter(Disc.estado != "Descartado")
            .order_by(Disc.artista.asc(), Disc.titulo.asc())
            .all()
        )
        from models import Cliente
        clientes = Cliente.query.order_by(Cliente.nombre.asc()).all()
        return render_template(
            "admin_reserva_nueva.html",
            discos=discs, estados=ESTADOS_RESERVA,
            canales=_CANALES_RESERVA, metodos=_METODOS_RESERVA,
            clientes=clientes,
        )

    @app.route("/admin/reservas/<int:reserva_id>/toggle-entrega", methods=["POST"])
    @auth.admin_required
    def reserva_toggle_entrega(reserva_id: int):
        """Alterna el estado de ENTREGA de la reserva (recibido / pendiente de
        entrega). Es independiente del pago (estado Pagada)."""
        r = Reserva.query.get_or_404(reserva_id)
        r.entregado = not bool(r.entregado)
        db.session.commit()
        if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"ok": True, "entregado": r.entregado})
        return redirect(request.referrer or url_for("admin_reservas"))

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
                    reserva.pagado = (nuevo == "Pagada")  # la venta se cuenta al Pagar
                    db.session.commit()

                    # Aplicar cambio de estado a TODOS los discos de la reserva
                    # Pendiente -> Reservado (apartado) | Pagada -> Vendido | Cancelada -> Disponible
                    estado_disco = {
                        "Pendiente": "Reservado",
                        "Pagada": "Vendido",
                        "Cancelada": "Disponible",
                    }.get(nuevo)

                    if estado_disco:
                        # Al marcar Pagada (la venta): asegurar el cliente y linkear la reserva
                        cli_res = None
                        if nuevo == "Pagada":
                            from models import Cliente
                            cli_res = Cliente.query.get(reserva.cliente_id) if reserva.cliente_id else None
                            if cli_res is None:
                                cli_res = _buscar_o_crear_cliente(
                                    reserva.nombre, reserva.whatsapp, reserva.email,
                                    origen=("web" if (reserva.canal or "web") == "web" else "manual"))
                                if cli_res and not reserva.cliente_id:
                                    reserva.cliente_id = cli_res.id
                            # Si la reserva no tiene email pero el cliente si, usarlo (para
                            # poder mandarle la confirmacion de compra).
                            if cli_res and not reserva.email and cli_res.email:
                                reserva.email = cli_res.email
                        for item in reserva.items_efectivos:
                            disco = item.disco
                            if not disco:
                                continue
                            # No des-vender un disco que ya vendio OTRA reserva/venta:
                            # bajarlo de Vendido borraria esa venta (fecha/precio) en silencio.
                            if (disco.estado == "Vendido" and estado_disco != "Vendido"
                                    and (disco.venta_grupo or "") != f"r{reserva.id}"):
                                continue
                            if disco.estado != estado_disco:
                                register_status_change(
                                    disco, estado_disco,
                                    nota=f"Reserva #{reserva.id} -> {nuevo}"
                                )
                            # Al vender: registrar canal (de la reserva), metodo, grupo y
                            # comprador — aunque el disco ya estuviera Vendido.
                            if estado_disco == "Vendido":
                                # Precio realmente cobrado = el snapshot de la reserva
                                # (Fire Sale/oferta si aplico), no el precio de lista.
                                if getattr(item, "precio_uyu_snap", None):
                                    disco.precio_venta_uyu = item.precio_uyu_snap
                                if not disco.canal_venta:
                                    disco.canal_venta = reserva.canal or "web"
                                if not disco.metodo_pago_venta:
                                    # El metodo REAL de la reserva (no derivar de 'pagado',
                                    # que ahora es True para toda venta y etiquetaba todo MP).
                                    _rm = (reserva.metodo_pago or "").lower()
                                    if _rm == "mercadopago" or reserva.mp_status == "approved":
                                        disco.metodo_pago_venta = "mercadopago"
                                    elif _rm in ("transferencia", "efectivo", "otro"):
                                        disco.metodo_pago_venta = _rm
                                if not disco.venta_grupo:
                                    disco.venta_grupo = f"r{reserva.id}"
                                if cli_res and not disco.cliente_id:
                                    disco.cliente_id = cli_res.id
                        db.session.commit()

                    # Al marcar PAGADA una compra por Mercado Pago o transferencia, avisarle
                    # al cliente que su compra quedo confirmada. (MP tambien lo manda solo al
                    # confirmarse el pago via webhook; esto cubre cuando VOS confirmas la
                    # transferencia y la marcas Pagada.)
                    _mail_ok = False
                    if (nuevo == "Pagada" and reserva.email and mailer.can_send()
                            and (reserva.metodo_pago or "") in ("mercadopago", "transferencia")):
                        try:
                            _discos = [i.disco for i in reserva.items_efectivos if i.disco]
                            # "confirmado" -> el mail dice "Tu pago quedo confirmado" (no el
                            # "recibimos tu comprobante" del checkout), porque aca ya lo verificaste.
                            mailer.send_compra_confirmacion_cliente(reserva, _discos, metodo="confirmado")
                            _mail_ok = True
                        except Exception:
                            _mail_ok = False
                    if _mail_ok:
                        flash(f"Reserva marcada como {nuevo}. Le mandamos el mail de confirmación de compra a {reserva.email}.", "success")
                    else:
                        flash(f"Reserva marcada como {nuevo}. (No se envió mail; usá el botón si querés avisarle.)", "success")

            elif accion == "enviar_mail":
                # Envio MANUAL del aviso de estado al cliente (lo dispara Facundo).
                if not reserva.email:
                    flash("Esta reserva no tiene email del cliente.", "error")
                elif not mailer.can_send():
                    flash("El envío de mails no está configurado (SMTP).", "error")
                else:
                    _discos = [i.disco for i in reserva.items_efectivos if i.disco]
                    texto_custom = (request.form.get("texto_mail") or "").strip() or None
                    try:
                        ok, msg = mailer.send_reserva_estado_cliente(reserva, reserva.estado, _discos, texto_custom=texto_custom)
                    except Exception as e:
                        ok, msg = False, str(e)
                    if ok:
                        flash(f"Mail enviado a {reserva.email} (estado: {reserva.estado}).", "success")
                    else:
                        flash(f"No se envió el mail: {msg}", "error")

            elif accion == "asignar_cliente":
                # Asignar (o cambiar) el cliente de una reserva ya existente a uno ya
                # cargado. Ademas atribuye los discos de la reserva a ese cliente, para
                # que aparezcan en sus compras (los Vendidos cuentan; los Reservados
                # quedan atribuidos para cuando se vendan).
                from models import Cliente
                cid = (request.form.get("cliente_id") or "").strip()
                if cid == "":
                    reserva.cliente_id = None
                    for item in reserva.items_efectivos:
                        if item.disco:
                            item.disco.cliente_id = None
                    db.session.commit()
                    flash("Reserva desvinculada del cliente.", "success")
                elif cid.isdigit() and Cliente.query.get(int(cid)):
                    cli = Cliente.query.get(int(cid))
                    reserva.cliente_id = cli.id
                    if not reserva.email and cli.email:
                        reserva.email = cli.email
                    for item in reserva.items_efectivos:
                        if item.disco:
                            item.disco.cliente_id = cli.id
                    db.session.commit()
                    flash(f"Reserva asignada a {cli.nombre}. Sus discos quedaron atribuidos a este cliente.", "success")
                else:
                    flash("Eleg&iacute; un cliente v&aacute;lido.", "warning")

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

        _par = mailer.RESERVA_ESTADO_MENSAJES.get(reserva.estado)
        mail_subject = f"{_par[0]} - pedido #{reserva.id} - Arcano Record Store" if _par else ""
        from models import Cliente
        clientes = Cliente.query.order_by(Cliente.nombre.asc()).all()
        cliente_actual_reserva = Cliente.query.get(reserva.cliente_id) if reserva.cliente_id else None
        return render_template(
            "admin_reserva_detail.html",
            reserva=reserva,
            estados=ESTADOS_RESERVA,
            conflictos=conflictos,
            tc=Setting.get_tc(),
            texto_mail_default=mailer.texto_estado_default(reserva.estado),
            mail_subject=mail_subject,
            clientes=clientes,
            cliente_actual_reserva=cliente_actual_reserva,
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
            "meta_pixel_id",
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
            "fire_sale_activa",
            "fire_sale_pct",
            "fire_sale_alcance",
            "fire_sale_fin",
            "fire_sale_mensaje",
            "lealtad_activa",
            "lealtad_umbral1",
            "lealtad_umbral2",
            "lealtad_premio1",
            "lealtad_premio2",
        ]
        carrusel_keys = ("carrusel_preorder", "carrusel_tips", "carrusel_escuchados",
                         "carrusel_destacados", "carrusel_nuevos")
        checkbox_keys = carrusel_keys + ("mercadopago_activo", "transferencia_activa",
                                         "fire_sale_activa", "lealtad_activa")
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
            # ID del pixel de Meta: solo digitos (evita inyeccion al insertarlo en el HTML)
            elif key == "meta_pixel_id":
                import re as _re
                value = _re.sub(r"\D", "", value)[:20]
            # Fire Sale: porcentaje entre 1 y 90
            elif key == "fire_sale_pct":
                try:
                    n = int(float(value or 0))
                    value = str(max(1, min(n, 90)))
                except (ValueError, TypeError):
                    value = "10"
            # Fire Sale: alcance solo puede ser "todo" o "seleccion"
            elif key == "fire_sale_alcance":
                value = "seleccion" if value == "seleccion" else "todo"
            # Fire Sale: fecha/hora de fin (datetime-local). Se valida que parsee;
            # si no, se guarda vacio (sin reloj).
            elif key == "fire_sale_fin":
                if value:
                    try:
                        datetime.fromisoformat(value)
                    except (ValueError, TypeError):
                        value = ""
            # Lealtad: umbrales enteros positivos (en pesos)
            elif key in ("lealtad_umbral1", "lealtad_umbral2"):
                try:
                    n = int(float(value or 0))
                    value = str(max(1, n))
                except (ValueError, TypeError):
                    value = "15000" if key == "lealtad_umbral1" else "30000"
            Setting.set(key, value)
        flash("Textos publicos actualizados.", "success")
        return redirect(url_for("settings_page"))

    @app.route("/admin/settings/video", methods=["POST"])
    @auth.admin_required
    def settings_update_video():
        """Actualiza SOLO el video de fondo del hero. Ruta propia para NO pisar el resto
        de la config (cobros, analytics, pixel, carruseles) que maneja el form grande:
        antes el form del video posteaba a settings_update_public y, al no mandar esos
        campos, los apagaba/borraba en silencio."""
        Setting.set("public_banner_video_url",
                    (request.form.get("public_banner_video_url") or "").strip())
        flash("Video de fondo actualizado.", "success")
        return redirect(url_for("settings_page"))

    @app.route("/admin/fire-sale/avisar", methods=["POST"])
    @auth.admin_required
    def fire_sale_avisar():
        """Manda el mail de aviso del Fire Sale a todos los clientes con email."""
        if not mailer.can_send():
            flash("El correo no esta configurado: no se pudo enviar el aviso.", "error")
            return redirect(url_for("settings_page"))
        cfg = fire_sale_config()
        pct = cfg.get("pct") or int(float(Setting.get("fire_sale_pct", "10") or 10))
        emails = [c.email for c in Cliente.query
                  .filter(Cliente.email.isnot(None), Cliente.email != "").all()]
        if not emails:
            flash("No hay clientes con email para avisar.", "warning")
            return redirect(url_for("settings_page"))

        import html as _html
        mensaje = (Setting.get("fire_sale_mensaje", "") or "").strip()
        url_tienda = url_for("home", _external=True)
        asunto = f"Fire Sale en Arcano: {pct}% de descuento para clientes"
        texto = (
            f"Activamos un Fire Sale con {pct}% de descuento, exclusivo para clientes con cuenta.\n\n"
            + (mensaje + "\n\n" if mensaje else "")
            + "Entra a la tienda e inicia sesion para ver los precios con descuento:\n"
            + url_tienda + "\n\nArcano Record Store"
        )
        msg_html = ("<p>" + _html.escape(mensaje).replace("\n", "<br>") + "</p>") if mensaje else ""
        html = (
            '<div style="font-family:Arial,Helvetica,sans-serif;max-width:520px;margin:0 auto;'
            'background:#0A0A0A;color:#EDEDED;padding:28px 24px;border-radius:12px;">'
            '<h1 style="font-size:22px;letter-spacing:2px;margin:0 0 4px;">ARCANO &middot; FIRE SALE</h1>'
            f'<p style="font-size:34px;font-weight:800;color:#ff5a3c;margin:8px 0 16px;">{pct}% OFF</p>'
            '<p style="font-size:15px;line-height:1.5;margin:0 0 12px;">Descuento <b>exclusivo para clientes con cuenta</b>. '
            'Inicia sesion en la tienda para ver los precios con el descuento aplicado.</p>'
            + msg_html +
            f'<p style="margin:22px 0;"><a href="{url_tienda}" '
            'style="background:#ff5a3c;color:#fff;text-decoration:none;padding:12px 22px;'
            'border-radius:8px;font-weight:700;display:inline-block;">Ver el Fire Sale &rarr;</a></p>'
            '<p style="font-size:12px;color:#888;margin-top:20px;">Arcano Record Store &middot; Montevideo</p>'
            '</div>'
        )
        enviados, fallidos = mailer.send_bulk(emails, asunto, texto, html)
        if enviados:
            extra = f" ({fallidos} no salieron)" if fallidos else ""
            flash(f"Aviso de Fire Sale enviado a {enviados} cliente(s).{extra}", "success")
        else:
            flash("No se pudo enviar el aviso (revisa la configuracion de correo).", "error")
        return redirect(url_for("settings_page"))

    # =====================================================================
    # BASE DE CLIENTES
    # =====================================================================
    def _compras_de_cliente(cli):
        """Lista de compras de un cliente. La historia real vive en las RESERVAS
        Pagadas (via sus items); ademas se suman las ventas de IG que quedaron
        atadas directo al disco (disc.cliente_id) y no pasaron por una reserva. Se
        deduplica por disco. Devuelve entradas {artista, titulo, precio, fecha, canal,
        disc_id}."""
        entradas = []
        vistos = set()
        # 1) Reservas Pagadas del cliente -> sus discos (con precio pagado)
        reservas = (Reserva.query
                    .filter(Reserva.cliente_id == cli.id, Reserva.estado == "Pagada")
                    .all())
        for r in reservas:
            for it in r.items_efectivos:
                did = it.disc_id
                if did and did in vistos:
                    continue
                if did:
                    vistos.add(did)
                d = it.disco
                entradas.append({
                    "disc_id": did,
                    "artista": (d.artista if d else it.artista_snap) or "",
                    "titulo": (d.titulo if d else it.titulo_snap) or "",
                    "precio": (it.precio_uyu_snap or (d.precio_mostrar if d else 0) or 0),
                    "fecha": (d.fecha_venta if (d and d.fecha_venta) else r.fecha),
                    "canal": (d.canal_venta if d else (r.canal or "web")) or "",
                })
        # 2) Discos vendidos atados directo a este cliente (venta rapida IG) no contados
        for d in (Disc.query
                  .filter(Disc.estado == "Vendido", Disc.cliente_id == cli.id)
                  .all()):
            if d.id in vistos:
                continue
            vistos.add(d.id)
            entradas.append({
                "disc_id": d.id, "artista": d.artista or "", "titulo": d.titulo or "",
                "precio": (d.precio_mostrar or d.precio_uyu or 0),
                "fecha": d.fecha_venta, "canal": d.canal_venta or "",
            })
        entradas.sort(key=lambda e: (e["fecha"] or datetime.min), reverse=True)
        return entradas

    def _clientes_agregados():
        """Agregados de compras por cliente (mismo criterio que _compras_de_cliente pero
        en bloque, para la lista): reservas Pagadas + ventas IG directas, deduplicado
        por disco. Devuelve {cliente_id: {n, total, ultima}} y {cliente_id: reservas activas}."""
        agg = {}   # cid -> {"discs": set, "sueltos": int, "total": float, "ultima": dt}

        def _add(cid, disc_id, precio, fecha):
            if not cid:
                return
            a = agg.setdefault(cid, {"discs": set(), "sueltos": 0, "total": 0.0, "ultima": None})
            if disc_id:
                if disc_id in a["discs"]:
                    return
                a["discs"].add(disc_id)
            else:
                a["sueltos"] += 1  # disco borrado: no se puede deduplicar, cuenta igual
            a["total"] += (precio or 0)
            if fecha and (not a["ultima"] or fecha > a["ultima"]):
                a["ultima"] = fecha

        # 1) Reservas Pagadas -> sus items (la historia real de compras)
        for r in (Reserva.query
                  .filter(Reserva.cliente_id.isnot(None), Reserva.estado == "Pagada")
                  .all()):
            for it in r.items_efectivos:
                # MISMA formula de precio/fecha que _compras_de_cliente (la ficha), para
                # que el total gastado y la lealtad coincidan entre la lista y la ficha.
                _d = it.disco
                _pre = (it.precio_uyu_snap or (_d.precio_mostrar if _d else 0) or 0)
                _fec = (_d.fecha_venta if (_d and _d.fecha_venta) else r.fecha)
                _add(r.cliente_id, it.disc_id, _pre, _fec)
        # 2) Ventas directas (IG) atadas al disco, no contadas arriba
        for d in (Disc.query
                  .filter(Disc.estado == "Vendido", Disc.cliente_id.isnot(None))
                  .all()):
            _add(d.cliente_id, d.id, (d.precio_mostrar or d.precio_uyu or 0), d.fecha_venta)

        compras = {cid: {"n": len(a["discs"]) + a["sueltos"], "total": a["total"], "ultima": a["ultima"]}
                   for cid, a in agg.items()}
        activas = {}
        for r in (Reserva.query
                  .filter(Reserva.cliente_id.isnot(None),
                          Reserva.estado.in_(["Pendiente"]))
                  .all()):
            activas[r.cliente_id] = activas.get(r.cliente_id, 0) + 1
        return compras, activas

    def _lealtad_config():
        """Config del programa de lealtad (barra de premios)."""
        def _int(k, d):
            try:
                return int(float(Setting.get(k, str(d)) or d))
            except (TypeError, ValueError):
                return d
        return {
            "activa": Setting.get("lealtad_activa", "0") == "1",
            "u1": max(1, _int("lealtad_umbral1", 15000)),
            "u2": max(1, _int("lealtad_umbral2", 30000)),
            "premio1": (Setting.get("lealtad_premio1", "") or "Un vale de regalo").strip(),
            "premio2": (Setting.get("lealtad_premio2", "") or "Un disco sorpresa de regalo").strip(),
        }

    def _lealtad_estado(cli, total_gastado):
        """Estado del programa de lealtad para un cliente, dado su gasto total real.

        Ciclo de u2: dos hitos (u1 y u2). progreso = gasto_total - lealtad_base.
        Al cobrar el premio del 2do hito, la base sube u2 (deja el excedente a favor).
        El premio del 1er hito se entrega en el camino (no reinicia)."""
        cfg = _lealtad_config()
        u1, u2 = cfg["u1"], cfg["u2"]
        base = int(getattr(cli, "lealtad_base", 0) or 0)
        total = int(round(total_gastado or 0))
        progreso = max(0, total - base)
        p1_cobrado = bool(getattr(cli, "premio1_cobrado", False))
        premio1_disp = progreso >= u1 and not p1_cobrado
        premio2_disp = progreso >= u2
        proximo = u1 if progreso < u1 else u2
        pct = min(100, round(progreso / u2 * 100)) if u2 > 0 else 0
        pct_u1 = min(100, round(u1 / u2 * 100)) if u2 > 0 else 50
        return {
            "activa": cfg["activa"], "u1": u1, "u2": u2,
            "premio1_desc": cfg["premio1"], "premio2_desc": cfg["premio2"],
            "base": base, "total": total, "progreso": progreso,
            "premio1_cobrado": p1_cobrado,
            "premio1_disponible": premio1_disp, "premio2_disponible": premio2_disp,
            "tiene_premio": premio1_disp or premio2_disp,
            "proximo_umbral": proximo, "falta": max(0, proximo - progreso),
            "pct": pct, "pct_u1": pct_u1,
        }

    @app.route("/admin/clientes")
    @auth.admin_required
    def admin_clientes():
        from models import Cliente
        q = (request.args.get("q") or "").strip().lower()
        clientes = Cliente.query.all()
        if q:
            clientes = [c for c in clientes if q in " ".join(filter(None, [
                (c.nombre or "").lower(), (c.email or "").lower(),
                (c.whatsapp or "").lower(), (c.instagram or "").lower()])).lower()]
        compras, activas = _clientes_agregados()
        # Los que mas gastaron primero (los que no compraron quedan al final)
        clientes.sort(key=lambda c: (-(compras.get(c.id, {}).get("total", 0) or 0),
                                     (c.nombre or "").lower()))
        # Lealtad: quienes cruzaron un umbral y tienen premio para entregar
        lcfg = _lealtad_config()
        premios = {}
        if lcfg["activa"]:
            for c in Cliente.query.all():
                est = _lealtad_estado(c, compras.get(c.id, {}).get("total", 0))
                if est["tiene_premio"]:
                    premios[c.id] = est
        con_cuenta = Cliente.query.filter(Cliente.password_hash.isnot(None)).count()
        return render_template(
            "admin_clientes.html", clientes=clientes, compras=compras,
            activas=activas, q=q, total=Cliente.query.count(),
            con_cuenta=con_cuenta,
            lealtad_activa=lcfg["activa"], premios=premios,
        )

    @app.route("/admin/clientes/export.csv")
    @auth.admin_required
    def admin_clientes_export():
        from models import Cliente
        import csv as _csv
        import io as _io
        compras, activas = _clientes_agregados()
        buf = _io.StringIO()
        w = _csv.writer(buf)
        w.writerow(["Nombre", "Celular", "Email", "Instagram", "Origen",
                    "Compras", "Total gastado (UYU)", "Ultima compra", "Reservas activas", "Notas"])
        for c in Cliente.query.order_by(Cliente.nombre.asc()).all():
            ag = compras.get(c.id, {})
            w.writerow([
                c.nombre or "", c.whatsapp or "", c.email or "",
                ("@" + c.instagram) if c.instagram else "", c.origen or "",
                ag.get("n", 0), round(ag.get("total", 0)),
                ag["ultima"].strftime("%d/%m/%Y") if ag.get("ultima") else "",
                activas.get(c.id, 0), (c.notas or "").replace("\n", " "),
            ])
        from flask import Response
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=arcano_clientes.csv"})

    @app.route("/admin/clientes/nuevo", methods=["POST"])
    @auth.admin_required
    def admin_cliente_nuevo():
        from models import Cliente
        nombre = (request.form.get("nombre") or "").strip()[:200]
        whatsapp = (request.form.get("whatsapp") or "").strip()[:50] or None
        email = (request.form.get("email") or "").strip().lower()[:200] or None
        instagram = _norm_ig(request.form.get("instagram"))
        notas = (request.form.get("notas") or "").strip() or None
        if not (nombre or whatsapp or instagram):
            flash("Poné al menos el nombre, el celular o el Instagram.", "error")
            return redirect(url_for("admin_clientes"))
        # Evitar duplicado obvio por email
        if email and Cliente.query.filter(db.func.lower(Cliente.email) == email).first():
            flash("Ya hay un cliente con ese email.", "warning")
            return redirect(url_for("admin_clientes"))
        cli = Cliente(nombre=nombre or (("@" + instagram) if instagram else whatsapp),
                      whatsapp=whatsapp, email=email, instagram=instagram, notas=notas,
                      origen="manual", fecha_registro=now_uy())
        db.session.add(cli)
        db.session.commit()
        flash(f"Cliente '{cli.nombre}' agregado.", "success")
        return redirect(url_for("admin_cliente_detail", cliente_id=cli.id))

    @app.route("/admin/compra/<int:disc_id>/canal", methods=["POST"])
    @auth.admin_required
    def compra_set_canal(disc_id):
        """Marca el canal (Web/Instagram/...) de una compra desde la ficha del cliente.
        Es el MISMO campo disc.canal_venta que edita el panel de Ventas (quedan sincro)."""
        d = Disc.query.get_or_404(disc_id)
        canal = (request.form.get("canal") or "").strip().lower()
        if canal and canal not in ("web", "instagram", "presencial", "otro"):
            return jsonify({"ok": False, "error": "canal invalido"}), 400
        d.canal_venta = canal or None
        db.session.commit()
        return jsonify({"ok": True, "canal": d.canal_venta or ""})

    @app.route("/admin/clientes/<int:cliente_id>")
    @auth.admin_required
    def admin_cliente_detail(cliente_id):
        from models import Cliente
        cli = Cliente.query.get_or_404(cliente_id)
        compras = _compras_de_cliente(cli)  # desde las reservas Pagadas + ventas IG
        reservas_activas = (Reserva.query
                            .filter(Reserva.cliente_id == cli.id,
                                    Reserva.estado.in_(["Pendiente"]))
                            .order_by(Reserva.fecha.desc()).all())
        total = sum(e["precio"] for e in compras)
        otros = (Cliente.query.filter(Cliente.id != cli.id)
                 .order_by(Cliente.nombre.asc()).all())
        lealtad = _lealtad_estado(cli, total)
        return render_template(
            "admin_cliente_detail.html", cli=cli, compras=compras,
            reservas_activas=reservas_activas, total=total, otros=otros,
            lealtad=lealtad,
        )

    @app.route("/admin/clientes/<int:cliente_id>/premio/<int:hito>", methods=["POST"])
    @auth.admin_required
    def admin_cliente_premio(cliente_id, hito):
        """Marca un premio de lealtad como ENTREGADO.
        hito 1: solo marca el premio del 1er umbral como cobrado (no reinicia).
        hito 2: entrega el premio del 2do umbral y REINICIA el ciclo (base += u2),
                dejando el excedente a favor. Tambien limpia el flag del 1er premio."""
        from models import Cliente
        cli = Cliente.query.get_or_404(cliente_id)
        cfg = _lealtad_config()
        # Recomputar el estado real ANTES de tocar el saldo, para no aplicar un premio
        # que no esta disponible (ej: doble clic / reenvio del POST que consumiria un
        # ciclo de mas y le comeria el excedente al cliente).
        _total = sum(e["precio"] for e in _compras_de_cliente(cli))
        est = _lealtad_estado(cli, _total)
        if hito == 1:
            if not est["premio1_disponible"]:
                flash("El premio 1 no esta disponible (ya lo entregaste o todavia no lo alcanzo).", "warning")
                return redirect(url_for("admin_cliente_detail", cliente_id=cli.id))
            cli.premio1_cobrado = True
            db.session.commit()
            flash(f"Premio 1 marcado como entregado a {cli.nombre}.", "success")
        elif hito == 2:
            if not est["premio2_disponible"]:
                flash("El premio 2 no esta disponible todavia (el cliente no llego a la meta).", "warning")
                return redirect(url_for("admin_cliente_detail", cliente_id=cli.id))
            cli.lealtad_base = int(getattr(cli, "lealtad_base", 0) or 0) + cfg["u2"]
            cli.premio1_cobrado = False
            db.session.commit()
            # Aviso si tambien tenia el premio 1 sin entregar (el reinicio lo deja atras).
            if est["premio1_disponible"]:
                flash(f"Premio 2 entregado a {cli.nombre}: el ciclo se reinicia (el excedente queda a favor). OJO: este cliente TAMBIEN tenia el premio 1 pendiente — asegurate de dárselo.", "success")
            else:
                flash(f"Premio 2 entregado a {cli.nombre}: el ciclo se reinicia y el excedente queda a favor.", "success")
        else:
            flash("Hito invalido.", "error")
        return redirect(url_for("admin_cliente_detail", cliente_id=cli.id))

    @app.route("/admin/clientes/<int:cliente_id>/editar", methods=["POST"])
    @auth.admin_required
    def admin_cliente_editar(cliente_id):
        from models import Cliente
        cli = Cliente.query.get_or_404(cliente_id)
        cli.nombre = (request.form.get("nombre") or "").strip()[:200] or cli.nombre
        cli.whatsapp = (request.form.get("whatsapp") or "").strip()[:50] or None
        email = (request.form.get("email") or "").strip().lower()[:200] or None
        if email and email != (cli.email or "").lower():
            otro = Cliente.query.filter(db.func.lower(Cliente.email) == email,
                                        Cliente.id != cli.id).first()
            if otro:
                flash("Otro cliente ya usa ese email.", "error")
                return redirect(url_for("admin_cliente_detail", cliente_id=cli.id))
        cli.email = email
        cli.instagram = _norm_ig(request.form.get("instagram"))
        cli.notas = (request.form.get("notas") or "").strip() or None
        db.session.commit()
        flash("Cliente actualizado.", "success")
        return redirect(url_for("admin_cliente_detail", cliente_id=cli.id))

    @app.route("/admin/clientes/<int:cliente_id>/fusionar", methods=["POST"])
    @auth.admin_required
    def admin_cliente_fusionar(cliente_id):
        """Fusiona ESTE cliente dentro de otro (destino): pasa sus compras y reservas al
        destino, le completa los datos que le falten, y borra este."""
        from models import Cliente
        cli = Cliente.query.get_or_404(cliente_id)
        destino_id = request.form.get("destino_id")
        destino = Cliente.query.get(int(destino_id)) if (destino_id or "").isdigit() else None
        if not destino or destino.id == cli.id:
            flash("Elegí un cliente destino válido para fusionar.", "error")
            return redirect(url_for("admin_cliente_detail", cliente_id=cli.id))
        # Mover compras (discos) y reservas al destino
        Disc.query.filter_by(cliente_id=cli.id).update({"cliente_id": destino.id})
        Reserva.query.filter_by(cliente_id=cli.id).update({"cliente_id": destino.id})
        # Completar datos que al destino le falten
        if not destino.whatsapp and cli.whatsapp:
            destino.whatsapp = cli.whatsapp
        if not destino.email and cli.email:
            destino.email = cli.email
        if not destino.instagram and cli.instagram:
            destino.instagram = cli.instagram
        if cli.notas:
            destino.notas = ((destino.notas + "\n") if destino.notas else "") + cli.notas
        db.session.delete(cli)
        db.session.commit()
        flash(f"Cliente fusionado dentro de '{destino.nombre}'.", "success")
        return redirect(url_for("admin_cliente_detail", cliente_id=destino.id))

    @app.route("/admin/clientes/<int:cliente_id>/eliminar", methods=["POST"])
    @auth.admin_required
    def admin_cliente_eliminar(cliente_id):
        from models import Cliente
        cli = Cliente.query.get_or_404(cliente_id)
        # Los discos comprados quedan con cliente_id NULL (no se borra la venta)
        Disc.query.filter_by(cliente_id=cli.id).update({"cliente_id": None})
        Reserva.query.filter_by(cliente_id=cli.id).update({"cliente_id": None})
        nombre = cli.nombre
        db.session.delete(cli)
        db.session.commit()
        flash(f"Cliente '{nombre}' eliminado.", "success")
        return redirect(url_for("admin_clientes"))

    @app.route("/admin/clientes/eliminar-multiple", methods=["POST"])
    @auth.admin_required
    def admin_clientes_eliminar_multiple():
        """Borra varios clientes de una (para limpiar los que solo testearon la web).
        Las ventas NO se borran: los discos/reservas quedan sin ese cliente."""
        from models import Cliente
        ids = [int(x) for x in request.form.getlist("ids") if str(x).isdigit()]
        if not ids:
            flash("No elegiste ningún cliente.", "warning")
            return redirect(url_for("admin_clientes"))
        Disc.query.filter(Disc.cliente_id.in_(ids)).update({"cliente_id": None}, synchronize_session=False)
        Reserva.query.filter(Reserva.cliente_id.in_(ids)).update({"cliente_id": None}, synchronize_session=False)
        n = Cliente.query.filter(Cliente.id.in_(ids)).delete(synchronize_session=False)
        db.session.commit()
        flash(f"{n} cliente(s) eliminado(s). Las ventas no se borran, solo se desvinculan.", "success")
        return redirect(url_for("admin_clientes"))

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
            ip = _client_ip()
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
