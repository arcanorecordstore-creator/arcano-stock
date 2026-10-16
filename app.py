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
from sqlalchemy.orm import selectinload

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
    sello_base,
    invalidar_ajuste_tc,
    ESTADOS_DISCO,
    AUDIO_ESTADOS,
    ESTADOS_RESERVA,
    CONDICIONES_DISCO,
    now_uy,
    today_uy,
    fire_sale_config,
    MYSTERY_BOX_PRECIOS,
    mystery_box_precio,
)
import vision
import scraper

# ---------------------------------------------------------------------------
# Version del proyecto. UNICA fuente de verdad: la muestran el pie del admin y
# el pie de la tienda publica. Al entregar un cambio, se bumpea SOLO aca.
# ---------------------------------------------------------------------------
APP_VERSION = "6.14"
import metrics
import auth
import mailer
import mp
from operation_history import register_history, visible_records, archived_ids


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


# Tope de discos que puede apartar un pedido SIN pago online (coordinar / transferencia).
# Decision de Facundo (12/10/2026): maximo 10.
MAX_DISCOS_SIN_PAGO = 10


def _vendible_web(d):
    """True si el disco se puede pedir desde la tienda: no agotado ni oculto, no es la
    ficha de la Mystery Box (esa se pide con su propio formulario) y tiene precio."""
    return (d is not None and not d.esta_agotado and not d.oculto_web
            and not getattr(d, "es_mystery_box", False)
            and (d.precio_uyu or 0) > 0)


_mail_cuenta_hist: dict = {}  # clave (ip o email) -> timestamps de mails del registro


def _mail_cuenta_limitado(email, tope_ip=6, tope_email=2, ventana=3600):
    """Tope de mails que dispara el registro de cuentas: por conexion y por email.
    Devuelve True si se paso (y entonces NO hay que mandar el mail)."""
    import time as _t
    from flask import request as _rq
    ahora = _t.time()
    ip = _rq.headers.get("X-Forwarded-For", _rq.remote_addr or "")
    claves = ((("ip", ip), tope_ip), (("em", (email or "").lower()), tope_email))
    limitado = False
    for clave, tope in claves:
        marcas = [t for t in _mail_cuenta_hist.get(clave, []) if ahora - t < ventana]
        _mail_cuenta_hist[clave] = marcas
        if len(marcas) >= tope:
            limitado = True
    if not limitado:
        for clave, _ in claves:
            _mail_cuenta_hist[clave].append(ahora)
    if len(_mail_cuenta_hist) > 5000:
        _mail_cuenta_hist.clear()
    return limitado


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


def _ruta_interna_segura(destino, prefijo="/"):
    """Devuelve `destino` solo si es una ruta de ESTE sitio que empieza con `prefijo`
    (y sin caracteres raros); si no, None. Acepta tambien una URL completa del propio
    host (el referer). Antes se chequeaba solo que empezara con "/": un link como
    /cuenta/login?next=/%09/evil.com mandaba al cliente a otro sitio despues del login."""
    from urllib.parse import urlsplit
    if not destino or not isinstance(destino, str):
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in destino) or "\\" in destino:
        return None
    try:
        partes = urlsplit(destino)
    except ValueError:
        return None
    if partes.scheme or partes.netloc:
        try:
            from flask import request as _rq
            propio = _rq.host
        except Exception:
            propio = None
        if partes.scheme not in ("http", "https") or not propio or partes.netloc != propio:
            return None
    ruta = partes.path or ""
    if not ruta.startswith(prefijo) or ruta.startswith("//"):
        return None
    return ruta + (("?" + partes.query) if partes.query else "")


def _base_publica():
    """Direccion publica de la web para links que salen por mail o a Mercado Pago.
    En produccion es fija (PUBLIC_BASE_URL o arcanorecordstore.com): antes se armaba con
    el Host que mandaba quien hacia el pedido, y alguien podia hacer que el mail de
    "cambiar contrasena" le llegara a un cliente con un link a OTRO sitio."""
    base = (os.environ.get("PUBLIC_BASE_URL") or "").strip()
    if not base and os.environ.get("RENDER"):
        base = "https://arcanorecordstore.com"
    if not base:
        from flask import request as _rq
        base = _rq.url_root
    return base.rstrip("/")


def _norm_busqueda(texto):
    """Texto para comparar en el buscador: minusculas, sin tildes ni signos raros
    (Verniß -> verniss, Garçon -> garcon, Ø -> o). Asi "vernis" encuentra "Verniß"."""
    import unicodedata as _ud
    t = str(texto or "").lower()
    t = (t.replace("ß", "ss").replace("ø", "o").replace("æ", "ae").replace("œ", "oe")
          .replace("đ", "d").replace("ł", "l"))
    t = _ud.normalize("NFKD", t)
    return "".join(ch for ch in t if not _ud.combining(ch))


_recuperar_hist = {}   # clave (ip o email) -> timestamps de pedidos de "olvide mi contrasena"
_play_ip_hist = {}   # ip -> timestamps de escuchas contadas (tope por IP)
_busq_ip_hist = {}   # ip -> timestamps de busquedas registradas en el Radar (tope por IP)


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
    # Tope por IP (cualquier track): 40 escuchas cada 10 minutos. Una persona
    # escuchando a mano no llega; un bot que recorre todos los tracks si, y cada
    # escucha es una escritura en la base (con el disco de 1 GB, eso importa).
    hist = [t for t in _play_ip_hist.get(ip, []) if ahora - t < 600]
    if len(hist) >= 40:
        _play_ip_hist[ip] = hist
        return True
    hist.append(ahora)
    _play_ip_hist[ip] = hist
    if len(_play_ip_hist) > 5000:
        for k in list(_play_ip_hist.keys())[:2500]:
            _play_ip_hist.pop(k, None)
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


from werkzeug.routing import BaseConverter as _BaseConverter


class _DiscoSlugConverter(_BaseConverter):
    """Direccion de la ficha: url_for('public_disc', disc_id=<id>) devuelve
    /artista-titulo (sin tocar las plantillas). Al entrar, la vista recibe el texto
    de la direccion y busca el disco."""
    regex = r"[^/]+"

    def to_python(self, value):
        return value

    def to_url(self, value):
        from models import Disc as _D
        d = value if isinstance(value, _D) else None
        if d is None:
            try:
                d = db.session.get(_D, int(value))
            except Exception:
                d = None
        if d is not None and getattr(d, "slug", None):
            return d.slug
        return super().to_url(getattr(d, "id", value))


def create_app() -> Flask:
    app = Flask(__name__)
    if os.environ.get("RENDER"):
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=0, x_proto=1, x_host=0)

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
    _ENDPOINTS_UPLOAD_GRANDE = {"disc_new_from_video", "disc_video_review", "disc_new_combo"}
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

    @app.errorhandler(500)
    def _handle_500(e):
        # Ante un 500 dejamos la sesion de base limpia (rollback) para que el proximo
        # request no arrastre el error. Si el disco esta lleno, avisamos claro.
        orig = getattr(e, "original_exception", None) or e
        try:
            db.session.rollback()
        except Exception:
            pass
        if "disk is full" in str(orig).lower() or "database or disk is full" in str(orig).lower():
            return (
                "<h1>Sin espacio en disco</h1><p>El disco del servidor esta lleno y no se "
                "puede guardar. Libera espacio (Ajustes &rarr; limpiar samples) o agranda el "
                "disco en Render, y volve a intentar.</p>"
            ), 500
        return "Internal Server Error", 500

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

    # --- Sesion de cliente atada a su contrasena ---
    # Cada sesion guarda una "version" de la contrasena (un pedazo del hash). Si la
    # contrasena cambia (reset, activacion), las sesiones abiertas con la version vieja
    # se cierran: quien hubiera entrado con un acceso viejo queda afuera.
    @app.before_request
    def _sesion_cliente_vigente():
        cid = session.get("cliente_id")
        if not cid or request.path.startswith(("/static/", "/admin")):
            return
        try:
            from models import Cliente as _Cli
            cli = _Cli.query.get(cid)
        except Exception:
            return
        if cli is None or not cli.password_hash:
            auth.cliente_logout()
            return
        pv = cli.password_hash[-16:]
        if "cliente_pv" not in session:
            session["cliente_pv"] = pv
        elif session.get("cliente_pv") != pv:
            auth.cliente_logout()

    # --- Proteccion CSRF liviana para el admin: verificar el origen de los POST ---
    # Si un POST a /admin viene desde otro sitio (Origin/Referer ajeno), se rechaza.
    # No requiere tokens. Cubre admin Y tienda publica (reservas, checkout, cuenta);
    # excluye el webhook de Mercado Pago, que lo llama MP desde otro servidor.
    @app.before_request
    def csrf_origin_check():
        # El webhook de MP y la API externa (Drop Hunter) los llaman otros servidores,
        # sin Origin del sitio; la API se protege aparte con su clave secreta.
        if (request.method in ("POST", "PUT", "DELETE")
                and not request.path.startswith("/mp/webhook")
                and not request.path.startswith("/api/")):
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
            "background_color": "#1E1C1C",
            "theme_color": "#1E1C1C",
            "icons": [
                {"src": url_for("static", filename="brand/app-icon-192.png"), "sizes": "192x192", "type": "image/png"},
                {"src": url_for("static", filename="brand/app-icon-512.png"), "sizes": "512x512", "type": "image/png"},
                {"src": url_for("static", filename="brand/app-icon-maskable-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
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
const CACHE = 'arcano-admin-v2';
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
  // SOLO cacheamos estaticos y portadas (inmutables por nombre). Las paginas del
  // admin NO se tocan: van siempre a la red, para no servir nunca una version vieja
  // ni datos financieros desactualizados.
  if (url.pathname.indexOf('/static/') === 0 || url.pathname.indexOf('/covers/') === 0) {
    e.respondWith(caches.match(req).then(function(hit){
      return hit || fetch(req).then(function(res){
        if (res && res.status === 200) { var copy = res.clone(); caches.open(CACHE).then(function(c){ c.put(req, copy); }); }
        return res;
      });
    }));
  }
});
self.addEventListener('push', function(e){
  var data = {};
  try { data = e.data ? e.data.json() : {}; } catch(err) {}
  var opts = {
    body: data.body || '',
    icon: '/static/brand/app-icon-192.png',
    badge: '/static/brand/app-icon-192.png',
    vibrate: [80, 40, 80],
    data: { url: data.url || '/admin/' }
  };
  e.waitUntil(self.registration.showNotification(data.title || 'Arcano', opts));
});
self.addEventListener('notificationclick', function(e){
  e.notification.close();
  var url = (e.notification.data && e.notification.data.url) || '/admin/';
  e.waitUntil(clients.matchAll({ type: 'window' }).then(function(cl){
    for (var i = 0; i < cl.length; i++) { if (cl[i].url.indexOf(url) >= 0 && 'focus' in cl[i]) return cl[i].focus(); }
    if (clients.openWindow) return clients.openWindow(url);
  }));
});
"""
        resp = _Resp(sw, mimetype="application/javascript")
        resp.headers["Service-Worker-Allowed"] = "/admin/"
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    # ---- Avisos al celular (Web Push) ----
    def _vapid_keys():
        """(private_pem, public_key_b64). Se generan UNA vez y quedan en Settings,
        asi Facundo no tiene que configurar nada."""
        priv = Setting.get("vapid_private_pem", "") or ""
        pub = Setting.get("vapid_public_key", "") or ""
        if priv and pub:
            return priv, pub
        try:
            from py_vapid import Vapid01
            from cryptography.hazmat.primitives import serialization
            import base64 as _b64
            v = Vapid01(); v.generate_keys()
            priv = v.private_pem().decode()
            pubk = v.public_key.public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
            pub = _b64.urlsafe_b64encode(pubk).rstrip(b"=").decode()
            Setting.set("vapid_private_pem", priv)
            Setting.set("vapid_public_key", pub)
            return priv, pub
        except Exception:
            return "", ""

    @app.route("/admin/push/key")
    @auth.admin_required
    def push_key():
        _, pub = _vapid_keys()
        return jsonify({"key": pub})

    @app.route("/admin/push/subscribe", methods=["POST"])
    @auth.admin_required
    def push_subscribe():
        from models import PushSubscription
        data = request.get_json(silent=True) or {}
        endpoint = data.get("endpoint")
        keys = data.get("keys") or {}
        p256dh = keys.get("p256dh"); auth_k = keys.get("auth")
        if not (endpoint and p256dh and auth_k):
            return jsonify({"ok": False, "error": "datos incompletos"}), 400
        if not PushSubscription.query.filter_by(endpoint=endpoint).first():
            db.session.add(PushSubscription(endpoint=endpoint, p256dh=p256dh, auth=auth_k))
            db.session.commit()
        return jsonify({"ok": True})

    def _enviar_push(titulo, cuerpo, url="/admin/"):
        """Manda un aviso a todos los dispositivos suscritos. Silencioso: nunca
        rompe el flujo que lo dispara (una venta/reserva). Queda colgada de la app
        (app.enviar_push) para que la usen las rutas de register_routes."""
        from models import PushSubscription
        try:
            from pywebpush import webpush, WebPushException
        except Exception:
            return
        priv, _ = _vapid_keys()
        if not priv:
            return
        subs = PushSubscription.query.all()
        if not subs:
            return
        import json as _json
        payload = _json.dumps({"title": titulo, "body": cuerpo, "url": url})
        muertas = []
        for s in subs:
            try:
                webpush(
                    subscription_info={"endpoint": s.endpoint,
                                       "keys": {"p256dh": s.p256dh, "auth": s.auth}},
                    data=payload,
                    vapid_private_key=priv,
                    vapid_claims={"sub": "mailto:arcanorecordstore@gmail.com"},
                )
            except WebPushException as e:
                _r = getattr(e, "response", None)
                if _r is not None and _r.status_code in (404, 410):
                    muertas.append(s)   # suscripcion vencida -> limpiar
            except Exception:
                pass
        for s in muertas:
            db.session.delete(s)
        if muertas:
            db.session.commit()

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

    app.url_map.converters["disco_slug"] = _DiscoSlugConverter
    init_db(app)
    _backfill_ventas_sin_cliente(app)
    app.enviar_push = _enviar_push
    register_routes(app)
    # Direcciones de las fichas: no pueden pisar ninguna ruta de la app
    try:
        import models as _mdl
        for _rule in app.url_map.iter_rules():
            _seg = _rule.rule.lstrip("/").split("/")[0].lower()
            if _seg and "<" not in _seg:
                _mdl.SLUGS_RESERVADOS.add(_seg)
        _mdl.asignar_slugs_faltantes(app)
    except Exception as _e:
        print("Aviso: no se pudieron armar las direcciones de los discos:", _e)
    from security import init_security
    init_security(app)
    register_history(app)

    # Context processor: vars disponibles en todos los templates
    @app.context_processor
    def inject_globals():
        # Pendientes reales (para la tarjeta del dashboard "N pendiente/s").
        try:
            reservas_pendientes_count = Reserva.query.filter_by(estado="Pendiente").count()
        except Exception:
            reservas_pendientes_count = 0
        # Badge del nav = reservas NUEVAS que todavia no viste (id mayor al ultimo
        # visto). Se limpia al entrar a /admin/reservas. Es solo el aviso de "llego
        # algo nuevo"; los pendientes se siguen viendo adentro.
        try:
            from models import Setting as _S
            try:
                _visto = int(_S.get("reservas_visto_hasta_id", "0") or 0)
            except (TypeError, ValueError):
                _visto = 0
            reservas_sin_ver_count = Reserva.query.filter(Reserva.id > _visto).count()
        except Exception:
            reservas_sin_ver_count = 0

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

        def _clientes_para_encargo():
            """Clientes para el selector 'Encargo de' del form del disco. Se pasa
            como funcion (no como lista) para que la consulta corra SOLO en el form."""
            try:
                return Cliente.query.order_by(Cliente.nombre.asc()).all()
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

        # Diseno de la tienda (Ajustes -> Diseno de la tienda): tema, carruseles y modo
        try:
            from models import Setting as _SD
            _diseno = _SD.get("public_diseno", "") or ""
            _carr_modo = _SD.get("public_carrusel_modo", "compacto") or "compacto"
            _modo_def = _SD.get("public_modo_defecto", "oscuro") or "oscuro"
            _admin_diseno = _SD.get("admin_diseno", "") or ""
        except Exception:
            _diseno, _carr_modo, _modo_def, _admin_diseno = "", "compacto", "oscuro", ""

        return {
            "diseno_tienda": _diseno,
            "carrusel_modo": _carr_modo,
            "modo_defecto": _modo_def,
            "admin_diseno": _admin_diseno,
            "reservas_sin_ver_count": reservas_sin_ver_count,
            "reservas_pendientes_count": reservas_pendientes_count,
            "now": now_uy(),
            "url_pagina": url_pagina,
            "sort_url": sort_url,
            "cliente_actual": cliente_actual,
            "todos_los_pedidos": _todos_los_pedidos(),
            "clientes_para_encargo": _clientes_para_encargo,
            # TC EUR y USD disponibles en TODAS las paginas (el header del admin los muestra)
            "tc": Setting.get_tc(),
            "tc_usd": Setting.get_tc_usd(),
            # Version del proyecto, disponible en el admin Y en la tienda publica.
            "app_version": APP_VERSION,
        }

    @app.template_filter("texto")
    def _texto_legible(valor):
        """Para avisos (flash) y mensajes: convierte los codigos HTML de las tildes
        (&iacute;, &ntilde;...) en letras normales y despues escapa el texto. Antes se
        veia literal "Eleg&iacute;" o "per&iacute;odo"; y escapar despues mantiene a
        salvo los avisos que incluyen datos que escribio un cliente (nombre, email)."""
        import html as _html
        from markupsafe import escape as _esc
        return _esc(_html.unescape(str(valor or "")))

    @app.template_filter("hora_uy")
    def _hora_uy(dt, fmt="%d/%m/%Y %H:%M"):
        """Muestra un datetime (guardado en UTC por default=datetime.utcnow) en hora
        de Montevideo (UTC-3). Evita mostrar 'UTC' 3 horas adelantado."""
        if dt is None:
            return ""
        try:
            from datetime import timedelta as _td
            return (dt - _td(hours=3)).strftime(fmt)
        except Exception:
            return ""

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
    if order_by in ("ganancia_uyu", "margen_pct", "costo_final_uyu"):
        discs = q.all()
        reverse = direction != "asc"
        if order_by == "costo_final_uyu":
            # La columna muestra el costo REAL (con la diferencia de cambio del pedido)
            discs.sort(key=lambda d: d.costo_real_uyu, reverse=reverse)
        elif order_by == "ganancia_uyu":
            discs.sort(key=lambda d: d.ganancia_uyu or 0, reverse=reverse)
        else:
            discs.sort(key=lambda d: d.margen_pct or 0, reverse=reverse)
        return discs

    col = ORDER_FIELDS.get(order_by, Disc.fecha_ingreso)
    q = q.order_by(col.asc()) if direction == "asc" else q.order_by(col.desc())
    return q


def compute_totals(discs):
    # La Mystery Box NO es inventario real: no tiene costo ni stock fisico (su
    # "stock" es solo para que nunca figure agotada en la tienda). Si entra aca,
    # infla el valor de venta y la ganancia potencial por millones. Se excluye de
    # TODOS los totales; la fila igual se sigue viendo en la tabla.
    discs = [d for d in discs if not getattr(d, "es_mystery_box", False)]
    total = len(discs)
    suma_costo_eur = 0.0
    suma_envio_eur = 0.0
    suma_costo_final_uyu = 0.0
    suma_ajuste_tc_uyu = 0.0
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
        costo_neg = 0.0 if (d.origen or "arcano") == "personal" else d.costo_real_uyu
        gan = ((d.precio_mostrar or d.precio_uyu or 0) - costo_neg)
        suma_costo_eur += (d.costo_eur or 0) * cant
        suma_envio_eur += (d.envio_eur or 0) * cant
        suma_costo_final_uyu += d.costo_real_uyu * cant
        suma_ajuste_tc_uyu += d.ajuste_tc_uyu * cant
        # precio_mostrar (con oferta) para que el pie cuadre: precio - costo = ganancia
        # (la ganancia ya usa el precio de oferta).
        suma_precio_uyu += (d.precio_mostrar or d.precio_uyu or 0) * cant
        suma_ganancia_uyu += gan * cant
        if d.estado != "Vendido":
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
        "suma_ajuste_tc_uyu": round(suma_ajuste_tc_uyu, 2),
        "suma_precio_uyu": round(suma_precio_uyu, 2),
        "suma_ganancia_uyu": round(suma_ganancia_uyu, 2),
        "ganancia_potencial_uyu": round(ganancia_potencial_uyu, 2),
        "ganancia_realizada_uyu": round(ganancia_realizada_uyu, 2),
    }


# Reglas de envio (checkout). Montevideo: $50 con 1-2 discos, GRATIS con 3+.
# Interior: $0 en la web (lo cobra la agencia). Retiro: $0.
ENVIO_MVD_COSTO = 50
ENVIO_MVD_GRATIS_DESDE = 3


# Candados por cliente para el checkout: evitan que dos envios simultaneos del
# mismo pedido (doble click) creen dos reservas. Se guarda uno por email.
import threading as _threading
_checkout_locks = {}
_checkout_locks_guard = _threading.Lock()


def _lock_checkout(clave):
    with _checkout_locks_guard:
        lk = _checkout_locks.get(clave)
        if lk is None:
            lk = _threading.Lock()
            # Higiene: si se acumulan muchos (muchos mails distintos), se limpian los
            # que no estan en uso. Nunca crece sin control.
            if len(_checkout_locks) > 500:
                for k in [k for k, v in list(_checkout_locks.items()) if not v.locked()]:
                    _checkout_locks.pop(k, None)
            _checkout_locks[clave] = lk
        return lk


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
                "item_id": str(it.disc_id) if it.disc_id else ("mystery-box" if getattr(it, "es_mystery_box", False) else ""),
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


_GENERO_EXCLUIR = {"vinyl only", "vinyl", "vinilo", "only", ""}


def _norm_generos(genero_str):
    """Separa un campo 'genero' en generos individuales.

    - Separadores: coma, ' - ' y ' / ' (con espacios). Un guion SIN espacios
      (Trip-Hop, Break-Beat) NO separa: es parte del nombre.
    - Excluye no-generos (Vinyl Only, etc.).
    - Dedupe case-insensitive, conservando el primer texto visto.
    """
    if not genero_str:
        return []
    import re as _re
    out = []
    seen = set()
    for tok in _re.split(r",|\s+-\s+|\s+/\s+", genero_str):
        t = tok.strip()
        if not t or t.lower() in _GENERO_EXCLUIR:
            continue
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


def _pretty_genero(t):
    """Forma de display: Title Case, salvo siglas cortas en mayuscula (IDM, EBM)."""
    t = t.strip()
    if t.isupper() and len(t) <= 4:
        return t
    return t.title()


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
    # Encargo: el disco se trae para un cliente puntual. Solo se toca el cliente
    # cuando el estado es Encargo (en otros estados el cliente lo maneja la venta/reserva).
    if estado == "Encargo" and "encargo_cliente_id" in form:
        _cid = (form.get("encargo_cliente_id") or "").strip()
        disc.cliente_id = int(_cid) if _cid.isdigit() else None
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
    if "tags" in form:   # el campo se saco del form: no pisar lo guardado
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
    estado_anterior = disc.estado
    disc.estado = nuevo_estado

    # Salir de Vendido = la venta se deshace: limpiar TODO lo que ata el disco a esa
    # venta (comprador, canal, metodo, grupo). Si no, la proxima venta de este disco
    # heredaba el comprador y el metodo "mercadopago" de la anterior (caja con plata
    # "retenida en MP" que estaba en mano, lealtad del cliente viejo inflada) y el panel
    # de Ventas la agrupaba con la venta vieja. Una fila = una copia: si el stock habia
    # quedado en 0 al vender, vuelve a 1 (si no, queda "Disponible" pero agotado).
    if estado_anterior == "Vendido" and nuevo_estado != "Vendido":
        disc.cliente_id = None
        disc.canal_venta = None
        disc.metodo_pago_venta = None
        disc.venta_grupo = None
        if (disc.stock or 0) <= 0 and nuevo_estado in ("Disponible", "Reservado", "Pre-order", "Encargo"):
            disc.stock = 1
    # Un pre-order tomado por un pedido web lleva venta_grupo "r<id>". Si deja de ser
    # pre-order sin venderse (llego y lo pasaste a Disponible, Reservado, etc.), se
    # suelta de ese pedido: si no, una venta futura quedaria atada al pedido viejo.
    if (estado_anterior in ("Pre-order", "Encargo") and nuevo_estado != "Vendido"
            and nuevo_estado not in ("Pre-order", "Encargo")):
        disc.venta_grupo = None
    # Reservado -> Disponible = la reserva se cayo: el disco ya no es de ese cliente.
    # Si no, la proxima venta (por MP o reserva) le quedaba a nombre del anterior.
    if estado_anterior == "Reservado" and nuevo_estado == "Disponible":
        disc.cliente_id = None

    if nuevo_estado == "Reservado" and not disc.fecha_reserva:
        disc.fecha_reserva = ahora
    if nuevo_estado == "Vendido":
        # Una fila = una copia: vendida, no queda stock (MP y la Mystery Box ya lo
        # hacian; el cambio de estado a mano dejaba stock 1 y el pie de Stock lo sumaba).
        if estado_anterior != "Vendido":
            disc.stock = 0
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

    # Sena de reserva: solo aplica mientras el disco esta Reservado (apartado), Encargo
    # (el cliente adelanto plata por un disco que le voy a traer) o Vendido
    # (donde la sena fue el adelanto). Si va a cualquier otro estado (Disponible/Descartado/
    # etc.) la reserva se cayo -> limpiar la sena para que no quede plata huerfana en la caja.
    if nuevo_estado not in ("Reservado", "Encargo", "Vendido") and getattr(disc, "sena_cobrada", False):
        disc.sena_cobrada = False
        disc.sena_monto_uyu = None
        disc.sena_fecha = None
        disc.sena_metodo = None

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
                            origen="web", crear=True, seguro=False):
    """Base de clientes: busca uno existente por email / whatsapp / instagram (en ese
    orden) y, si no hay, lo crea. Rellena los datos que le falten al que encuentre (sin
    pisar lo que ya tiene). Devuelve el Cliente, o None si no hay datos utiles.

    seguro=True (compras web que se atan solas): por celular o Instagram SOLO se toma
    una ficha que NO es cuenta web (sin contrasena). Si no, alguien que se registraba
    con el celular de otra persona veia sus compras en "Mi cuenta" y se llevaba sus
    puntos de lealtad."""
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
        _q = Cliente.query.filter(Cliente.whatsapp == whatsapp)
        if seguro:
            _q = _q.filter(Cliente.password_hash.is_(None))
        cli = _q.first()
    if not cli and ig:
        _q = Cliente.query.filter(db.func.lower(Cliente.instagram) == ig)
        if seguro:
            _q = _q.filter(Cliente.password_hash.is_(None))
        cli = _q.first()
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
            # v5.77: este backfill corria en CADA arranque y, como puede CREAR clientes,
            # resucitaba los que borrabas a mano (y re-ataba reservas que desvinculaste)
            # en cada deploy. Ya corrio en todos los arranques anteriores, asi que las
            # ventas viejas estan atadas: se marca como hecho SIN volver a correrlo.
            _FLAG_BF = "backfill_ventas_cliente_hecho"
            if Setting.get(_FLAG_BF):
                return
            Setting.set(_FLAG_BF, "1")
            db.session.commit()
            return
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
    # Las bajas son recuperables: los archivos deben seguir disponibles.
    return

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

    def _enviar_push(titulo, cuerpo, url="/admin/"):
        """Aviso push al celu del admin (venta MP / reserva web). La implementacion
        esta en create_app; antes se la llamaba directo desde aca y daba NameError
        (silenciado), asi que los avisos nunca salian."""
        f = getattr(app, "enviar_push", None)
        if f:
            f(titulo, cuerpo, url)

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
    def home_admin():
        """Home del admin (v5.60): donde abre la web. Tareas con deadline, Constancia,
        KPIs principales y estado de la caja."""
        import metrics as _m
        kpis = {}
        try:
            kpis["mes"] = _m.kpis_mes_actual()
        except Exception:
            db.session.rollback(); kpis["mes"] = None
        try:
            kpis["conta"] = _m.contabilidad("mes_actual")
        except Exception:
            db.session.rollback(); kpis["conta"] = None
        try:
            kpis["stock"] = _m.kpis_principales()
        except Exception:
            db.session.rollback(); kpis["stock"] = None
        try:
            kpis["cobrar"] = _m.dinero_por_cobrar()
        except Exception:
            db.session.rollback(); kpis["cobrar"] = None
        try:
            _sincronizar_caja()   # igual que la pagina de Caja: trae lo nuevo (silencioso)
            caja_res = _resumen_caja()
        except Exception:
            db.session.rollback(); caja_res = None
        return render_template(
            "home_admin.html", kpis=kpis, caja=caja_res,
            tareas_dash=_tareas_dash(), constancia=_constancia_dash(),
            hoy=today_uy(),
        )

    @app.route("/admin/stock")
    @auth.admin_required
    def dashboard():
        _res = query_discs(request.args)
        discs = _res if isinstance(_res, list) else _res.all()
        totals = compute_totals(discs)  # totales sobre TODO lo filtrado
        discs = visible_records(discs, 'discs')
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

        # Relaciones solo de la pagina renderizada; evita una consulta por disco.
        if discs:
            Disc.query.options(selectinload(Disc.tracks)).filter(Disc.id.in_([d.id for d in discs])).all()

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
                formato="Vinyl",  # tienda de vinilos: siempre por defecto
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

    def _limpiar_samples_huerfanos(max_horas=24):
        """Borra samples que ningun Track referencia (archivos que quedan de altas de
        video abandonadas). Con max_horas>0 solo toca los mas viejos que eso (sesiones
        en curso quedan a salvo); con max_horas=0 borra TODOS los huerfanos (limpieza
        manual para liberar espacio). Devuelve (cantidad, bytes_liberados). No escribe
        en la base: solo borra archivos, asi funciona aunque el disco este lleno."""
        import time
        borrados, bytes_libres = 0, 0
        samples_dir = app.config.get("SAMPLES_DIR")
        if not samples_dir or not os.path.isdir(samples_dir):
            return 0, 0
        limite = time.time() - max_horas * 3600
        from operation_history import retained_samples
        protegidos = retained_samples()
        for fname in os.listdir(samples_dir):
            if fname in protegidos:
                continue
            path = os.path.join(samples_dir, fname)
            try:
                if not os.path.isfile(path):
                    continue
                if max_horas and os.path.getmtime(path) > limite:
                    continue  # muy reciente: puede ser una sesion en curso
                if Track.query.filter_by(sample_local=fname).first() is not None:
                    continue  # en uso
                sz = os.path.getsize(path)
                os.remove(path)
                borrados += 1
                bytes_libres += sz
            except Exception:
                pass
        return borrados, bytes_libres

    def _sweep_samples_huerfanos(max_horas=24):
        """Limpieza automatica diaria de samples huerfanos. Best-effort: cualquier error
        se ignora, y si la base falla (ej: disco lleno) se hace rollback para no dejar la
        sesion rota (evita que la pagina explote despues). Libera espacio ANTES de escribir
        la marca del dia, asi tambien ayuda a recuperarse de un disco lleno."""
        try:
            if Setting.get("last_sweep_samples") == today_uy().isoformat():
                return
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass
            return
        # Borrar huerfanos primero (libera espacio; no toca la base)
        try:
            _limpiar_samples_huerfanos(max_horas)
        except Exception:
            pass
        # Recien ahora marcar el dia (si el disco estaba lleno, ya liberamos algo)
        try:
            Setting.set("last_sweep_samples", today_uy().isoformat())
        except Exception:
            try:
                db.session.rollback()
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

    # ------------------------------------------------------------------
    # ALTA COMBINADA: URL (Deejay.de) + Video(s). Reemplaza "Desde URL" y
    # "Desde video": podés usar uno, el otro, o los dos. La URL da la
    # estructura (datos + tracklist + portada); los videos dan los samples,
    # que se emparejan con cada track por el nombre.
    # ------------------------------------------------------------------
    def _procesar_video_file(f, samples_dir):
        """Procesa UN video: lo guarda como sample, saca frames y lee la placa con IA.
        Devuelve dict {ok, track, disc_fields, cost_usd, error}."""
        import video as _video
        import uuid as _uuid
        out = {"ok": False, "track": None, "disc_fields": {}, "cost_usd": 0.0, "error": None}
        mtype = (f.mimetype or "").lower()
        if mtype not in _video.VIDEO_EXT_MAP:
            out["error"] = f"{f.filename}: tipo no soportado ({mtype})"
            return out
        try:
            f.stream.seek(0, os.SEEK_END)
            fsize = f.stream.tell()
            f.stream.seek(0)
        except (OSError, ValueError):
            fsize = 0
        if fsize and fsize > _video.MAX_VIDEO_BYTES:
            out["error"] = f"{f.filename}: pesa demasiado ({fsize // 1024 // 1024} MB, max 50 MB)"
            return out
        ext = _video.VIDEO_EXT_MAP[mtype]
        fname = f"sample_{_uuid.uuid4().hex[:12]}{ext}"
        dest_path = os.path.join(samples_dir, fname)
        try:
            f.save(dest_path)
        except Exception as e:
            out["error"] = f"{f.filename}: no se pudo guardar ({e})"
            return out
        fn_artista, fn_track = _video.split_artist_track(
            _video.clean_track_name_from_filename(f.filename))
        track_artista, track_titulo = fn_artista, fn_track
        frames, ferr = _video.extract_frames_from_path(dest_path)
        if ferr:
            out["error"] = f"{f.filename}: {ferr}"
        else:
            res = vision.extract_from_video_frames(frames, filename=f.filename)
            out["cost_usd"] = res.cost_usd or 0.0
            if res.ok:
                d = res.data
                for k in ("sello", "anio", "genero", "catalogo", "precio_uyu"):
                    if d.get(k):
                        out["disc_fields"][k] = d.get(k)
                if d.get("artista"):
                    track_artista = d.get("artista")
                if d.get("track"):
                    track_titulo = d.get("track")
            else:
                out["error"] = f"{f.filename}: {res.error}"
        # Guardar SOLO el audio como sample (la ficha lo reproduce como audio; nadie ve
        # el video). Pesa ~10% del video. Si no se puede sacar el audio, queda el video.
        sample_final = fname
        try:
            audio_fname = f"sample_{_uuid.uuid4().hex[:12]}.m4a"
            audio_path = os.path.join(samples_dir, audio_fname)
            ok_audio, _aerr = _video.extract_audio_from_path(dest_path, audio_path)
            if ok_audio:
                sample_final = audio_fname
                try:
                    os.remove(dest_path)  # ya tenemos el audio; el video se descarta
                except Exception:
                    pass
            else:
                try:
                    if os.path.exists(audio_path):
                        os.remove(audio_path)
                except Exception:
                    pass
        except Exception:
            sample_final = fname
        out["ok"] = True
        out["track"] = {"artista": track_artista or "", "titulo": track_titulo or "",
                        "sample_local": sample_final, "archivo": f.filename}
        return out

    def _norm_nombre(s):
        """Normaliza un nombre de track para comparar (sin acentos, minusculas, solo alfanum)."""
        import unicodedata as _ud
        import re as _re
        if not s:
            return ""
        s = _ud.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
        s = s.lower()
        s = _re.sub(r"[^a-z0-9]+", " ", s)
        return s.strip()

    def _emparejar_videos_tracks(tracks, videos, umbral=0.55):
        """Asigna a cada track (del tracklist de la URL) el video cuyo nombre mas se
        parece. Cada video se usa una sola vez. Devuelve (tracks, set de indices usados)."""
        from difflib import SequenceMatcher
        usados = set()
        for tr in tracks:
            obj_t = _norm_nombre(tr.get("titulo"))
            obj_f = _norm_nombre(f"{tr.get('artista', '')} {tr.get('titulo', '')}")
            mejor_i, mejor = None, 0.0
            for i, v in enumerate(videos):
                if i in usados:
                    continue
                vt = _norm_nombre(v.get("titulo"))
                vf = _norm_nombre(f"{v.get('artista', '')} {v.get('titulo', '')}")
                score = max(
                    SequenceMatcher(None, obj_t, vt).ratio() if (obj_t and vt) else 0.0,
                    SequenceMatcher(None, obj_f, vf).ratio() if (obj_f and vf) else 0.0,
                    0.9 if (obj_t and vt and (obj_t in vt or vt in obj_t)) else 0.0,
                )
                if score > mejor:
                    mejor, mejor_i = score, i
            if mejor_i is not None and mejor >= umbral:
                tr["sample_local"] = videos[mejor_i]["sample_local"]
                tr["sample_archivo"] = videos[mejor_i]["archivo"]
                usados.add(mejor_i)
        return tracks, usados

    @app.route("/admin/discs/new/combo", methods=["GET", "POST"])
    @auth.admin_required
    def disc_new_combo():
        """Alta combinada: URL de Deejay.de y/o videos. La URL trae los datos y el
        tracklist; los videos traen los samples, emparejados por nombre con cada track."""
        api_key_set = bool(config.ANTHROPIC_API_KEY)
        _sweep_samples_huerfanos()
        if request.method == "POST":
            urls_raw = (request.form.get("url") or "").strip()
            urls = []
            for _linea in urls_raw.splitlines():
                _u = _linea.strip()
                if _u and _u not in urls:
                    urls.append(_u)
            files = [f for f in request.files.getlist("videos") if f and f.filename]
            ss_file = request.files.get("screenshot")
            _hay_ss = bool(ss_file and ss_file.filename)
            if not urls and not files and not _hay_ss:
                flash("Pegá un link de Deejay.de, subí un screenshot, subí videos, o combinalos.", "error")
                return redirect(url_for("disc_new_combo"))
            # Varios links: van por el flujo por lotes de siempre (a la ficha, de a uno).
            # Los videos no se pueden repartir entre varios discos, asi que ahi se ignoran.
            if len(urls) > 1:
                if files:
                    flash("Pusiste varios links: los proceso de a uno (para sumar videos, cargá un disco por vez).", "warning")
                session["url_queue"] = urls[1:]
                session["url_queue_total"] = len(urls)
                session["url_queue_done"] = 0
                return _procesar_url_scrape(urls[0])
            url = urls[0] if urls else ""

            try:
                disc_fields = {}
                cover_local = None
                url_tracks = []
                errores = []
                source_url = None
                total_cost = 0.0

                # 1) URL (Deejay.de) -> datos + tracklist + portada
                if url:
                    result = scraper.scrape_url(url)
                    if result.ok:
                        data = dict(result.data)
                        source_url = data.get("source_url") or url
                        if data.get("cover_url"):
                            covers_dir = app.config["COVERS_DIR"]
                            try:
                                cl = scraper.download_cover(data["cover_url"], covers_dir)
                                if cl:
                                    cover_local = convertir_cover_a_webp(os.path.join(covers_dir, cl))
                            except Exception:
                                cover_local = None  # la portada la agrega despues; no frenar por eso
                        for k in ("artista", "titulo", "sello", "anio", "genero", "formato", "costo_eur"):
                            if data.get(k) not in (None, ""):
                                disc_fields[k] = data.get(k)
                        if data.get("tracklist"):
                            disc_fields["tracklist"] = data.get("tracklist")
                            url_tracks = parse_tracklist_text(data.get("tracklist"))
                    else:
                        errores.append(f"URL: {result.error}")
                        if not files:
                            session["scrape_failed_url"] = url
                            flash(f"No se pudo leer la URL: {result.error}", "error")
                            return redirect(url_for("disc_new_combo"))
                        flash(f"No se pudo leer la URL ({result.error}). Sigo con los videos.", "warning")

                # 2) Screenshot (opcional) -> completa lo que la URL no trajo (o TODO si
                #    la URL fallo/no vino). Da datos + tracklist, igual que la URL.
                if _hay_ss:
                    if not api_key_set:
                        flash("Sin API key no puedo leer el screenshot (el link y los videos igual funcionan).", "warning")
                    else:
                        try:
                            ssres = vision.extract_disc_from_image(ss_file.read(), (ss_file.mimetype or "").lower())
                            total_cost += ssres.cost_usd or 0.0
                            if ssres.ok:
                                ssd = ssres.data or {}
                                for k in ("artista", "titulo", "sello", "anio", "genero",
                                          "formato", "pais", "catalogo", "precio_uyu"):
                                    if not disc_fields.get(k) and ssd.get(k) not in (None, ""):
                                        disc_fields[k] = ssd.get(k)
                                if not url_tracks and ssd.get("tracklist"):
                                    disc_fields["tracklist"] = ssd.get("tracklist")
                                    url_tracks = parse_tracklist_text(ssd.get("tracklist"))
                            else:
                                errores.append(f"Screenshot: {ssres.error}")
                        except Exception as _e:
                            errores.append(f"Screenshot: {_e}")

                # 3) Videos -> samples + datos de respaldo
                videos = []
                if files:
                    if not api_key_set:
                        flash("La API key de Anthropic no esta configurada; no puedo leer los videos.", "error")
                        return redirect(url_for("disc_new_combo"))
                    samples_dir = app.config["SAMPLES_DIR"]
                    os.makedirs(samples_dir, exist_ok=True)
                    for f in files:
                        r = _procesar_video_file(f, samples_dir)
                        total_cost += r.get("cost_usd") or 0.0
                        if r.get("error"):
                            errores.append(r["error"])
                        if r.get("track"):
                            videos.append(r["track"])
                            for k, v in (r.get("disc_fields") or {}).items():
                                if not disc_fields.get(k) and v:
                                    disc_fields[k] = v
                if total_cost > 0:
                    _track_api_cost(total_cost)
                    db.session.commit()

                # 3) Definir los tracks y emparejar los videos
                if url_tracks:
                    tracks = url_tracks
                    tracks, usados = _emparejar_videos_tracks(tracks, videos)
                    sin_asignar = [v for i, v in enumerate(videos) if i not in usados]
                else:
                    # Sin tracklist de URL: cada video es un track (flujo video clasico)
                    tracks = [{
                        "posicion": i, "lado": None,
                        "titulo": v.get("titulo"), "artista": v.get("artista"),
                        "sample_local": v.get("sample_local"), "sample_archivo": v.get("archivo"),
                    } for i, v in enumerate(videos)]
                    sin_asignar = []

                if not tracks and not disc_fields:
                    flash("No pude sacar datos ni de la URL ni de los videos. " + " · ".join(errores[:3]), "error")
                    return redirect(url_for("disc_new_combo"))

                # Artista del disco por defecto (si la URL no lo trajo)
                if not disc_fields.get("artista"):
                    arts = [v.get("artista") for v in videos if v.get("artista")]
                    if arts:
                        disc_fields["artista"] = arts[0] if len(set(arts)) == 1 else "Various"
                disc_fields.setdefault("formato", "Vinyl")

                # La sesion es una cookie: si el tracklist en texto es largo, no lo
                # guardamos ahi (se reconstruye de los tracks al crear); evita romper la cookie.
                _prefill_sesion = {k: v for k, v in disc_fields.items() if k != "tracklist"}
                session["combo_prefill"] = _prefill_sesion
                session["combo_tracks"] = tracks
                session["combo_videos"] = videos
                session["combo_cover"] = cover_local
                session["combo_info"] = {
                    "cost_usd": round(total_cost, 4),
                    "errores": errores,
                    "sin_asignar": [v.get("archivo") for v in sin_asignar],
                    "source_url": source_url,
                }
                _msg = "Listo. Revisá y creá la ficha."
                if total_cost:
                    _msg += f" Costo IA: ${total_cost:.4f} USD."
                flash(_msg, "success")
                return redirect(url_for("disc_combo_review"))
            except Exception as e:
                app.logger.exception("Error en alta combinada (URL + Video)")
                flash(f"Se rompió procesando: {type(e).__name__}: {e}", "error")
                return redirect(url_for("disc_new_combo"))

        return render_template(
            "disc_combo.html",
            api_key_set=api_key_set,
            failed_url=session.pop("scrape_failed_url", None),
        )

    @app.route("/admin/discs/new/combo/review", methods=["GET", "POST"])
    @auth.admin_required
    def disc_combo_review():
        prefill = session.get("combo_prefill") or {}
        tracks = session.get("combo_tracks") or []
        videos = session.get("combo_videos") or []
        cover_local = session.get("combo_cover")
        info = session.get("combo_info") or {}
        if not tracks and not prefill:
            flash("No hay datos para revisar. Empezá de nuevo.", "error")
            return redirect(url_for("disc_new_combo"))

        if request.method == "POST":
            def _s(k):
                v = (request.form.get(k) or "").strip()
                return v or None

            def _i(k):
                try:
                    return int(request.form.get(k))
                except (TypeError, ValueError):
                    return None

            def _f(k):
                try:
                    return round(float(str(request.form.get(k)).replace(",", ".")), 2)
                except (TypeError, ValueError):
                    return None

            titulo = _s("titulo") or "Sin título"
            disc = Disc(
                estado="Disponible", stock=1, origen="arcano",
                artista=_s("artista") or "Various", titulo=titulo, sello=_s("sello"),
                anio=_i("anio"), formato=_s("formato") or "Vinyl", genero=_s("genero"),
                tags=_s("catalogo"), precio_uyu=float(_i("precio_uyu") or 0),
                costo_eur=_f("costo_eur") or 0.0, envio_eur=0.0,
                cover_local=cover_local or None,
                fecha_ingreso=now_uy(),
            )
            try:
                disc.recalcular_costo_final(tc=Setting.get_tc())
            except Exception:
                pass
            db.session.add(disc)
            db.session.flush()
            db.session.add(StatusHistory(disc_id=disc.id, estado=disc.estado, nota="Ingreso al stock (URL + video)"))

            validos = {v["sample_local"] for v in videos}
            n = _i("track_count") or 0
            creados = 0
            tl_lines = []
            for idx in range(n):
                tit = (request.form.get(f"track_titulo_{idx}") or "").strip() or f"Track {idx + 1}"
                art = (request.form.get(f"track_artista_{idx}") or "").strip() or None
                lado = (request.form.get(f"track_lado_{idx}") or "").strip() or None
                sample = (request.form.get(f"track_sample_{idx}") or "").strip()
                sample = sample if sample in validos else None
                db.session.add(Track(
                    disc_id=disc.id, posicion=idx, lado=lado, titulo=tit[:300],
                    artista=art, sample_local=sample,
                ))
                creados += 1
                _pref = (f"{lado}. " if lado else f"{idx + 1}. ")
                tl_lines.append(_pref + (f"{art} - {tit}" if art else tit))
            if tl_lines:
                disc.tracklist = "\n".join(tl_lines)
            db.session.commit()
            for k in ("combo_prefill", "combo_tracks", "combo_videos", "combo_cover", "combo_info"):
                session.pop(k, None)
            flash(f"Ficha creada con {creados} track(s). Revisá la portada y los samples.", "success")
            return redirect(url_for("disc_edit", disc_id=disc.id) + "#tracks-section")

        return render_template(
            "disc_combo_review.html",
            prefill=prefill, tracks=tracks, videos=videos,
            cover_local=cover_local, info=info, tc=Setting.get_tc(),
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
                formato = _str_or_none(request.form.get(prefix + "formato")) or "Vinyl"
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
        # Se conserva el archivo para recuperar esta baja desde Historial.
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
            campos_bool = ("destacado", "en_nuevos", "cartel_ultima", "mostrar_agotado", "es_preorder", "es_tip", "en_fire_sale", "oculto_web")
            cambios = 0
            if campo in campos_bool:
                nuevo_val = valor in ("1", "true", "on", "si")
                for d in discs:
                    setattr(d, campo, nuevo_val)
                    cambios += 1
                db.session.commit()
                etiquetas = {
                    "destacado": "Destacado", "en_nuevos": "Nuevos ingresos", "es_tip": "Staff Pick",
                    "cartel_ultima": "Ultima copia", "mostrar_agotado": "Agotado visible",
                    "es_preorder": "Carrusel Pre-order", "en_fire_sale": "Fire Sale",
                    "oculto_web": "NO mostrar en la web",
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

        # Conservar el sample anterior para recuperacion desde Historial.
        samples_dir = app.config["SAMPLES_DIR"]
        os.makedirs(samples_dir, exist_ok=True)

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
                # Mismo camino que el resto del admin (register_status_change): al salir
                # de Vendido limpia la venta (fecha, copias, precio cobrado, comprador,
                # canal, metodo, grupo) y la sena si corresponde; al entrar a Vendido
                # fija fecha/copias/precio cobrado. Antes la edicion lo hacia a mano y
                # dejaba la venta vieja pegada.
                _nuevo_estado = disc.estado
                _cliente_encargo = disc.cliente_id if _nuevo_estado == "Encargo" else None
                disc.estado = estado_anterior
                register_status_change(disc, _nuevo_estado,
                                       nota=f"Cambio desde {estado_anterior} (edicion)")
                if _nuevo_estado == "Encargo" and _cliente_encargo:
                    disc.cliente_id = _cliente_encargo

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
        # La copia es OTRA copia fisica: arranca Disponible, con stock 1 y sin NADA de la
        # venta/reserva del original (comprador, canal, metodo, grupo, sena, precio
        # cobrado). Antes copiaba todo: la copia de un disco vendido nacia atada al
        # comprador anterior y con metodo MP, y una sena cobrada aparecia DOS veces en caja.
        nuevo.estado = "Disponible"
        nuevo.stock = 1
        nuevo.copias_vendidas = 0
        nuevo.fecha_venta = None
        nuevo.fecha_reserva = None
        nuevo.precio_venta_uyu = None
        nuevo.cliente_id = None
        nuevo.canal_venta = None
        nuevo.metodo_pago_venta = None
        nuevo.venta_grupo = None
        nuevo.sena_cobrada = False
        nuevo.sena_monto_uyu = None
        nuevo.sena_fecha = None
        nuevo.sena_metodo = None
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
            # Formato uruguayo: "1.800" = mil ochocientos (punto de miles). Antes se leia
            # como 1,8 y el disco quedaba a $1 en la tienda.
            try:
                import math as _math
                _txt = str(valor if valor is not None else "").strip()
                # Solo numeros (con punto/coma de miles o decimales): "abc" o vacio no
                # pueden terminar guardando el disco a $0.
                if not _re_email.fullmatch(r"\$?\s*\d[\d.,\s]*", _txt):
                    raise ValueError()
                _p = _parse_monto_caja(_txt)
                if not _math.isfinite(_p) or _p < 0 or _p > 10_000_000:
                    raise ValueError()
                nuevo = int(round(_p))
            except (ValueError, TypeError, OverflowError):
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
                nuevo = int(str(valor).strip())
                if nuevo < 0 or nuevo > 1000:
                    raise ValueError()
            except (ValueError, TypeError, OverflowError):
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

        elif campo == "sena_cobrar":
            monto = _parse_monto_caja(valor)
            met = (data.get("metodo") or "").strip() or None if hasattr(data, "get") else None
            ok, msg = _aplicar_sena(disc, "cobrar", monto=monto, metodo=met)
            if not ok:
                return jsonify({"ok": False, "error": msg}), 400
            db.session.commit()
            return jsonify({"ok": True, "campo": campo, "sena_cobrada": True,
                            "sena_monto": round(disc.sena_monto_uyu or 0),
                            "falta": round(disc.precio_falta_uyu or 0)})

        elif campo == "sena_resolver":
            accion = (valor or "").strip()
            if accion not in ("devolver", "quedar"):
                return jsonify({"ok": False, "error": "Accion invalida"}), 400
            ok, msg = _aplicar_sena(disc, accion)
            if not ok:
                return jsonify({"ok": False, "error": msg}), 400
            db.session.commit()
            return jsonify({"ok": True, "campo": campo, "sena_cobrada": False})

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
            invalidar_ajuste_tc()
            return jsonify({
                "ok": True,
                "campo": campo,
                "valor": nuevo,
                "costo_final_uyu": round(disc.costo_real_uyu),
                "ajuste_tc_uyu": round(disc.ajuste_tc_uyu),
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

        elif campo in ("destacado", "en_nuevos", "cartel_ultima", "mostrar_agotado", "es_preorder", "es_tip", "en_fire_sale", "oculto_web"):
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
                "costo_final_uyu": round(disc.costo_real_uyu),
                "ajuste_tc_uyu": round(disc.ajuste_tc_uyu),
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
        for d in Disc.query.options(selectinload(Disc.tracks)).filter(Disc.estado != "Descartado").all():
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
             ("Disponible", "Pre-order", "Encargo", "Reservado", "Apartado para mi")],
            key=lambda f: f["interes"], reverse=True,
        )
        vendidos = sorted(
            [f for f in con_escuchas if f["disc"].estado == "Vendido"],
            key=lambda f: f["interes"], reverse=True,
        )

        # --- Búsquedas de la tienda (últimos 30 días): qué busca la gente ---
        # Radar unificado: además de las escuchas, sumamos lo que se busca. Lo que se
        # busca y NO se encuentra (resultados=0) es "demanda no satisfecha".
        from models import SearchQuery
        from datetime import datetime as _dtb, timedelta as _tdb
        _cut = _dtb.utcnow() - _tdb(days=30)
        _agg = {}
        _total_busq = 0
        try:
            for r in SearchQuery.query.filter(SearchQuery.fecha >= _cut).all():
                t = (r.termino or "").strip().lower()
                if not t:
                    continue
                _total_busq += 1
                a = _agg.setdefault(t, {"termino": t, "veces": 0, "sin_resultado": 0})
                a["veces"] += 1
                if (r.resultados or 0) == 0:
                    a["sin_resultado"] += 1
        except Exception:
            _agg = {}
        _all_busq = list(_agg.values())
        # Demanda no satisfecha: las que alguna vez dieron 0, ordenadas por cuántas veces
        sin_resultado = sorted(
            [a for a in _all_busq if a["sin_resultado"] > 0],
            key=lambda a: (a["sin_resultado"], a["veces"]), reverse=True,
        )[:25]
        # Lo más buscado en general (con o sin resultado)
        top_busquedas = sorted(_all_busq, key=lambda a: a["veces"], reverse=True)[:25]

        return render_template(
            "radar.html",
            disponibles=disponibles,
            vendidos=vendidos,
            con_escuchas=len(con_escuchas),
            sin_escuchas=len(filas) - len(con_escuchas),
            total_discos=len(filas),
            sin_resultado=sin_resultado,
            top_busquedas=top_busquedas,
            busquedas_total=_total_busq,
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
            for r in Reserva.query.options(selectinload(Reserva.items)).all():
                # NO derivar de 'pagado' (Pagada != Mercado Pago; puede ser transferencia
                # o efectivo coordinado por privado).
                met = ("mercadopago" if (r.mp_status == "approved" or r.metodo_pago == "mercadopago")
                       else ("transferencia" if r.metodo_pago == "transferencia"
                       else ("efectivo" if r.metodo_pago == "efectivo" else None)))
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

        # #5: ventas manuales atribuidas a un cliente (disc.cliente_id) que no vinieron de
        # una reserva web -> que se muestren y se busquen por su nombre igual.
        _cids = {d.cliente_id for d in vendidos if getattr(d, "cliente_id", None) and d.id not in cliente_res}
        if _cids:
            from models import Cliente as _Cli
            _cmap = {c.id: c.nombre for c in _Cli.query.filter(_Cli.id.in_(_cids)).all()}
            for d in vendidos:
                if d.id not in cliente_res and getattr(d, "cliente_id", None) in _cmap:
                    cliente_res[d.id] = {"nombre": _cmap[d.cliente_id], "reserva_id": None}

        # Filtro (canal / metodo / buscador). Con filtro activo, los numeros de arriba se
        # calculan sobre lo FILTRADO para que coincidan con la lista (#9).
        _filtrando = bool(f_canal or f_metodo or f_q)
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
        base = lista if _filtrando else vendidos

        kpis = metrics.kpis_de_discos(base)
        if _filtrando:
            comp = {k: None for k in ("ventas", "facturado", "ganancia")}
        else:
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
            for d in base:
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

        # Tendencia: facturado del PERIODO ELEGIDO (antes eran siempre los ultimos 30
        # dias, aunque eligieras "Todo" o "Este ano"). Segun el largo del periodo las
        # barras son por dia (hasta 45 dias), por semana (hasta ~6 meses) o por mes.
        # Misma cuenta que el KPI "Facturado": la suma de las barras da ese numero.
        _MESES_C = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
        _MESES_L = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
                    "setiembre", "octubre", "noviembre", "diciembre"]
        t_ini = inicio_dt.date()
        t_fin = min(fin_dt.date(), today_uy() + _td(days=1))      # sin dibujar el futuro
        _fechas_v = [d.fecha_venta.date() for d in base if d.fecha_venta]
        if periodo == "todo" or (t_fin - t_ini).days > 1500:
            # "Todo" arranca en un tope tecnico (ano 2000): empezar en la primera venta
            t_ini = min(_fechas_v) if _fechas_v else max(t_ini, t_fin - _td(days=30))
        if t_fin <= t_ini:
            t_fin = t_ini + _td(days=1)
        _span = (t_fin - t_ini).days
        if _span <= 45:
            trend_unidad = "dia"
            _cubos = [t_ini + _td(days=i) for i in range(_span)]
            _clave = lambda f: f
            _etq = lambda c: (c.strftime("%d/%m"), c.strftime("%d/%m/%Y"))
        elif _span <= 190:
            trend_unidad = "semana"
            _lunes0 = t_ini - _td(days=t_ini.weekday())
            _cubos = []
            _c = _lunes0
            while _c < t_fin:
                _cubos.append(_c)
                _c += _td(days=7)
            _clave = lambda f: f - _td(days=f.weekday())
            _etq = lambda c: (c.strftime("%d/%m"), "semana del " + c.strftime("%d/%m/%Y"))
        else:
            trend_unidad = "mes"
            _cubos = []
            _y, _m = t_ini.year, t_ini.month
            while (_y, _m) < (t_fin.year, t_fin.month) or (
                    (_y, _m) == (t_fin.year, t_fin.month) and t_fin.day > 1):
                _cubos.append(t_ini.replace(year=_y, month=_m, day=1))
                _m += 1
                if _m > 12:
                    _m, _y = 1, _y + 1
            _clave = lambda f: f.replace(day=1)
            _etq = lambda c: (_MESES_C[c.month - 1] + " " + c.strftime("%y"),
                              _MESES_L[c.month - 1] + " " + str(c.year))
        tmap = {c: 0.0 for c in _cubos}
        for d in base:
            fv = d.fecha_venta.date() if d.fecha_venta else None
            if fv is None:
                continue
            k = _clave(fv)
            if k in tmap:
                tmap[k] += (d.precio_mostrar or d.precio_uyu or 0) * d.copias_facturadas
        trend = [{"label": _etq(c)[0], "titulo": _etq(c)[1], "monto": round(tmap[c])} for c in _cubos]
        trend_max = max((t["monto"] for t in trend), default=0) or 1

        def _top(attr):
            acc = {}
            for d in base:
                k = (getattr(d, attr) or "").strip()
                if k:
                    acc[k] = acc.get(k, 0) + (d.precio_mostrar or d.precio_uyu or 0) * d.copias_facturadas
            if not acc:
                return None
            k = max(acc, key=acc.get)
            return {"nombre": k, "monto": round(acc[k])}
        top_sello, top_artista = _top("sello"), _top("artista")

        if request.args.get("export") == "csv":
            return _ventas_csv(lista, canal_de, metodo_de, cliente_res)
        lista = visible_records(lista, 'discs')

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
            trend_unidad=trend_unidad,
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
                round(d.ganancia_negocio_uyu),
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

    def _desvender_disco(d, nota="Venta eliminada"):
        """Revierte una venta: el disco vuelve a Disponible y se limpian TODOS sus
        datos de venta (comprador, canal, metodo, grupo). La caja, la contabilidad,
        el panel de ventas y la barra de lealtad son derivados del estado 'Vendido',
        asi que se limpian solos al des-vender."""
        if d is None:
            return
        # register_status_change limpia fecha_venta/copias/precio_venta y, al salir de
        # Vendido, comprador/canal/metodo/grupo y repone el stock.
        register_status_change(d, "Disponible", nota=nota)
        d.cliente_id = None
        d.canal_venta = None
        d.metodo_pago_venta = None
        d.venta_grupo = None
        if (d.stock or 0) <= 0:
            d.stock = 1

    def _reparto_box(reserva):
        """{disc_id: parte del precio} para los discos asignados a una Mystery Box de
        esta reserva (items sin precio propio). El precio de la caja se reparte entre
        ellos para que la contabilidad lo cuente UNA vez, con el costo real de cada disco."""
        items = reserva.items_efectivos
        box_total = sum((it.precio_uyu_snap or 0) for it in items if getattr(it, "es_mystery_box", False))
        asignados = [it.disc_id for it in items
                     if it.disc_id and not getattr(it, "es_mystery_box", False)
                     and not (it.precio_uyu_snap or 0)]
        if box_total <= 0 or not asignados:
            return {}
        n = len(asignados)
        base = round(box_total / n)
        shares = [base] * n
        shares[0] += round(box_total - base * n)   # el remanente al primero
        return dict(zip(asignados, shares))

    def _apartar_para_pedido(reserva, discos, motivo):
        """Aparta los discos de un pedido web que todavia no se pago (transferencia con
        comprobante o "reservar y coordinar"): los Disponibles pasan a Reservado (nota
        "Reserva #id", asi cancelar/borrar el pedido los libera) y los Pre-order quedan
        tomados por el pedido (venta_grupo "r<id>"), asi nadie mas los compra.

        El apartado es ATOMICO en la base (UPDATE ... WHERE estado='Disponible'): si dos
        clientes confirman el mismo disco a la vez (el servidor corre en 2 procesos), solo
        uno lo gana. Devuelve la lista de discos que NO se pudieron apartar porque otro
        pedido se los llevo en el medio (el que llama los saca del pedido)."""
        from sqlalchemy import update as _upd
        perdidos = []
        grupo = f"r{reserva.id}"
        for d in discos:
            if d.estado == "Disponible":
                res = db.session.execute(
                    _upd(Disc).where(Disc.id == d.id, Disc.estado == "Disponible")
                    .values(estado="Reservado")
                    .execution_options(synchronize_session=False))
                if res.rowcount != 1:
                    perdidos.append(d)
                    continue
                register_status_change(d, "Reservado", nota=f"Reserva #{reserva.id} - {motivo}")
            elif d.estado in ("Pre-order", "Encargo") and not d.venta_grupo:
                res = db.session.execute(
                    _upd(Disc).where(Disc.id == d.id, Disc.venta_grupo.is_(None))
                    .values(venta_grupo=grupo)
                    .execution_options(synchronize_session=False))
                if res.rowcount != 1:
                    perdidos.append(d)
                    continue
                d.venta_grupo = grupo
        return perdidos

    def _disco_es_de_reserva(d, reserva):
        """True si el estado actual del disco (Vendido / Reservado) se lo puso ESTA
        reserva. Sirve para que borrar una reserva (o eliminar una venta) solo deshaga
        lo suyo: un pedido abandonado que tenia el disco NO puede des-vender una venta
        que despues se hizo por Instagram u otra reserva.
        - Vendido: su venta es de esta reserva si venta_grupo == r<id>. Ventas viejas sin
          grupo: si el historial dice que la vendio esta reserva, o es el mismo cliente.
        - Reservado: si el ultimo "Reservado" del historial lo puso esta reserva (todas
          las notas llevan "#<id>": manual, transferencia, cambio de estado)."""
        import re as _re
        if d is None or reserva is None:
            return False
        _patron = _re.compile(r"#%d(?!\d)" % reserva.id)

        def _ultimo(estado):
            for h in (d.history or []):   # ordenado del mas nuevo al mas viejo
                if h.estado == estado:
                    return h
            return None

        if d.estado == "Vendido":
            if d.venta_grupo:
                return d.venta_grupo == f"r{reserva.id}"
            h = _ultimo("Vendido")
            if h and h.nota and _patron.search(h.nota):
                return True
            return bool(reserva.cliente_id) and d.cliente_id == reserva.cliente_id
        if d.estado == "Reservado":
            h = _ultimo("Reservado")
            return bool(h and h.nota and _patron.search(h.nota))
        if d.estado in ("Pre-order", "Encargo"):
            return d.venta_grupo == f"r{reserva.id}"
        return False

    @app.route("/admin/ventas/eliminar", methods=["POST"])
    @auth.admin_required
    def ventas_eliminar():
        """Elimina una venta por completo: cada disco vuelve a Disponible y desaparece
        de ventas, contabilidad, caja y lealtad (todo se recalcula del estado Vendido).
        Si la venta vino de un pedido web (reserva), tambien se borra ese pedido."""
        from models import Reserva as _Reserva, ReservaItem as _RI
        ids = {int(x) for x in (request.form.get("ids") or "").split(",") if x.strip().isdigit()}
        if not ids:
            flash("No se indicaron discos para eliminar la venta.", "warning")
            return redirect(url_for("ventas_panel"))
        # Pedidos web (reservas) que tocan alguno de estos discos (por items o por el
        # disc_id directo de reservas viejas).
        reserva_ids = {it.reserva_id for it in _RI.query.filter(_RI.disc_id.in_(ids)).all()}
        reserva_ids |= {r.id for r in _Reserva.query.filter(_Reserva.disc_id.in_(ids)).all()}
        candidatas = _Reserva.query.filter(_Reserva.id.in_(reserva_ids)).all() if reserva_ids else []
        elegidos = Disc.query.filter(Disc.id.in_(ids)).all() if ids else []
        # Solo se borra el pedido web que ES esta venta (algun disco elegido lo vendio esa
        # reserva). Un pedido abandonado o de otra persona que tambien tenia el disco NO
        # se toca. Antes se borraban todos y se des-vendian sus discos: eliminar la venta
        # de Juan borraba tambien la de Pedro si compartian un pedido viejo.
        reservas = [r for r in candidatas if any(_disco_es_de_reserva(d, r) for d in elegidos)]
        # De esos pedidos, se des-venden solo los discos que vendio/aparto ese pedido.
        for r in reservas:
            for it in r.items_efectivos:
                if it.disc_id and _disco_es_de_reserva(it.disco, r):
                    ids.add(it.disc_id)
        discs = Disc.query.filter(Disc.id.in_(ids)).all() if ids else []
        for d in discs:
            _desvender_disco(d, nota="Venta eliminada desde el panel")
        for r in reservas:
            db.session.delete(r)
        db.session.commit()
        msg = f"Venta eliminada. {len(discs)} disco(s) volvieron a Disponible."
        if reservas:
            msg += f" Se borró {len(reservas)} pedido(s) web del cliente."
        flash(msg, "success")
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
            .filter(Disc.estado.in_(["Disponible", "Reservado", "Pre-order", "Encargo", "Apartado para mi"]))
            .filter(Disc.es_mystery_box.isnot(True))   # la box no se vende como disco
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
            egresos=visible_records(egresos, 'egresos'),
            retiros=visible_records(retiros, 'egresos'),
            recurrentes=visible_records(recurrentes, 'egresos_recurrentes'),
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
        raw_in = (request.form.get("monto_uyu") or "").strip()
        if raw_in == "":
            # Recalcular desde la moneda original * tc (si es UYU, queda igual)
            if e.moneda == "UYU":
                e.monto_uyu = e.monto
            else:
                e.monto_uyu = round((e.monto or 0) * (e.tc_usado or 1), 2)
            db.session.commit()
            flash("Monto recalculado con el tipo de cambio.", "success")
        else:
            # Mismo parser que la Caja (punto = miles, coma = decimal, es-UY)
            val = _parse_monto_caja(raw_in)
            if val < 0 or (val == 0 and not any(ch.isdigit() for ch in raw_in)):
                flash("Monto invalido.", "error")
            else:
                e.monto_uyu = round(val, 2)
                db.session.commit()
                flash(f"Monto en pesos actualizado: {val:,.0f} UYU.".replace(",", "."), "success")
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
        _cambio_base = (abs((monto or 0) - (e.monto or 0)) > 0.004) or (moneda != e.moneda)
        _uyu_viejo = e.monto_uyu or 0
        e.monto = monto
        e.moneda = moneda

        # Monto en pesos: si viene un valor, se respeta; si viene vacio, se recalcula.
        # Mismo parser que la Caja (punto = miles, coma = decimal, es-UY).
        # Si cambiaste el monto o la moneda y NO tocaste "En pesos" (el form lo manda
        # lleno con el valor viejo), se recalcula: antes quedaba el valor en pesos viejo.
        raw_in = (request.form.get("monto_uyu") or "").strip()
        _uyu_sin_tocar = False
        if raw_in and any(ch.isdigit() for ch in raw_in):
            _uyu_sin_tocar = abs(_parse_monto_caja(raw_in) - round(_uyu_viejo)) < 1
        if raw_in and any(ch.isdigit() for ch in raw_in) and not (_cambio_base and _uyu_sin_tocar):
            val = _parse_monto_caja(raw_in)
            if val >= 0:
                e.monto_uyu = round(val, 2)
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
            for r in Reserva.query.options(selectinload(Reserva.items)).all():
                met = None
                # NO derivar de 'pagado': marcar una reserva Pagada NO implica Mercado
                # Pago. Muchas reservas web se cobran coordinando por privado
                # (transferencia/efectivo). Solo es MP si el metodo real es MP o MP la
                # aprobo; si no, queda 'sin especificar' (plata en la mano, no retenida).
                if (r.mp_status == "approved") or (r.metodo_pago == "mercadopago"):
                    met = "mercadopago"
                elif r.metodo_pago == "transferencia":
                    met = "transferencia"
                elif r.metodo_pago == "efectivo":
                    met = "efectivo"
                # 'coordinar'/vacio/'otro' -> lo deja sin especificar para que Facundo lo defina
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
            _bruto = (d.precio_mostrar or d.precio_uyu or 0) * d.copias_facturadas
            _sena_v = d.sena_efectiva  # sena ya cobrada por adelantado (0 si no hubo)
            monto = round(max(0.0, _bruto - _sena_v), 2)
            concepto = (f"Venta: {d.artista or ''} - {d.titulo or ''}".strip(" -") or "Venta")[:300]
            if _sena_v > 0:
                _sfmt = f"{_sena_v:,.0f}".replace(",", ".")
                concepto = (concepto + f" (neto de sena $ {_sfmt})")[:300]
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
                # sincronizados en ambos sentidos. Si el disco no implica metodo (venta
                # coordinada por fuera), la caja queda 'sin especificar' en vez de quedar
                # pegada a un metodo viejo (ej: un Mercado Pago fantasma que restaba de
                # 'plata en la mano').
                if mov.metodo != metodo:
                    mov.metodo = metodo
                    actualizados += 1

        # --- SENAS (ingresos): sena de reserva cobrada por adelantado ---
        # Plata real que entro al reservar (el cliente pago para apartar el disco).
        # Cuenta aunque el disco siga Reservado. Al venderse, la VENTA se cuenta NETA
        # de esta sena (arriba) para no contar la misma plata dos veces.
        for d in Disc.query.filter(Disc.sena_cobrada.is_(True)).all():
            monto = round(d.sena_monto_uyu or 0, 2)
            if monto <= 0:
                continue
            key = ("sena", d.id)
            vigentes.add(key)
            concepto = (f"Sena reserva: {d.artista or ''} - {d.titulo or ''}".strip(" -") or "Sena de reserva")[:300]
            if d.sena_fecha:
                fecha = d.sena_fecha
            elif d.fecha_reserva:
                fecha = d.fecha_reserva.date() if hasattr(d.fecha_reserva, "date") else d.fecha_reserva
            else:
                fecha = today_uy()
            metodo = d.sena_metodo or metodo_por_disc.get(d.id)
            mov = existentes.get(key)
            if mov is None:
                db.session.add(MovimientoCaja(
                    fecha=fecha, tipo="ingreso", monto_uyu=monto,
                    concepto=concepto, metodo=metodo, categoria="Sena",
                    origen="sena", origen_id=d.id,
                ))
                creados += 1
            else:
                if not mov.manual_override:
                    if (_difiere(mov.monto_uyu, monto) or mov.fecha != fecha
                            or mov.tipo != "ingreso" or mov.categoria != "Sena"):
                        mov.monto_uyu = monto
                        mov.fecha = fecha
                        mov.tipo = "ingreso"
                        mov.categoria = "Sena"
                        mov.concepto = concepto
                        actualizados += 1
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
        # Solo restan los adelantos de la MISMA ventana de 30 dias (son los que cubren
        # esas ventas). Antes restaban todos los adelantos de la historia, incluso los ya
        # saldados, y tapaban las ventas nuevas por MP ("En MP" quedaba en 0).
        adelanto_todos = sum(
            m.monto_uyu or 0 for m in movs
            if (m.adelanto_mp or "") and m.fecha and m.fecha > limite_mp
        )
        mp_pendiente = max(0.0, mp_bruto - adelanto_todos)
        # Adelantos de MP: aportes que Facundo hizo para tener la liquidez de MP
        # antes de que MP pague. Los 'pendiente' todavia esperan el pago de MP.
        adelantos_pendientes = [m for m in movs if (m.adelanto_mp or "") == "pendiente"]
        adelanto_pendiente_total = sum(m.monto_uyu or 0 for m in adelantos_pendientes)
        # Lo que hay en MP y todavia no adelantaste (para no adelantar dos veces). Los
        # adelantos ya estan descontados en mp_pendiente: restarlos otra vez era doble.
        por_adelantar = mp_pendiente
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
        # comprados y todavia no vendidos. Misma definicion que la contabilidad: TODO lo
        # que no esta Vendido (Disponible + Reservado + Apartado para mi) + Pre-order con
        # pedido asignado. No es plata en caja: es plata convertida en stock.
        mercaderia_discos = (
            Disc.query
            .filter(
                Disc.estado.in_(["Disponible", "Reservado", "Apartado para mi"])
                | ((Disc.estado.in_(["Pre-order", "Encargo"])) & (Disc.pedido_id.isnot(None)))
            )
            .filter((Disc.origen != "personal") | (Disc.origen.is_(None)))
            .filter(Disc.es_mystery_box.isnot(True))   # la box no es mercaderia real
            .all()
        )
        mercaderia_uyu = sum(d.costo_real_uyu * (d.stock or 1) for d in mercaderia_discos)
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
            elif m.origen == "sena":
                key, label, orden = "sena", "Senas de reserva", 0.6
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
            if m.origen == "sena":
                return "Senas de reserva cobradas"
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
        resumen["filas"] = [f for f in resumen["filas"] if f["mov"].id not in archived_ids("movimientos_caja")]
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
        # Retiro de capital (plata que Facundo saca para el): se guarda como "Retiro
        # personal", lo mismo que desde Contabilidad. NO es un gasto (no baja la ganancia):
        # baja el capital aportado y sale de la caja (la caja lo trae sola al sincronizar).
        if tipo == "egreso" and request.form.get("es_retiro"):
            from models import Egreso, CAT_RETIRO_PERSONAL
            db.session.add(Egreso(
                fecha=fecha, categoria=CAT_RETIRO_PERSONAL,
                descripcion=(concepto or "Retiro de capital")[:300],
                monto=round(monto, 2), moneda="UYU", tc_usado=1, monto_uyu=round(monto, 2),
            ))
            db.session.commit()
            _sincronizar_caja()
            db.session.commit()
            flash(f"Retiro de capital registrado: - ${monto:,.0f} UYU. Baja tu capital aportado "
                  "y la caja; no cuenta como gasto.", "success")
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
    def _opciones_proveedor():
        """Lista del desplegable de proveedor: los fijos + los que Facundo ya escribio
        a mano en pedidos anteriores (ej. 'Discogs', asi no lo tipea de nuevo) + 'Otro'
        al final, que abre el campo para escribir uno nuevo."""
        from models import Pedido, PROVEEDORES_PEDIDO
        base = [p for p in PROVEEDORES_PEDIDO if p != "Otro"]
        propios = sorted({(x[0] or "").strip() for x in db.session.query(Pedido.proveedor).distinct()
                          if (x[0] or "").strip() and (x[0] or "").strip() not in PROVEEDORES_PEDIDO},
                         key=str.lower)
        return base + propios + ["Otro"]

    def _proveedor_del_form(actual=None):
        """Proveedor elegido en el form. Si eligio 'Otro', se guarda el NOMBRE que
        escribio (ej. 'Discogs'); si no escribio nada, queda 'Otro'."""
        prov = (request.form.get("proveedor") or "").strip()
        if prov == "Otro":
            return ((request.form.get("proveedor_otro") or "").strip()[:40]) or "Otro"
        if prov in _opciones_proveedor():
            return prov
        return actual or "Deejay.de"

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
            pedidos=visible_records(lista, 'pedidos'),
            proveedores=_opciones_proveedor(),
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
        proveedor = _proveedor_del_form()
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
            # Si Facundo carga el costo en pesos directo, ese manda sobre la conversion
            # de EUR/USD (mismo comportamiento que editar 'costo manual' despues).
            costo_uyu_manual=_opt_float("costo_uyu_manual"),
            # Comisión del banco (UYU): suma al costo total del pedido.
            comision_banco_uyu=_opt_float("comision_banco_uyu"),
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

    @app.route("/admin/pedidos/<int:pedido_id>/seguimiento", methods=["POST"])
    @auth.admin_required
    def pedido_seguimiento(pedido_id):
        """Guarda (o borra, si viene vacio) el link de seguimiento del paquete."""
        from models import Pedido
        from urllib.parse import urlsplit
        ped = Pedido.query.get_or_404(pedido_id)
        raw = (request.form.get("seguimiento_url") or "").strip()
        if not raw:
            ped.seguimiento_url = None
            db.session.commit()
            flash("Se borro el link de seguimiento del pedido.", "success")
        else:
            if "://" not in raw:
                raw = "https://" + raw          # pegado sin https:// -> se completa
            partes = urlsplit(raw)
            if partes.scheme not in ("http", "https") or "." not in (partes.netloc or ""):
                flash("Ese link no parece valido. Pegalo completo (ej: https://www.dhl.com/...).", "error")
            else:
                ped.seguimiento_url = raw[:500]
                db.session.commit()
                flash("Link de seguimiento guardado.", "success")
        return redirect(url_for("pedidos") + f"#ped-{ped.id}")

    @app.route("/admin/pedidos/<int:pedido_id>/editar", methods=["POST"])
    @auth.admin_required
    def pedido_editar(pedido_id):
        """Edita todos los campos de un pedido (por si se cargo algo mal)."""
        from models import Pedido, PROVEEDORES_PEDIDO
        from datetime import datetime as _dt
        ped = Pedido.query.get_or_404(pedido_id)

        ped.numero = (request.form.get("numero") or "").strip() or None

        ped.proveedor = _proveedor_del_form(actual=ped.proveedor)

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

        # Comisión del banco (UYU): plata real que SUMA al total del pedido. Vacío => None.
        # Mismo parseo es-UY que el costo manual (puntos=miles, coma=decimal).
        raw_com = (request.form.get("comision_banco_uyu") or "").strip()
        if raw_com == "":
            ped.comision_banco_uyu = None
        elif any(c.isdigit() for c in raw_com):
            vc = _parse_monto_caja(raw_com)
            ped.comision_banco_uyu = vc if vc >= 0 else None

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
                ws2.cell(row=row, column=7, value=round(d.costo_real_uyu))
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
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        import io
        from xml.sax.saxutils import escape
        import marca_pdf as MARCA   # paleta, tipografia y logo del manual de marca

        FONT, FONT_M, FONT_B = MARCA.FONT, MARCA.FONT_M, MARCA.FONT_B

        pedidos = Pedido.query.order_by(Pedido.fecha.desc()).all()
        output = io.BytesIO()
        doc = SimpleDocTemplate(output, pagesize=landscape(A4),
                                leftMargin=1*cm, rightMargin=1*cm, topMargin=1.2*cm, bottomMargin=1.4*cm)
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle("titulo", parent=styles["Title"], fontName=FONT_B, fontSize=18, textColor=MARCA.NEGRO)
        sub_style = ParagraphStyle("sub", parent=styles["Normal"], fontName=FONT, fontSize=9, textColor=MARCA.TEXTO_2)
        h2_style = ParagraphStyle("h2", parent=styles["Heading2"], fontName=FONT_M, fontSize=13, textColor=MARCA.NEGRO)
        celda = ParagraphStyle("celda", parent=styles["Normal"], fontName=FONT, fontSize=7, leading=9, textColor=MARCA.NEGRO)
        elems = []

        # Cabecera de marca: logo primario negro sobre fondo claro (regla del manual)
        _logo = MARCA.logo_path()
        if _logo:
            _lw, _lh = MARCA.logo_size(4.6*cm)
            elems.append(Image(_logo, width=_lw, height=_lh, hAlign="LEFT"))
            elems.append(Spacer(1, 0.15*cm))
        else:
            elems.append(Paragraph("ARCANO RECORD STORE", title_style))
        from datetime import datetime as _dt
        elems.append(Paragraph(f"Informe de pedidos &middot; generado el {_dt.now().strftime('%d/%m/%Y')}", sub_style))
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
        kpi_table = Table(kpi_data, colWidths=[4.5*cm, 5*cm, 4.5*cm, 6*cm], hAlign="LEFT")
        kpi_table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), FONT),
            ("FONTNAME", (0, 0), (0, -1), FONT_M),
            ("FONTNAME", (2, 0), (2, -1), FONT_M),
            ("TEXTCOLOR", (0, 0), (0, -1), MARCA.TEXTO_2),
            ("TEXTCOLOR", (2, 0), (2, -1), MARCA.TEXTO_2),
            ("TEXTCOLOR", (1, 0), (1, -1), MARCA.NEGRO),
            ("TEXTCOLOR", (3, 0), (3, -1), MARCA.NEGRO),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("BACKGROUND", (0, 0), (-1, -1), MARCA.CREMA),
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
        t = Table(data, repeatRows=1, hAlign="LEFT")
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), MARCA.NEGRO),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), FONT_M),
            ("FONTNAME", (0, 1), (-1, -1), FONT),
            ("TEXTCOLOR", (0, 1), (-1, -1), MARCA.NEGRO),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("GRID", (0, 0), (-1, -1), 0.5, MARCA.LINEA),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, MARCA.FILA_ALT]),
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
                    Paragraph(escape(d.artista or ""), celda),
                    Paragraph(escape(d.titulo or ""), celda),
                    Paragraph(escape(d.sello or ""), celda),
                    fmt(d.precio_mostrar or d.precio_uyu or 0),
                    d.estado,
                ])
            dt = Table(ddata, repeatRows=1, hAlign="LEFT", colWidths=[5*cm, 6*cm, 4*cm, 3*cm, 3*cm])
            dt.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), MARCA.MARRON),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), FONT_M),
                ("FONTNAME", (0, 1), (-1, -1), FONT),
                ("TEXTCOLOR", (0, 1), (-1, -1), MARCA.NEGRO),
                ("FONTSIZE", (0, 0), (-1, -1), 7),
                ("GRID", (0, 0), (-1, -1), 0.5, MARCA.LINEA),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, MARCA.FILA_ALT]),
                ("ALIGN", (3, 0), (3, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]))
            elems.append(dt)

        _pw = landscape(A4)[0]

        def _pie(canv, docu):
            canv.saveState()
            canv.setFont(FONT, 7.5)
            canv.setFillColor(MARCA.TEXTO_3)
            canv.drawString(1*cm, 0.8*cm, "Arcano Record Store  ·  Montevideo  ·  numeros en pesos uruguayos (UYU)")
            canv.drawRightString(_pw - 1*cm, 0.8*cm, "Pagina %d" % docu.page)
            canv.setStrokeColor(MARCA.LINEA)
            canv.setLineWidth(0.5)
            canv.line(1*cm, 1.1*cm, _pw - 1*cm, 1.1*cm)
            canv.restoreState()

        doc.build(elems, onFirstPage=_pie, onLaterPages=_pie)
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
        from backups_service import download_database
        return download_database()

    @app.route("/admin/backup/completo")
    @auth.admin_required
    def backup_completo():
        from backups_service import download_complete
        return download_complete(app)

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

    VIDEO_SAMPLE_EXTS = {".mp4", ".mov", ".webm", ".m4v", ".avi", ".mkv"}

    @app.route("/admin/mantenimiento/comprimir-samples", methods=["POST"])
    @auth.admin_required
    def admin_comprimir_samples():
        """Convierte a AUDIO los samples que todavia son VIDEO (libera ~90% por archivo).
        Procesa de a lotes para no pasarse del tiempo de respuesta; si quedan, avisa que
        se toque de nuevo. El cliente escucha igual (la ficha reproduce audio)."""
        import video as _video
        import uuid as _uuid
        samples_dir = app.config.get("SAMPLES_DIR")
        if not samples_dir or not os.path.isdir(samples_dir):
            flash("No hay carpeta de samples.", "error")
            return redirect(url_for("settings_page"))
        # Tracks cuyo sample sigue siendo un video
        pend = []
        for t in Track.query.filter(Track.sample_local.isnot(None)).all():
            if os.path.splitext(t.sample_local)[1].lower() in VIDEO_SAMPLE_EXTS:
                pend.append(t)
        LOTE = 8
        conv, libres, err = 0, 0, 0
        for t in pend[:LOTE]:
            src = os.path.join(samples_dir, t.sample_local)
            if not os.path.isfile(src):
                continue
            audio_fname = f"sample_{_uuid.uuid4().hex[:12]}.m4a"
            audio_path = os.path.join(samples_dir, audio_fname)
            ok, _e = _video.extract_audio_from_path(src, audio_path)
            if ok:
                try:
                    sz_old = os.path.getsize(src)
                    sz_new = os.path.getsize(audio_path)
                    old_name = t.sample_local
                    t.sample_local = audio_fname
                    db.session.commit()
                    # Borrar el video original (es lo que ocupa): antes quedaba en disco y
                    # "comprimir" en realidad SUMABA espacio. Solo si ningun otro track lo
                    # usa (un disco duplicado comparte el mismo archivo).
                    try:
                        if not Track.query.filter_by(sample_local=old_name).first():
                            os.remove(src)
                    except Exception:
                        pass
                    conv += 1
                    libres += max(0, sz_old - sz_new)
                except Exception:
                    db.session.rollback()
                    try:
                        if os.path.exists(audio_path):
                            os.remove(audio_path)
                    except Exception:
                        pass
                    err += 1
            else:
                try:
                    if os.path.exists(audio_path):
                        os.remove(audio_path)
                except Exception:
                    pass
                err += 1
        quedan = max(0, len(pend) - conv)
        mb = libres / 1024 / 1024
        if conv:
            msg = f"Convertí {conv} sample(s) a audio y liberaste {mb:.0f} MB."
            if quedan:
                msg += f" Quedan {quedan} — toca de nuevo el boton para seguir."
            if err:
                msg += f" ({err} con error)"
            flash(msg, "success")
        elif err:
            flash(f"No se pudo convertir ({err} con error). Puede que esos videos no tengan audio.", "warning")
        else:
            flash("No hay videos para convertir: ya estan todos en audio.", "info")
        return redirect(url_for("settings_page"))

    @app.route("/admin/mantenimiento/limpiar-samples", methods=["POST"])
    @auth.admin_required
    def admin_limpiar_samples():
        """Borra TODOS los samples huerfanos (no usados por ningun track) para liberar
        espacio en disco. Manual: ignora el limite de 24h."""
        try:
            n, libres = _limpiar_samples_huerfanos(0)
            mb = libres / 1024 / 1024
            if n:
                flash(f"Listo: borre {n} sample(s) sin usar y liberaste {mb:.1f} MB.", "success")
            else:
                flash("No habia samples para borrar ahora. Los de cambios de los ultimos 30 "
                      "dias se guardan por si queres recuperar algo desde el Historial; despues "
                      "se borran solos.", "info")
        except Exception as e:
            flash(f"No se pudo limpiar: {e}", "error")
        return redirect(url_for("settings_page"))

    def _uso_disco():
        """Uso del disco persistente (samples, covers, base) + espacio libre, para
        mostrar en Ajustes. Solo lee: funciona aunque el disco este lleno."""
        import shutil
        info = {"samples_mb": 0.0, "samples_n": 0, "huerfanos_n": 0, "huerfanos_mb": 0.0,
                "video_n": 0, "video_mb": 0.0,
                "covers_mb": 0.0, "db_mb": 0.0, "libre_mb": None, "total_mb": None}
        samples_dir = app.config.get("SAMPLES_DIR")
        info["protegidos_n"] = 0
        info["protegidos_mb"] = 0.0
        try:
            from operation_history import retained_samples as _retenidos
            protegidos = _retenidos()
        except Exception:
            protegidos = set()
        try:
            if samples_dir and os.path.isdir(samples_dir):
                usados = {t.sample_local for t in
                          Track.query.filter(Track.sample_local.isnot(None)).all()}
                for fn in os.listdir(samples_dir):
                    p = os.path.join(samples_dir, fn)
                    if not os.path.isfile(p):
                        continue
                    sz = os.path.getsize(p)
                    info["samples_n"] += 1
                    info["samples_mb"] += sz / 1024 / 1024
                    if fn not in usados and fn in protegidos:
                        # Sin usar, pero de un cambio reciente: se puede recuperar desde el
                        # Historial, asi que la limpieza lo guarda hasta 30 dias.
                        info["protegidos_n"] += 1
                        info["protegidos_mb"] += sz / 1024 / 1024
                    elif fn not in usados:
                        info["huerfanos_n"] += 1
                        info["huerfanos_mb"] += sz / 1024 / 1024
                    elif os.path.splitext(fn)[1].lower() in VIDEO_SAMPLE_EXTS:
                        # sample EN USO que todavia es video (se puede comprimir a audio)
                        info["video_n"] += 1
                        info["video_mb"] += sz / 1024 / 1024
        except Exception:
            pass
        try:
            covers_dir = app.config.get("COVERS_DIR")
            if covers_dir and os.path.isdir(covers_dir):
                for fn in os.listdir(covers_dir):
                    p = os.path.join(covers_dir, fn)
                    if os.path.isfile(p):
                        info["covers_mb"] += os.path.getsize(p) / 1024 / 1024
        except Exception:
            pass
        try:
            db_file = app.config["SQLALCHEMY_DATABASE_URI"].replace("sqlite:///", "")
            if not os.path.isabs(db_file):
                db_file = os.path.join(app.instance_path, db_file)
            if os.path.exists(db_file):
                info["db_mb"] = os.path.getsize(db_file) / 1024 / 1024
            du = shutil.disk_usage(os.path.dirname(db_file) or ".")
            info["libre_mb"] = du.free / 1024 / 1024
            info["total_mb"] = du.total / 1024 / 1024
        except Exception:
            pass
        for k in list(info):
            if isinstance(info[k], float):
                info[k] = round(info[k], 1)
        return info

    # ------------------------------------------------------------------
    # API externa: crear ficha desde el Drop Hunter (u otra herramienta).
    # Protegida por clave secreta (header X-Api-Key). Ver Ajustes.
    # ------------------------------------------------------------------
    def _dh_api_key():
        """Clave de la API externa: env DROP_HUNTER_API_KEY tiene prioridad; si no,
        una guardada en settings (se genera sola la primera vez)."""
        env = (os.environ.get("DROP_HUNTER_API_KEY") or "").strip()
        if env:
            return env
        k = (Setting.get("drop_hunter_api_key", "") or "").strip()
        if not k:
            import secrets
            k = secrets.token_urlsafe(24)
            try:
                Setting.set("drop_hunter_api_key", k)
            except Exception:
                pass
        return k

    @app.route("/admin/ajustes/dh-key/regenerar", methods=["POST"])
    @auth.admin_required
    def admin_regenerar_dh_key():
        import secrets
        try:
            Setting.set("drop_hunter_api_key", secrets.token_urlsafe(24))
            flash("Clave del Drop Hunter regenerada. Actualizala en el Drop Hunter.", "success")
        except Exception as e:
            flash(f"No se pudo regenerar: {e}", "error")
        return redirect(url_for("settings_page"))

    @app.route("/api/disco", methods=["POST"])
    def api_crear_disco():
        """Crea una ficha desde una herramienta externa (Drop Hunter). Auth por clave
        (header X-Api-Key). Acepta JSON, o multipart con un archivo 'cover' + campo
        'payload' (JSON). Idempotente por source_url (si ya existe, la devuelve)."""
        import hmac
        import json as _json
        import uuid as _uuid
        # --- Auth por clave ---
        esperada = _dh_api_key()
        recibida = (request.headers.get("X-Api-Key") or "").strip()
        if not esperada or not recibida or not hmac.compare_digest(recibida, esperada):
            return jsonify({"ok": False, "error": "clave invalida"}), 401
        # --- Payload (JSON o multipart) ---
        cover_file = None
        if request.content_type and "multipart" in (request.content_type or ""):
            try:
                data = _json.loads(request.form.get("payload") or "{}")
            except Exception:
                return jsonify({"ok": False, "error": "payload JSON invalido"}), 400
            cover_file = request.files.get("cover")
        else:
            data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "payload invalido"}), 400
        artista = (data.get("artista") or data.get("artist") or "").strip()
        titulo = (data.get("titulo") or data.get("title") or "").strip()
        if not artista and not titulo:
            return jsonify({"ok": False, "error": "faltan artista y titulo"}), 400
        source_url = (data.get("source_url") or data.get("url") or "").strip() or None

        # --- Dedup por source_url ---
        if source_url:
            ya = Disc.query.filter_by(source_url=source_url).first()
            if ya:
                return jsonify({"ok": True, "disc_id": ya.id, "existing": True,
                                "url": url_for("disc_detail", disc_id=ya.id, _external=True)})

        def _f(k):
            try:
                return round(float(str(data.get(k)).replace(",", ".")), 2)
            except (TypeError, ValueError):
                return None

        def _i(k):
            try:
                return int(data.get(k))
            except (TypeError, ValueError):
                return None

        # Estado elegido en el Drop Hunter. Solo estados que NO tocan la contabilidad:
        # Vendido registraria una venta, y Reservado/Descartado no aplican a un alta.
        # es_preorder (carrusel) no se toca: se prende a mano.
        _estado = data.get("estado")
        _estado = _estado.strip() if isinstance(_estado, str) else _estado
        if _estado not in ("Disponible", "Pre-order", "Encargo", "Apartado para mi"):
            _estado = "Disponible"

        disc = Disc(
            estado=_estado, stock=1, origen="arcano",
            artista=(artista or "Various")[:200], titulo=(titulo or "Sin título")[:300],
            sello=((data.get("sello") or data.get("label") or "").strip() or None),
            anio=_i("anio") or _i("year"),
            genero=((data.get("genero") or data.get("genre") or "").strip() or None),
            formato=((data.get("formato") or data.get("format") or "").strip() or "Vinyl"),
            pais=((data.get("pais") or "").strip() or None),
            tags=((data.get("catalogo") or data.get("catalog") or data.get("tags") or "").strip() or None),
            # Costo y envio (la parte de ESTE disco), en euros. Con el TC arman el
            # costo final en pesos, que es lo que usa Contabilidad para la ganancia.
            costo_eur=max(0.0, _f("costo_eur") or 0.0),
            envio_eur=max(0.0, _f("envio_eur") or 0.0),
            precio_uyu=float(_i("precio_uyu") or _i("precio") or _i("precio_venta") or 0),
            # Condicion: solo se acepta una de la lista de la web; si no, queda vacia.
            condicion=((data.get("condicion") or "").strip()
                       if (data.get("condicion") or "").strip() in CONDICIONES_DISCO else None),
            source_url=source_url, fecha_ingreso=now_uy(),
        )
        try:
            disc.recalcular_costo_final(tc=Setting.get_tc())
        except Exception:
            pass

        # --- Portada: archivo subido (preferido) o descarga de cover_url ---
        try:
            covers_dir = app.config["COVERS_DIR"]
            cover_local = None
            if cover_file and cover_file.filename:
                ext = os.path.splitext(cover_file.filename)[1].lower()
                if ext not in (".jpg", ".jpeg", ".png", ".webp"):
                    ext = ".jpg"
                tmp = f"dh_{_uuid.uuid4().hex[:12]}{ext}"
                cover_file.save(os.path.join(covers_dir, tmp))
                cover_local = convertir_cover_a_webp(os.path.join(covers_dir, tmp))
            elif (data.get("cover_url") or "").strip().startswith("http"):
                cl = scraper.download_cover(data["cover_url"], covers_dir)
                if cl:
                    cover_local = convertir_cover_a_webp(os.path.join(covers_dir, cl))
            if cover_local:
                disc.cover_local = cover_local
        except Exception:
            pass

        db.session.add(disc)
        db.session.flush()
        db.session.add(StatusHistory(disc_id=disc.id, estado=disc.estado, nota="Ingreso al stock (Drop Hunter API)"))

        # --- Tracks. Sample: un audio subido (track_audio_<idx>, ej MP3 del Drop Hunter)
        #     tiene prioridad; si no, el link de YouTube. Un youtube_url "upload://..."
        #     es un placeholder de audio subido: NO es una URL real, se ignora. ---
        _audio_exts = (".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac", ".aac")
        tl_lines = []
        for idx, t in enumerate(data.get("tracks") or []):
            if not isinstance(t, dict):
                continue
            lado = ((t.get("lado") or t.get("side") or "").strip() or None)
            art = ((t.get("artista") or t.get("artist") or "").strip() or None)
            tit = ((t.get("titulo") or t.get("title") or "").strip() or f"Track {idx + 1}")
            yt = ((t.get("youtube_url") or t.get("youtube") or "").strip() or None)
            if yt and yt.lower().startswith("upload://"):
                yt = None  # placeholder de MP3 subido, no es una URL
            sample_local = None
            try:
                audio_f = request.files.get(f"track_audio_{idx}")
                if audio_f and audio_f.filename:
                    aext = os.path.splitext(audio_f.filename)[1].lower()
                    if aext in _audio_exts:
                        samples_dir = app.config["SAMPLES_DIR"]
                        os.makedirs(samples_dir, exist_ok=True)
                        sname = f"sample_{_uuid.uuid4().hex[:12]}{aext}"
                        audio_f.save(os.path.join(samples_dir, sname))
                        sample_local = sname
            except Exception:
                sample_local = None
            db.session.add(Track(disc_id=disc.id, posicion=idx, lado=lado, artista=art,
                                 titulo=tit[:300], youtube_url=(None if sample_local else yt),
                                 sample_local=sample_local))
            _p = (f"{lado}. " if lado else f"{idx + 1}. ")
            tl_lines.append(_p + (f"{art} - {tit}" if art else tit))
        if tl_lines:
            disc.tracklist = "\n".join(tl_lines)
        db.session.commit()
        return jsonify({"ok": True, "disc_id": disc.id, "existing": False,
                        "url": url_for("disc_detail", disc_id=disc.id, _external=True)}), 201

    @app.route("/admin/settings")
    @auth.admin_required
    def settings_page():
        tc = Setting.get_tc()
        tc_row = Setting.query.filter_by(key="tc_eur_uyu").first()

        discos_disponibles = (Disc.query.filter_by(estado="Disponible")
                              .filter(Disc.es_mystery_box.isnot(True)).count())
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
            uso_disco=_uso_disco(),
            dh_api_key=_dh_api_key(),
            dh_api_url=url_for("api_crear_disco", _external=True),
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
            public_diseno=Setting.get("public_diseno", ""),
            public_carrusel_modo=Setting.get("public_carrusel_modo", "compacto"),
            public_modo_defecto=Setting.get("public_modo_defecto", "oscuro"),
            admin_diseno_actual=Setting.get("admin_diseno", ""),
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
                f"{d.costo_real_uyu:.2f}",
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

    def _mystery_box_id():
        """Id del disco Mystery Box (o None). Barato: solo la columna id."""
        row = (db.session.query(Disc.id)
               .filter(Disc.es_mystery_box == True)  # noqa: E712
               .first())
        return row[0] if row else None

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
            # Mystery Box: precios por cantidad (para la card, la ficha y el carrito).
            "mbox_precios": MYSTERY_BOX_PRECIOS,
            "mbox_min": min(MYSTERY_BOX_PRECIOS.values()),
            "mbox_max": max(MYSTERY_BOX_PRECIOS.values()),
            "mbox_disc_id": _mystery_box_id(),
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

    def _checkout_fs(ids_raw):
        """Aviso del checkout: hay discos en Fire Sale y el visitante no inicio sesion.
        Le ofrece crear la cuenta en el mismo paso o iniciar sesion y volver aca."""
        if auth.cliente_actual_id():
            return {"fs_aviso": False, "fs_login_url": None}
        ids = [int(x) for x in (ids_raw or "").split(",") if x.strip().isdigit()][:100]
        hay = bool(ids) and any(getattr(d, "en_fire_sale_activo", False)
                                for d in Disc.query.filter(Disc.id.in_(ids)).all())
        # El carrito normalmente llega SIN ids en la URL (los carga el JS desde el
        # navegador): el aviso se renderiza oculto y el JS lo muestra si algun disco
        # del carrito esta en Fire Sale.
        return {"fs_aviso": hay,
                "fs_login_url": url_for("cliente_login_page",
                                        next=url_for("public_checkout", ids=ids_raw) if ids_raw
                                        else url_for("public_checkout"))}

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

    def _mp_iniciar_pago(reserva, discos, extras=None):
        """Crea la preferencia de Mercado Pago para una reserva y devuelve la URL
        de pago (o None si algo falla; el caller cae al flujo normal de reserva).

        extras: items extra que no son discos (ej: la Mystery Box), como lista de
        {"title", "unit_price"}. Suman al subtotal y al recargo igual que un disco."""
        # Usar el precio REAL a cobrar (Fire Sale u oferta si aplica) para cobrar lo
        # mismo que ve el cliente en el checkout/mail, y descartar los sin precio
        # (un unit_price 0 rompe la preferencia).
        discos_cobrables = [d for d in discos if (_precio_a_cobrar(d) or 0) > 0]
        extras = [e for e in (extras or []) if float(e.get("unit_price") or 0) > 0]
        subtotal = (sum((_precio_a_cobrar(d) or 0) for d in discos_cobrables)
                    + sum(float(e["unit_price"]) for e in extras))
        if subtotal <= 0:
            return None
        recargo = mp.calcular_recargo(subtotal)
        items = [
            {"title": f"{d.artista} - {d.titulo}", "quantity": 1,
             "unit_price": float(_precio_a_cobrar(d) or 0)}
            for d in discos_cobrables
        ]
        for e in extras:
            items.append({"title": e["title"], "quantity": 1,
                          "unit_price": float(e["unit_price"])})
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
        base = _base_publica()
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
                                              origen="web", seguro=True)
            if cli_res and not reserva.cliente_id:
                reserva.cliente_id = cli_res.id
        conflicto = []  # discos comprometidos con OTRA venta/persona (posible doble cobro)
        _cli_pagador = cli_res.id if cli_res else reserva.cliente_id
        for item in reserva.items_efectivos:
            d = Disc.query.get(item.disc_id) if getattr(item, "disc_id", None) else None
            if not d:
                continue
            if d.estado == "Vendido" and (d.venta_grupo or "") != f"r{reserva.id}":
                # Ya vendido por otra via (otro cliente por MP, o venta manual/IG)
                conflicto.append(d)
                continue
            # Apartado / encargado / pre-order con dueno: es de otra persona salvo que sea
            # el mismo cliente que esta pagando (o que lo haya apartado esta reserva).
            # Antes solo contaba "Vendido": un disco Reservado por Instagram o un Encargo
            # con sena de otro cliente se vendia igual al que pagaba por MP.
            _de_otro = False
            if d.estado in ("Pre-order", "Encargo") and d.venta_grupo:
                # Pre-order tomado por un pedido web: es de ESE pedido y de nadie mas.
                _de_otro = d.venta_grupo != f"r{reserva.id}"
            elif d.estado in ("Reservado", "Encargo") or (d.estado == "Pre-order" and d.cliente_id):
                _mismo = bool(_cli_pagador) and d.cliente_id == _cli_pagador
                if not (_mismo or (d.estado == "Reservado" and _disco_es_de_reserva(d, reserva))):
                    _de_otro = True
            elif d.estado in ("Apartado para mi", "Descartado"):
                _de_otro = True
            if _de_otro:
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
            _nota = ("[ATENCION: pago por MP de disco(s) ya vendido(s), apartado(s) o encargado(s) "
                     "para otra persona: %s. NO se marcaron vendidos. Revisar/reembolsar.]" % _nombres)
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
                discos = [Disc.query.get(i.disc_id) for i in reserva.items_efectivos if i.disc_id]
                mailer.send_compra_confirmacion_cliente(
                    reserva, [d for d in discos if d], metodo="mercadopago")
            except Exception:
                pass
        # Aviso al admin (Facundo): entro un pago por Mercado Pago. Con MP el checkout
        # redirige antes de llegar al aviso, asi que lo mandamos recien al confirmarse.
        if mailer.is_configured() and reserva.items_efectivos:
            try:
                _disc0 = next((Disc.query.get(i.disc_id) for i in reserva.items_efectivos
                               if i.disc_id), None)
                mailer.send_reserva_notification(reserva, _disc0)
            except Exception:
                pass
        # Aviso push al celu del admin: entro una venta cobrada por Mercado Pago.
        try:
            _enviar_push(
                "\U0001F4B8 ¡Venta por Mercado Pago!",
                "%s pagó %s disco(s) · $%s" % (
                    reserva.nombre, reserva.cantidad_discos,
                    "{:,.0f}".format(reserva.total_uyu or 0).replace(",", ".")),
                "/admin/reservas/%s" % reserva.id,
            )
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
        disponibles = Disc.query.filter(Disc.estado == "Disponible", Disc.stock > 0,
                                        Disc.precio_uyu > 0, Disc.oculto_web.isnot(True))
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
                sugerencias.append({"texto": titulo_full, "tipo": "disco", "id": d.id,
                                    "url": url_for("public_disc", disc_id=d.id)})
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
            "Disallow: /favoritos",
            "Disallow: /reservar/",
            "Disallow: /mp/",
            "Disallow: /api/",
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
                    # Disponibles con stock y CON PRECIO puesto (los de precio 0 quedan
                    # ocultos hasta que Facundo les pone precio; ej: fichas creadas por la
                    # API del Drop Hunter, que llegan sin precio en pesos).
                    (Disc.estado == "Disponible") & (Disc.stock > 0) & (Disc.precio_uyu > 0),
                    # O agotados que el admin eligio mostrar igual
                    Disc.mostrar_agotado == True,  # noqa: E712
                    # O en estado Pre-order / Encargo (se reservan por adelantado)
                    Disc.estado.in_(["Pre-order", "Encargo"]),
                )
            )
            # "NO mostrar" (ej: la 2da copia de un disco repetido) no sale en la web
            .filter(Disc.oculto_web.isnot(True))
            .all()
        )
        # La Mystery Box no es un disco normal: se saca del listado (no entra en
        # carruseles, facetas ni contadores) y se vuelve a fijar como PRIMERA card
        # de la grilla en la vista sin filtros (mas abajo).
        mystery_box = next((d for d in all_disponibles if getattr(d, "es_mystery_box", False)), None)
        if mystery_box is not None:
            all_disponibles = [d for d in all_disponibles if not getattr(d, "es_mystery_box", False)]
        discs = list(all_disponibles)

        # Filtros
        q = (request.args.get("q") or "").strip()
        sellos_sel = [s.strip() for s in request.args.getlist("sello") if s.strip()]
        genero_sel = [g.strip() for g in request.args.getlist("genero") if g.strip()]
        tags_sel = [t.strip() for t in request.args.getlist("tag") if t.strip()]
        formato_sel = [f.strip() for f in request.args.getlist("formato") if f.strip()]
        pais_sel = [p.strip() for p in request.args.getlist("pais") if p.strip()]
        decada_sel = [d.strip() for d in request.args.getlist("decada") if d.strip()]
        # Condicion: 'nuevo' (Nuevo / Sellado) o 'usado' (cualquier "Usado - ...")
        # Solo los discos en SALE (precio de oferta), desde el boton junto a "Escuchar todo"
        sale_sel = request.args.get("sale") == "1"
        condicion_sel = [c.strip().lower() for c in request.args.getlist("condicion")
                         if c.strip().lower() in ("nuevo", "usado")]
        orden = (request.args.get("orden") or "nuevos").strip()
        con_sample = request.args.get("con_sample") == "1"
        precio_min = _parse_int(request.args.get("precio_min"), None)
        precio_max = _parse_int(request.args.get("precio_max"), None)
        anio_sel = []
        for _a in request.args.getlist("anio"):
            _ai = _parse_int(_a, None)
            if _ai is not None:
                anio_sel.append(_ai)

        def _decada(anio):
            if not anio:
                return None
            return f"{(anio // 10) * 10}s"

        def _tipo_condicion(d):
            """'nuevo' / 'usado' segun la condicion cargada en el disco (None si no tiene)."""
            c = (d.condicion or "").strip().lower()
            if c.startswith("usado"):
                return "usado"
            if c.startswith("nuevo"):
                return "nuevo"
            return None

        if q:
            # Busca tambien en los TRACKS (titulo y artista: la gente busca el tema que
            # escucho en el reel) y sin importar tildes. Antes "Orbiting Voices" (un
            # track) o "vernis" (Verniß) daban 0, y esos 0 falsos ensuciaban el Radar.
            _q = _norm_busqueda(q)
            _q_alt = _q.replace("ss", "s")   # "vernis" vs "verniss"

            def _coincide(d):
                campos = [d.artista, d.titulo, d.sello, d.genero]
                for t in (d.tracks or []):
                    campos.append(t.titulo)
                    campos.append(t.artista)
                for cmp in campos:
                    if not cmp:
                        continue
                    n = _norm_busqueda(cmp)
                    if _q in n or _q_alt in n.replace("ss", "s"):
                        return True
                return False
            discs = [d for d in discs if _coincide(d)]
        if sellos_sel:
            # Agrupado sin el numero de catalogo ("Fresh Tunez TUNEZ005" = "Fresh Tunez")
            sl = {sello_base(s).lower() for s in sellos_sel}
            discs = [d for d in discs if sello_base(d.sello).lower() in sl]
        if genero_sel:
            gl = {g.lower() for g in genero_sel}
            discs = [d for d in discs
                     if any(tok.lower() in gl for tok in _norm_generos(d.genero))]
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
        if condicion_sel:
            cs = set(condicion_sel)
            discs = [d for d in discs if _tipo_condicion(d) in cs]
        if sale_sel:
            discs = [d for d in discs if d.en_oferta and not d.esta_agotado]
        if con_sample:
            discs = [d for d in discs if d.tracks and any(t.has_sample for t in d.tracks)]
        if precio_min is not None:
            discs = [d for d in discs if (d.precio_uyu or 0) >= precio_min]
        if precio_max is not None:
            discs = [d for d in discs if (d.precio_uyu or 0) <= precio_max]
        if anio_sel:
            _aset = set(anio_sel)
            discs = [d for d in discs if d.anio in _aset]

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

        # Los agotados (que se muestran igual porque los marcaste "mostrar agotado") van
        # SIEMPRE al final, con cualquier orden: antes se mezclaban y habia paginas con
        # 10 agotados de 16. sort es estable: dentro de cada grupo se respeta el orden.
        discs.sort(key=lambda d: 1 if (d.esta_agotado and not d.mostrar_preorder) else 0)
        # Para el contador: solo los que se pueden comprar (sin agotados ni la box).
        total_comprables = sum(1 for d in discs if not (d.esta_agotado and not d.mostrar_preorder))

        # Mystery Box fijada primera: solo en la vista de navegacion (sin busqueda ni
        # filtros). Si hay un filtro/genero activo, la box no aplica y no se muestra.
        _sin_filtros = (not any([q, sellos_sel, genero_sel, tags_sel, formato_sel,
                                 pais_sel, decada_sel, anio_sel, con_sample, condicion_sel, sale_sel])
                        and precio_min is None and precio_max is None)
        if mystery_box is not None and _sin_filtros:
            discs = [mystery_box] + discs

        # Paginacion (sobre la lista ya filtrada y ordenada)
        # Grilla FIJA (decision de Facundo, 8/10/2026): 5 discos por fila x 5 filas = 25
        # por pagina, contando la Mystery Box como una card. Sin selectores para el cliente.
        # (En pantallas medianas el CSS baja las columnas; en celular, 1-2 por fila.)
        COLS = 5
        # Filas SIEMPRE completas: los discos por pagina dependen de cuantos entran por
        # fila en la pantalla del cliente (la tienda lo mide y lo manda en la cookie
        # arcano_cols). Compu 5 -> 25 (5x5); tablet 3 -> 27 (3x9); celular 2 -> 20 (2x10).
        # Sin cookie (primera visita) se adivina por el navegador: celular 2, si no 5.
        _cols_vis = _parse_int(request.cookies.get("arcano_cols"), 0) or 0
        if not (1 <= _cols_vis <= 6):
            _ua = (request.headers.get("User-Agent") or "").lower()
            _cols_vis = 2 if ("mobi" in _ua or "iphone" in _ua or "android" in _ua) else 5
        # Celular (1-2 por fila): 20 por pagina (2x10), pedido de Facundo.
        POR_PAGINA = 20 if _cols_vis <= 2 else _cols_vis * -(-25 // _cols_vis)
        total_resultados = len(discs)
        total_paginas = max(1, (total_resultados + POR_PAGINA - 1) // POR_PAGINA)
        pagina = _parse_int(request.args.get("pagina"), 1) or 1
        if pagina < 1:
            pagina = 1
        if pagina > total_paginas:
            pagina = total_paginas
        inicio = (pagina - 1) * POR_PAGINA
        discs_pagina = discs[inicio:inicio + POR_PAGINA]

        # Log de busqueda para el panel "Demanda no satisfecha" del Radar. Solo la
        # busqueda REAL de texto (no la paginacion ni el autocompletado), guardando
        # cuantos resultados dio. Lo que se busca y da 0 = lista de compras.
        if q and len(q) >= 2 and pagina == 1:
            try:
                from models import SearchQuery
                # Tope por IP (30 busquedas registradas cada 10 min): que un bot no llene
                # la base ni ensucie el Radar. La busqueda funciona igual, solo no se anota.
                import time as _tq
                _ip_q, _ahora_q = _client_ip(), _tq.time()
                _hq = [t for t in _busq_ip_hist.get(_ip_q, []) if _ahora_q - t < 600]
                if len(_hq) < 30:
                    _hq.append(_ahora_q)
                    # Insert por una conexion aparte: un commit de la sesion aca "vencia"
                    # todos los discos ya cargados y la pagina los volvia a leer de a uno
                    # (una busqueda hacia ~265 consultas en vez de ~50).
                    with db.engine.begin() as _cx:
                        _cx.execute(SearchQuery.__table__.insert().values(
                            termino=q[:120], resultados=total_resultados, fecha=datetime.utcnow()))
                _busq_ip_hist[_ip_q] = _hq
                if len(_busq_ip_hist) > 5000:
                    _busq_ip_hist.clear()
            except Exception:
                db.session.rollback()

        # Facetas con contadores (a partir de TODO el catalogo disponible)
        from collections import Counter
        sellos_count = Counter()
        sellos_display = {}          # clave minuscula sin catalogo -> nombre a mostrar
        generos_count = Counter()   # clave = genero en minuscula (agrupa May/min)
        generos_display = {}         # clave minuscula -> texto lindo para mostrar
        tags_count = Counter()
        formatos_count = Counter()
        paises_count = Counter()
        decadas_count = Counter()
        anios_count = Counter()
        condiciones_count = Counter()
        precios = []
        for d in all_disponibles:
            _tc = _tipo_condicion(d)
            if _tc:
                condiciones_count[_tc] += 1
            if d.sello:
                _sb = sello_base(d.sello)
                _nombre = sellos_display.setdefault(_sb.lower(), _sb)
                sellos_count[_nombre] += 1
            for g in _norm_generos(d.genero):
                key = g.lower()
                generos_count[key] += 1
                generos_display.setdefault(key, _pretty_genero(g))
            for tg in d.tags_list:
                tags_count[tg] += 1
            if d.formato:
                formatos_count[d.formato] += 1
            if d.pais:
                paises_count[d.pais] += 1
            dc = _decada(d.anio)
            if dc:
                decadas_count[dc] += 1
            if d.anio:
                anios_count[d.anio] += 1
            if d.precio_uyu and d.precio_uyu > 0:
                precios.append(d.precio_uyu)

        sellos = sorted(sellos_count.keys(), key=lambda x: x.lower())
        sellos_destacados = sellos_count.most_common(8)
        generos = sorted(
            ((generos_display[k], cnt) for k, cnt in generos_count.items()),
            key=lambda x: x[0].lower(),
        )
        tags_disponibles = sorted(tags_count.items())
        formatos = sorted(formatos_count.items())
        paises = sorted(paises_count.items())
        decadas = sorted(decadas_count.items(), reverse=True)
        anios = sorted(anios_count.items(), reverse=True)
        precio_rango = {
            "min": int(min(precios)) if precios else 0,
            "max": int(max(precios)) if precios else 0,
        }

        filtros_activos = sum([
            bool(sellos_sel), bool(genero_sel), bool(tags_sel),
            bool(formato_sel), bool(pais_sel), bool(decada_sel),
            con_sample, precio_min is not None, precio_max is not None,
            bool(anio_sel), bool(condicion_sel), sale_sel,
        ])

        # Secciones de la home: solo en pagina 1, sin filtros ni busqueda
        destacados = []
        nuevos_ingresos = []
        preorders_carrusel = []
        # La Mystery Box NUNCA va en un carrusel (solo como primera card de la grilla).
        disponibles_sin_mbox = [d for d in all_disponibles if not d.es_mystery_box]
        if pagina == 1 and filtros_activos == 0 and not q and orden == "nuevos":
            # Para los carruseles solo discos realmente disponibles (o pre-order).
            # Los vendidos/agotados NO entran, ni siquiera para completar al azar.
            disponibles_carrusel = [
                d for d in disponibles_sin_mbox
                if not d.esta_agotado or d.mostrar_preorder
            ]
            # --- DESTACADOS ---
            # Los marcados manualmente tienen prioridad. Si faltan para 16,
            # se completan con aleatorios que rotan cada 10 dias.
            _N_CARR = 16  # discos por carrusel (Destacados y Nuevos ingresos)
            marcados_dest = [d for d in disponibles_carrusel if d.destacado]
            destacados = list(marcados_dest[:_N_CARR])
            if len(destacados) < _N_CARR:
                import random as _random
                # Semilla estable por periodo de 10 dias
                periodo = int(datetime.utcnow().timestamp() // (10 * 86400))
                resto = [d for d in disponibles_carrusel if not d.destacado]
                rng = _random.Random(periodo)
                rng.shuffle(resto)
                destacados += resto[: _N_CARR - len(destacados)]

            # --- NUEVOS INGRESOS ---
            # Los marcados manualmente van primero, despues los mas recientes
            # por fecha de ingreso, hasta completar 16.
            marcados_nuevos = [d for d in disponibles_carrusel if d.en_nuevos]
            por_fecha = sorted(
                disponibles_carrusel,
                key=lambda d: d.fecha_ingreso or datetime.min,
                reverse=True,
            )
            nuevos_ingresos = list(marcados_nuevos[:_N_CARR])
            ids_ya = {d.id for d in nuevos_ingresos}
            for d in por_fecha:
                if len(nuevos_ingresos) >= _N_CARR:
                    break
                if d.id not in ids_ya:
                    nuevos_ingresos.append(d)
                    ids_ya.add(d.id)

            # --- PRE-ORDER ---
            # Discos en estado Pre-order con el toggle de carrusel prendido
            preorders_carrusel = [d for d in disponibles_sin_mbox if d.en_carrusel_preorder][:12]

            # Intercalar los staff picks (es_tip) para que no queden todos juntos
            # al principio y parezca que todos los discos lo son.
            destacados = _intercalar_tips(destacados)
            nuevos_ingresos = _intercalar_tips(nuevos_ingresos)

        # --- TIPS DEL CURADOR ---
        # Discos marcados como tip, disponibles. Se muestra siempre que haya.
        tips_carrusel = []
        if pagina == 1 and filtros_activos == 0 and not q and orden == "nuevos":
            tips_carrusel = [
                d for d in disponibles_sin_mbox
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
                    for d in disponibles_sin_mbox
                    if plays_por_disco.get(d.id, 0) > 0
                    and (not d.esta_agotado or d.mostrar_preorder)
                ]
                con_plays.sort(key=lambda x: x[1], reverse=True)
                mas_escuchados = [d for d, n in con_plays if not d.oculto_web][:6]

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
            anios=anios,
            filtro_anios=anio_sel,
            condiciones=[("nuevo", "Nuevo", condiciones_count.get("nuevo", 0)),
                         ("usado", "Usado", condiciones_count.get("usado", 0))],
            filtro_condiciones=condicion_sel,
            filtro_sale=sale_sel,
            por_pagina=POR_PAGINA,
            cols=COLS,
            n_sale=sum(1 for d in all_disponibles if d.en_oferta and not d.esta_agotado),
            url_sale=url_for("home", **{**{k: v for k, v in request.args.to_dict(flat=False).items()
                                            if k not in ("sale", "pagina")}, "sale": "1"}),
            url_sin_sale=url_for("home", **{k: v for k, v in request.args.to_dict(flat=False).items()
                                            if k not in ("sale", "pagina")}),
            total_comprables=total_comprables,
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

    _ESTADOS_FICHA = ("Disponible", "Pre-order", "Encargo", "Reservado", "Vendido")

    def _con_query(url):
        """Le agrega a una redireccion los parametros de la direccion original
        (?utm_source=..., fbclid de Meta, gclid de Google). Si se pierden, la visita de
        un anuncio queda como "directa" y no se puede medir la campana."""
        qs = request.query_string.decode("utf-8", "ignore") if request.query_string else ""
        if not qs:
            return url
        return url + ("&" if "?" in url else "?") + qs

    def _resolver_ficha(valor):
        """Busca el disco de una direccion. Devuelve (disco, redireccion).
        - /123 (numero): direccion vieja -> redirige a la linda.
        - /DJ-Gamba (mayusculas, tildes): redirige a la forma normal en minusculas.
        - Direccion vieja de un disco al que le cambiaste el nombre: redirige a la nueva.
        - Varias copias del mismo disco comparten direccion: se muestra la que se puede
          comprar (la visible y disponible); si todas se vendieron, la ultima vendida."""
        from models import slugify, DiscSlugViejo
        s = str(valor or "")
        if "." in s:
            return None, None   # /favicon.ico, /wp-login.php...: no es una ficha
        if s.isdigit():
            d = db.session.get(Disc, int(s))
            if d is not None and d.slug:
                return None, url_for("public_disc", disc_id=d.id)
            return d, None
        norm = slugify(s, largo=160)
        if not norm:
            return None, None
        if norm != s:
            return None, "/" + norm
        cands = Disc.query.filter(Disc.slug == norm).all()
        if not cands:
            v = (DiscSlugViejo.query.filter_by(slug=norm)
                 .order_by(DiscSlugViejo.id.desc()).first())
            if v is not None:
                d = db.session.get(Disc, v.disc_id)
                if d is not None and d.slug and d.slug != norm:
                    return None, url_for("public_disc", disc_id=d.id)
            return None, None
        publicos = [d for d in cands if d.estado in _ESTADOS_FICHA and not d.oculto_web]
        if not publicos:
            return cands[0], None   # la vista de abajo responde "no encontrado"

        def _orden(d):
            if d.estado == "Disponible" and not d.esta_agotado:
                return (0, 0)
            if d.estado in ("Pre-order", "Encargo") and not d.esta_agotado:
                return (1, 0)
            if d.estado == "Reservado":
                return (2, 0)
            _fv = d.fecha_venta.timestamp() if getattr(d, "fecha_venta", None) else 0
            return (3, -_fv)
        return sorted(publicos, key=_orden)[0], None

    @app.route("/disco/<int:disc_id>")
    def public_disc_id(disc_id: int):
        """Direccion vieja (/disco/123): redirige para siempre a la nueva
        (/artista-titulo). Asi siguen andando los links de mails, Instagram y Google."""
        if db.session.get(Disc, disc_id) is None:
            return render_template("public/not_found.html", **_public_context()), 404
        return redirect(_con_query(url_for("public_disc", disc_id=disc_id)), 301)

    @app.route("/<disco_slug:disc_id>")
    def public_disc(disc_id):
        """Ficha publica de un disco: arcanorecordstore.com/artista-titulo."""
        disc, _redir = _resolver_ficha(disc_id)
        if _redir:
            return redirect(_con_query(_redir), 301)
        if disc is None:
            return render_template("public/not_found.html", **_public_context()), 404
        # No mostrar discos que no esten disponibles publicamente
        # (excepcion: los pre-orders se ven siempre). "NO mostrar" = como si no existiera.
        if (disc.estado not in ("Disponible", "Pre-order", "Encargo", "Reservado", "Vendido")
                or disc.oculto_web):
            return render_template("public/not_found.html", **_public_context()), 404

        # Mystery Box: ficha propia (selector de cantidad + texto), sin "similares".
        if getattr(disc, "es_mystery_box", False):
            return render_template(
                "public/disc.html",
                disc=disc,
                disc_generos=[],
                relacionados=[],
                **_public_context(),
            )

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
            .filter(Disc.precio_uyu > 0)
            .filter(Disc.oculto_web.isnot(True))
            .filter((Disc.slug != disc.slug) | (Disc.slug.is_(None)))   # no la otra copia del mismo
            # La Mystery Box no va en "Similares" (ni en ningun carrusel)
            .filter(or_(Disc.es_mystery_box == False, Disc.es_mystery_box == None))  # noqa: E711,E712
        )

        _N_REL = 12  # se muestran en un carrusel horizontal con scroll (6 + 6)
        # 1. Mismo genero (lo mas relevante para diggear)
        if disc.genero:
            relacionados += _agregar(
                base.filter(Disc.genero == disc.genero).order_by(Disc.fecha_ingreso.desc()),
                _N_REL - len(relacionados),
            )
        # 2. Completar con mismo sello
        if len(relacionados) < _N_REL and disc.sello:
            relacionados += _agregar(
                base.filter(Disc.sello == disc.sello).order_by(Disc.fecha_ingreso.desc()),
                _N_REL - len(relacionados),
            )
        # 3. Completar con otros recientes si aun faltan
        if len(relacionados) < _N_REL:
            relacionados += _agregar(
                base.order_by(Disc.fecha_ingreso.desc()),
                _N_REL - len(relacionados),
            )

        return render_template(
            "public/disc.html",
            disc=disc,
            disc_generos=[_pretty_genero(g) for g in _norm_generos(disc.genero)],
            relacionados=relacionados,
            **_public_context(),
        )

    @app.route("/reservar/<int:disc_id>", methods=["GET", "POST"])
    def public_reservar(disc_id: int):
        """Form de reserva."""
        disc = Disc.query.get_or_404(disc_id)
        # Los pre-orders y encargos se pueden reservar aunque no tengan stock todavia,
        # salvo que ya los haya pedido otra persona (esta_agotado lo contempla). La ficha
        # de la Mystery Box y los discos sin precio no se reservan sueltos.
        if not _vendible_web(disc):
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
            # Igual que "Reservar y coordinar" del carrito: el disco queda reservado de
            # verdad (el mail le dice al cliente que esta reservado para el).
            reserva.metodo_pago = "coordinar"
            if _apartar_para_pedido(reserva, [disc], "a coordinar"):
                # Otra persona lo confirmo justo antes (compra simultanea)
                db.session.rollback()
                return render_template(
                    "public/not_available.html",
                    disc=Disc.query.get(disc_id),
                    **_public_context(),
                ), 400
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
        # "Reservar y coordinar" (y el pago online que fallo y quedo a coordinar): se mide
        # como Lead (GA4 generate_lead + Meta Lead), con el valor de la reserva. No es una
        # venta todavia, pero es el resultado que trae la publicidad.
        medir_lead = not medir_compra and reserva.metodo_pago in ("coordinar", None, "")
        return render_template(
            "public/reserva_ok.html",
            reserva=reserva,
            disc=disc,
            multi=multi,
            medir_compra=medir_compra,
            medir_lead=medir_lead,
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

    # --- Activacion de cuenta por email (v5.77) ---
    # Si el email ya tiene compras (una ficha creada sola por una compra sin cuenta, o
    # pedidos anonimos con ese email), la cuenta NO se crea al toque: se manda un link a
    # ese email y se activa recien cuando el dueno lo toca. Antes cualquiera que supiera
    # el email de un cliente se registraba con el y veia su historial, sus datos y su
    # lealtad. El link lleva la contrasena ya HASHEADA (nunca en texto) y vence en 48 h.
    def _mail_en_segundo_plano(asunto, texto, html, destino):
        """Manda el mail sin hacer esperar la respuesta: asi "recuperar" y "registro"
        tardan lo mismo exista o no la cuenta (antes se podia averiguar por el tiempo)."""
        import threading

        def _enviar():
            try:
                mailer.send_email(asunto, texto, html, to=destino)
            except Exception:
                pass
        threading.Thread(target=_enviar, daemon=True).start()

    def _activar_serializer():
        from itsdangerous import URLSafeTimedSerializer
        return URLSafeTimedSerializer(app.config.get("SECRET_KEY") or "dev-key",
                                      salt="cliente-activar-v1")

    def _email_tiene_compras(email, excluir_reserva_id=None):
        """True si ese email ya tiene una ficha sin contrasena o pedidos sin cuenta."""
        email = (email or "").strip().lower()
        if not email:
            return False
        ficha = Cliente.query.filter(db.func.lower(Cliente.email) == email).first()
        if ficha and not ficha.password_hash:
            return True
        q = Reserva.query.filter(Reserva.cliente_id.is_(None),
                                 db.func.lower(Reserva.email) == email)
        if excluir_reserva_id:
            q = q.filter(Reserva.id != excluir_reserva_id)
        return q.first() is not None

    def _mandar_activacion(email, password, nombre=None, whatsapp=None):
        """Manda el mail con el link para activar la cuenta. Devuelve True si salio."""
        from werkzeug.security import generate_password_hash
        if not mailer.can_send():
            return False
        token = _activar_serializer().dumps({
            "e": (email or "").strip().lower(), "h": generate_password_hash(password),
            "n": (nombre or "")[:120], "w": (whatsapp or "")[:40]})
        url = _base_publica() + url_for("cliente_activar", token=token)
        _nom = nombre or ""
        cuerpo = ("Hola %s,\n\nPara activar tu cuenta en Arcano Record Store y ver tus "
                  "compras, entra a este link (vence en 48 horas):\n%s\n\nSi no fuiste "
                  "vos, ignora este mail: no se crea nada." % (_nom, url))
        from markupsafe import escape as _esc
        html = mailer.mail_simple_html(
            "Tu cuenta", "Activ&aacute; tu cuenta.",
            [f"Hola {_esc(_nom.split(' ')[0])}, para activar tu cuenta en Arcano Record Store y ver tus "
             "compras, toc&aacute; el bot&oacute;n. El link vence en 48 horas."],
            boton=("Activar mi cuenta", url),
            nota="Si no fuiste vos, ignor&aacute; este mail: no se crea nada.")
        try:
            ok, _msg = mailer.send_email("Activa tu cuenta - Arcano Record Store",
                                         cuerpo, html, to=email)
            return bool(ok)
        except Exception:
            return False

    @app.route("/cuenta/activar/<token>")
    def cliente_activar(token):
        """Link del mail de activacion: crea/activa la cuenta y adopta las compras del email."""
        from itsdangerous import SignatureExpired, BadSignature
        try:
            data = _activar_serializer().loads(token, max_age=48 * 3600)
            email = (data.get("e") or "").strip().lower()
            pwd_hash = data.get("h") or ""
        except SignatureExpired:
            flash("El link venci&oacute; (dura 48 horas). Registrate de nuevo y te mandamos otro.", "error")
            return redirect(url_for("cliente_registro"))
        except (BadSignature, Exception):
            flash("El link no es v&aacute;lido.", "error")
            return redirect(url_for("cliente_registro"))
        if not email or not pwd_hash:
            flash("El link no es v&aacute;lido.", "error")
            return redirect(url_for("cliente_registro"))
        cli = Cliente.query.filter(db.func.lower(Cliente.email) == email).first()
        if cli and cli.password_hash:
            flash("Tu cuenta ya estaba activada. Inici&aacute; sesi&oacute;n.", "info")
            return redirect(url_for("cliente_login_page"))
        if cli is None:
            cli = Cliente(nombre=(data.get("n") or None), email=email,
                          whatsapp=(data.get("w") or None))
            db.session.add(cli)
        else:
            if not cli.nombre and data.get("n"):
                cli.nombre = data.get("n")
            if not cli.whatsapp and data.get("w"):
                cli.whatsapp = data.get("w")
        cli.password_hash = pwd_hash
        db.session.flush()
        for r in Reserva.query.filter(Reserva.cliente_id.is_(None),
                                      db.func.lower(Reserva.email) == email).all():
            r.cliente_id = cli.id
        db.session.commit()
        auth.cliente_login(cli.id, (cli.password_hash or "")[-16:])
        flash(f"Listo, tu cuenta est&aacute; activada. Bienvenido{(', ' + cli.nombre) if cli.nombre else ''}!", "success")
        return redirect(url_for("cliente_cuenta"))

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
            _existente = (Cliente.query.filter(db.func.lower(Cliente.email) == email).first()
                          if email else None)

            if errores:
                for e in errores:
                    flash(e, "error")
                return render_template(
                    "public/cliente_registro.html",
                    form={"nombre": nombre, "email": email, "whatsapp": whatsapp or ""},
                    **_public_context(),
                )

            # El email ya tiene CUENTA: no se dice en pantalla (asi nadie puede averiguar
            # que emails son clientes). Se le manda al dueno un mail con el link para
            # cambiar la contrasena, y en pantalla sale el mismo mensaje que en otros casos.
            # Tope de mails: el registro manda mails a emails ajenos (ya tenes cuenta /
            # activacion). Sin tope un bot podia bombardear a un cliente y agotar el cupo
            # de Gmail. Pasado el tope no se manda nada, pero la pantalla dice lo mismo
            # (para no revelar nada).
            _mail_ok = not _mail_cuenta_limitado(email)
            if _existente and _existente.password_hash:
                if mailer.can_send() and _mail_ok:
                    _tk = _reset_serializer().dumps({"c": _existente.id, "v": _existente.password_hash[-16:]})
                    _url = _base_publica() + url_for("cliente_reset", token=_tk)
                    _nom = _existente.nombre or ""
                    from markupsafe import escape as _esc_n
                    _mail_en_segundo_plano(
                        "Ya tenes cuenta en Arcano Record Store",
                        ("Hola %s,\n\nAlguien (seguramente vos) intento crear una cuenta con este "
                         "email, pero ya tenes una. Inicia sesion en la web, o si no te acordas la "
                         "contrasena, cambiala aca (vence en 1 hora):\n%s\n\nSi no fuiste vos, "
                         "ignora este mail." % (_nom, _url)),
                        mailer.mail_simple_html(
                            "Tu cuenta", "Ya ten&eacute;s cuenta.",
                            [f"Hola {_esc_n(_nom.split(' ')[0])}, alguien (seguramente vos) intent&oacute; crear una "
                             "cuenta con este email, pero ya ten&eacute;s una. Inici&aacute; sesi&oacute;n "
                             "en la web o, si no te acord&aacute;s la contrase&ntilde;a, cambiala con el "
                             "bot&oacute;n. El link vence en 1 hora."],
                            boton=("Cambiar mi contrase&ntilde;a", _url),
                            nota="Si no fuiste vos, ignor&aacute; este mail."),
                        _existente.email)
                from markupsafe import escape as _esc
                flash("Te mandamos un mail a "
                      f"{_esc(email)} para seguir: revis&aacute; tu correo (y la carpeta de spam).", "info")
                return redirect(url_for("cliente_login_page"))

            # El email ya tiene compras (ficha sin contrasena o pedidos sin cuenta): la
            # cuenta se activa recien cuando el dueno del email toca el link del mail.
            if _email_tiene_compras(email):
                if (not _mail_ok) or _mandar_activacion(email, password, nombre, whatsapp):
                    from markupsafe import escape as _esc
                    flash("Te mandamos un mail a "
                          f"{_esc(email)} para seguir: revis&aacute; tu correo (y la carpeta de spam).", "info")
                else:
                    flash("No pudimos mandarte el mail de confirmaci&oacute;n. Escribinos por "
                          "Instagram y te activamos la cuenta.", "error")
                return redirect(url_for("cliente_login_page"))

            cliente = Cliente(nombre=nombre, email=email, whatsapp=whatsapp)
            cliente.set_password(password)
            db.session.add(cliente)
            db.session.commit()
            auth.cliente_login(cliente.id, (cliente.password_hash or "")[-16:])
            flash(f"Cuenta creada. Bienvenido, {cliente.nombre}!", "success")
            return redirect(url_for("cliente_cuenta"))

        return render_template("public/cliente_registro.html",
                               form={"email": (request.args.get("email") or "").strip()[:200]},
                               **_public_context())

    def _reset_serializer():
        from itsdangerous import URLSafeTimedSerializer
        return URLSafeTimedSerializer(app.config.get("SECRET_KEY") or "dev-key",
                                      salt="cliente-reset-v1")

    @app.route("/cuenta/recuperar", methods=["GET", "POST"])
    def cliente_recuperar():
        """Paso 1 del 'olvide mi contrasena': el cliente pone su email y le mandamos
        un link con token que vence en 1 hora. No revelamos si el email existe."""
        if auth.cliente_actual_id():
            return redirect(url_for("cliente_cuenta"))
        if request.method == "POST":
            email = (request.form.get("email") or "").strip().lower()[:200]
            # Tope: 5 pedidos por hora desde una misma conexion y 3 por hora para un
            # mismo email. Sin esto se podia bombardear la casilla de un cliente y
            # agotar el cupo diario de Gmail (y ahi dejaban de salir las confirmaciones
            # de compra). La respuesta es la misma igual, para no revelar nada.
            import time as _tr
            _ahora_r = _tr.time()
            _limitado = False
            for _clave, _tope in ((("ip", _client_ip()), 5), (("em", email), 3)):
                _h = [t for t in _recuperar_hist.get(_clave, []) if _ahora_r - t < 3600]
                if len(_h) >= _tope:
                    _limitado = True
                _recuperar_hist[_clave] = _h
            if not _limitado:
                for _clave in (("ip", _client_ip()), ("em", email)):
                    _recuperar_hist[_clave].append(_ahora_r)
            if len(_recuperar_hist) > 5000:
                _recuperar_hist.clear()
            cli = (Cliente.query.filter(db.func.lower(Cliente.email) == email).first()
                   if email and not _limitado else None)
            if cli and cli.password_hash and mailer.can_send():
                # El token lleva un pedazo del hash actual: al cambiar la contrasena,
                # el link deja de servir (antes se podia reusar durante 1 hora).
                token = _reset_serializer().dumps({"c": cli.id, "v": cli.password_hash[-16:]})
                reset_url = _base_publica() + url_for("cliente_reset", token=token)
                try:
                    _nombre = cli.nombre or ""
                    cuerpo = ("Hola %s,\n\nPediste cambiar tu contrasena en Arcano. Entra a "
                              "este link (vence en 1 hora):\n%s\n\nSi no fuiste vos, ignora "
                              "este mail." % (_nombre, reset_url))
                    from markupsafe import escape as _esc
                    html = mailer.mail_simple_html(
                        "Tu cuenta", "Cambiar tu contrase&ntilde;a.",
                        [f"Hola {_esc(_nombre.split(' ')[0])}, pediste cambiar tu contrase&ntilde;a en Arcano "
                         "Record Store. Toc&aacute; el bot&oacute;n; el link vence en 1 hora."],
                        boton=("Cambiar mi contrase&ntilde;a", reset_url),
                        nota="Si no fuiste vos, ignor&aacute; este mail: tu contrase&ntilde;a queda igual.")
                    _mail_en_segundo_plano("Cambiar tu contrasena - Arcano Record Store",
                                           cuerpo, html, cli.email)
                except Exception:
                    pass
            flash("Si ese email tiene una cuenta, te mandamos un link para cambiar la "
                  "contrasena. Revisa tu correo (y la carpeta de spam).", "info")
            return redirect(url_for("cliente_login_page"))
        return render_template("public/cliente_recuperar.html", **_public_context())

    @app.route("/cuenta/reset/<token>", methods=["GET", "POST"])
    def cliente_reset(token):
        """Paso 2: valida el token del mail y deja poner una contrasena nueva."""
        from itsdangerous import SignatureExpired, BadSignature
        try:
            _tk = _reset_serializer().loads(token, max_age=3600)
            if not isinstance(_tk, dict):
                raise BadSignature("formato viejo")
            cid = _tk.get("c")
        except SignatureExpired:
            flash("El link venci&oacute; (dura 1 hora). Ped&iacute; uno nuevo.", "error")
            return redirect(url_for("cliente_recuperar"))
        except (BadSignature, Exception):
            flash("El link no es v&aacute;lido. Ped&iacute; uno nuevo.", "error")
            return redirect(url_for("cliente_recuperar"))
        cli = Cliente.query.get(cid)
        if not cli or not cli.password_hash:
            flash("No encontramos esa cuenta.", "error")
            return redirect(url_for("cliente_recuperar"))
        if cli.password_hash[-16:] != _tk.get("v"):
            flash("Ese link ya se us&oacute;. Si necesit&aacute;s cambiar la contrase&ntilde;a, "
                  "ped&iacute; uno nuevo.", "error")
            return redirect(url_for("cliente_recuperar"))
        if request.method == "POST":
            p1 = request.form.get("password") or ""
            p2 = request.form.get("password2") or ""
            if len(p1) < 6:
                flash("La contrase&ntilde;a debe tener al menos 6 caracteres.", "error")
            elif p1 != p2:
                flash("Las contrase&ntilde;as no coinciden.", "error")
            else:
                cli.set_password(p1)
                db.session.commit()
                auth.cliente_login(cli.id, (cli.password_hash or "")[-16:])
                flash("Listo, cambiaste tu contrase&ntilde;a. Ya est&aacute;s adentro.", "success")
                return redirect(url_for("cliente_cuenta"))
        return render_template("public/cliente_reset.html", token=token, **_public_context())

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
            auth.cliente_login(cliente.id, (cliente.password_hash or "")[-16:])
            flash(f"Hola de nuevo, {cliente.nombre}!", "success")
            next_url = _ruta_interna_segura(request.args.get("next"), "/")
            if next_url:
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
        favoritos = [fav_by_id[i] for i in fav_ids if i in fav_by_id and not fav_by_id[i].oculto_web]
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
                # La Mystery Box viaja aparte (localStorage arcano-mbox), no como
                # un id normal del carrito: si aparece aca, la ignoramos.
                if getattr(d, "es_mystery_box", False):
                    continue
                # "NO mostrar": no existe para la tienda (sale del carrito y favoritos)
                if getattr(d, "oculto_web", False):
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

            # Mystery Box: cantidad (2-5) + texto obligatorio. El precio lo resuelve
            # el servidor (nunca se confia en el front). box_intentada = el cliente
            # mando el campo (aunque venga invalido) -> hay que avisarle del error.
            box_cant_raw = (request.form.get("box_cantidad") or "").strip()
            box_texto = (request.form.get("box_texto") or "").strip()[:500]
            box_intentada = bool(box_cant_raw)
            box_precio = mystery_box_precio(box_cant_raw) if box_intentada else None
            box_cantidad = int(box_cant_raw) if (box_precio is not None) else None
            hay_box = box_precio is not None

            errores = []
            if not ids and not hay_box:
                errores.append("El carrito esta vacio.")
            if box_intentada and box_precio is None:
                errores.append("Eleg&iacute; una cantidad v&aacute;lida para la Mystery Box (2 a 5 discos).")
            if hay_box and not box_texto:
                errores.append("Dejanos 2 referencias para armarte la Mystery Box: un link, un track, un artista, un g&eacute;nero o un sello.")
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

            # Traer discos y validar disponibilidad. La ficha de la Mystery Box y los
            # discos sin precio (fichas del Drop Hunter sin completar) no se venden sueltos.
            discs = Disc.query.filter(Disc.id.in_(ids)).all() if ids else []
            by_id = {d.id: d for d in discs}
            disponibles = [by_id[i] for i in ids if i in by_id and _vendible_web(by_id[i])]

            if ids and not disponibles and not hay_box:
                errores.append("Ninguno de los discos del carrito esta disponible.")

            # Tope de discos por pedido SIN pago online (coordinar / transferencia): esos
            # pedidos apartan los discos al instante, y sin tope una sola persona (o un bot)
            # podia vaciar la tienda entera. Con Mercado Pago no hay tope: no se aparta
            # nada hasta que paga.
            _met_tope = request.form.get("metodo", "coordinar")
            _es_mp_tope = (_met_tope == "mercadopago" and mp.activo())
            if not _es_mp_tope and len(disponibles) > MAX_DISCOS_SIN_PAGO:
                errores.append(
                    f"Pod&eacute;s reservar hasta {MAX_DISCOS_SIN_PAGO} discos por pedido sin pago online. "
                    f"Ten&eacute;s {len(disponibles)} en el carrito: sac&aacute; algunos, pag&aacute; con "
                    "Mercado Pago, o escribinos por Instagram/WhatsApp y lo coordinamos.")

            # Fire Sale: los discos con descuento son exclusivos para clientes con
            # cuenta. Si el visitante no inicio sesion, no puede comprarlos.
            # Si dejo una contrasena para crear la cuenta (lo que el checkout le ofrece
            # para "desbloquear descuentos"), la cuenta se crea ANTES de cobrar y el
            # Fire Sale queda habilitado en el mismo paso (antes rebotaba con error).
            _fs_cuenta_nueva = False
            if _fire_sale_bloquea(disponibles):
                _email_n = (email or "").strip().lower()
                if (crear_cuenta and len(password_cuenta) >= 6 and _email_n and _email_valido(email)
                        and not Cliente.query.filter(db.func.lower(Cliente.email) == _email_n,
                                                     Cliente.password_hash.isnot(None)).first()
                        and not _email_tiene_compras(_email_n)):
                    _fs_cuenta_nueva = True
                elif crear_cuenta:
                    errores.append("Los discos en Fire Sale son exclusivos para clientes con cuenta y "
                                   "no pudimos crearla con ese email. Si ya ten&eacute;s cuenta, "
                                   "inici&aacute; sesi&oacute;n (link arriba) y volv&eacute;s ac&aacute; con tu carrito.")
                else:
                    errores.append("Hay discos en Fire Sale en tu carrito: son exclusivos para clientes "
                                   "con cuenta. Pon&eacute; una contrase&ntilde;a en \"Cre&aacute; tu cuenta\" "
                                   "y se crea en este mismo paso, o inici&aacute; sesi&oacute;n (link arriba).")

            # --- Entrega / envio: SOLO aplica en COMPRA (transferencia o Mercado Pago).
            # Si el cliente "reserva y coordina", el envio no se toca (se arregla por WhatsApp).
            _metodo_form = request.form.get("metodo", "coordinar")
            metodo_form_actual = _metodo_form
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
            # Envio: si hay una Mystery Box en el pedido, es GRATIS (la box siempre
            # viaja gratis y "arrastra" al resto). Solo se cobra cuando el pedido es
            # 1 o 2 discos sueltos (sin box) a Montevideo.
            if not es_compra or hay_box:
                envio_costo = 0
            else:
                envio_costo = _calcular_envio_uyu(entrega_tipo, len(disponibles))

            if errores:
                for e in errores:
                    flash(e, "error")
                return render_template(
                    "public/checkout.html",
                    ids=ids_raw,
                    form={"nombre": nombre_pila, "apellido": apellido, "whatsapp": whatsapp,
                          "email": email, "nota": nota,
                          "entrega": entrega_tipo, "envio_direccion": envio_direccion,
                          "envio_agencia": envio_agencia,
                          "box_cantidad": box_cantidad, "box_texto": box_texto},
                    **_checkout_fs(ids_raw),
                    **_public_context(),
                )

            # Fire Sale + cuenta nueva: se crea y se inicia sesion antes de armar el pedido.
            if _fs_cuenta_nueva:
                try:
                    _nc = Cliente(nombre=nombre, email=_email_n, whatsapp=whatsapp or None)
                    _nc.set_password(password_cuenta)
                    db.session.add(_nc)
                    db.session.commit()
                    auth.cliente_login(_nc.id, (_nc.password_hash or "")[-16:])
                    crear_cuenta = False   # ya esta creada: el bloque de abajo no la repite
                    flash(f"Te creamos una cuenta, {_nc.nombre}: ya ten&eacute;s el precio de Fire Sale.", "success")
                except Exception:
                    db.session.rollback()
                    flash("No pudimos crear tu cuenta. Prob&aacute; de nuevo o inici&aacute; sesi&oacute;n.", "error")
                    return redirect(url_for("public_checkout", ids=ids_raw))

            # Aviso si algun disco se agoto entre "agregar" y "confirmar"
            n_descartados = sum(1 for i in ids if i in by_id and not _vendible_web(by_id[i]))
            if n_descartados:
                flash(f"{n_descartados} disco(s) del carrito ya no estaban disponibles y se quitaron del pedido.", "info")

            es_transf_checkout = (_metodo_form == "transferencia"
                                  and Setting.get("transferencia_activa", "0") == "1")
            es_mp_checkout = (_metodo_form == "mercadopago" and mp.activo())

            # Un doble click puede mandar DOS pedidos a la vez: si los dos miran la
            # base antes de que el primero se guarde, los dos pasan. Este candado por
            # cliente los pone en fila, asi el segundo ya ve al primero y lo descarta.
            _lock_pedido = _lock_checkout((email or '').strip().lower())
            _lock_pedido.acquire()
            try:
                # --- Anti doble click (red del servidor) ---
                # Si el MISMO cliente mando un pedido IDENTICO hace segundos, es un doble
                # click o un reintento: no creamos otro pedido, lo mandamos al comprobante
                # del primero. (El boton ya se bloquea en el navegador; esto es el respaldo.)
                # No aplica a Mercado Pago: ahi cada intento necesita su propia preferencia
                # de pago, y un pedido sin pagar no hace dano.
                if metodo_form_actual != "mercadopago":
                    _ids_nuevos = sorted(d.id for d in disponibles)
                    _desde = datetime.utcnow() - timedelta(seconds=90)
                    _previas = (Reserva.query
                                .filter(Reserva.fecha >= _desde)
                                .filter(db.func.lower(Reserva.email) == (email or "").lower())
                                .order_by(Reserva.id.desc())
                                .limit(5).all())
                    for _p in _previas:
                        _ids_p = sorted(i.disc_id for i in _p.items_efectivos if i.disc_id)
                        _box_p = next((i.mbox_cantidad for i in _p.items_efectivos if i.es_mystery_box), None)
                        if _ids_p == _ids_nuevos and _box_p == box_cantidad:
                            session["ultima_reserva_ok"] = _p.id
                            session["ultima_reserva_multi"] = True
                            return redirect(url_for("reserva_confirmada"))

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

                # El precio a cobrar se anota ANTES de apartar: un disco ya Reservado deja
                # de estar "en Fire Sale" y se cobraria el precio normal.
                _precio_snap = {d.id: _precio_a_cobrar(d) for d in disponibles}

                # Pedido sin pago online: apartar los discos YA, en la misma transaccion
                # que crea el pedido. El apartado es atomico: si otro cliente se llevo un
                # disco en el medio (compra simultanea), ese disco sale de este pedido.
                if not es_mp_checkout:
                    _motivo_ap = ("transferencia (comprobante subido)" if es_transf_checkout
                                  else "a coordinar")
                    _perdidos = _apartar_para_pedido(reserva, disponibles, _motivo_ap)
                    if _perdidos:
                        _ids_perd = {d.id for d in _perdidos}
                        disponibles = [d for d in disponibles if d.id not in _ids_perd]
                        if not disponibles and not hay_box:
                            db.session.rollback()
                            flash("Justo otra persona confirm&oacute; esos discos antes que vos: "
                                  "ya no est&aacute;n disponibles.", "error")
                            return redirect(url_for("public_carrito"))
                        flash(f"{len(_perdidos)} disco(s) los acaba de confirmar otra persona y "
                              "se quitaron de tu pedido.", "info")
                        if es_compra and not hay_box:
                            envio_costo = _calcular_envio_uyu(entrega_tipo, len(disponibles))

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
                        precio_uyu_snap=_precio_snap.get(d.id, _precio_a_cobrar(d)),
                    )
                    db.session.add(item)

                # Mystery Box: item SIN disc_id (no descuenta stock; los discos se asignan
                # a mano despues). Precio resuelto por el servidor, texto del cliente guardado.
                if hay_box:
                    db.session.add(ReservaItem(
                        reserva_id=reserva.id,
                        disc_id=None,
                        artista_snap="Mystery Box",
                        titulo_snap=f"Mystery Box · {box_cantidad} discos",
                        precio_uyu_snap=box_precio,
                        mbox_cantidad=box_cantidad,
                        mbox_texto=box_texto,
                    ))

                db.session.commit()
                _reserva_registrar(ip_reserva)
            finally:
                _lock_pedido.release()

            # Auto-cuenta: si dejo una contrasena y el email esta libre, le creamos la cuenta
            # (asi puede seguir sus pedidos sin registrarse aparte). Best-effort: nunca rompe la reserva.
            if crear_cuenta and len(password_cuenta) >= 6 and email:
                email_norm = email.strip().lower()
                existente = Cliente.query.filter_by(email=email_norm).first()
                if existente and existente.password_hash:
                    flash("Ya tenias una cuenta con ese email. Inicia sesion para ver tus pedidos.", "info")
                elif _email_tiene_compras(email_norm, excluir_reserva_id=reserva.id):
                    # Ese email ya tiene compras anteriores: la cuenta se activa desde el
                    # link del mail (que solo le llega al dueno del email).
                    if _mandar_activacion(email_norm, password_cuenta, nombre, whatsapp):
                        flash("Te mandamos un mail para activar tu cuenta y ver todas tus "
                              "compras.", "info")
                else:
                    try:
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
                        auth.cliente_login(nuevo_cliente.id, (nuevo_cliente.password_hash or "")[-16:])
                        flash(f"Te creamos una cuenta, {nuevo_cliente.nombre}. Vas a poder seguir tus pedidos.", "success")
                    except Exception:
                        db.session.rollback()

            # Metodo de pago elegido en el checkout
            metodo = request.form.get("metodo", "coordinar")
            if metodo == "transferencia" and Setting.get("transferencia_activa", "0") == "1":
                reserva.metodo_pago = "transferencia"
                _guardar_comprobante(reserva, request.files.get("comprobante"))
                # Los discos ya quedaron apartados (Reservado) al crear el pedido, para
                # que nadie mas los compre. NO se marcan Vendido ni entran a la caja
                # hasta que Facundo confirme que la plata llego (marca la reserva
                # Pagada). El comprobante es una foto del cliente, no una confirmacion
                # real del banco.
                db.session.commit()
            elif metodo == "mercadopago" and mp.activo():
                reserva.metodo_pago = "mercadopago"
                db.session.commit()
                _extras = ([{"title": f"Mystery Box · {box_cantidad} discos",
                             "unit_price": float(box_precio)}] if hay_box else None)
                url_pago = _mp_iniciar_pago(reserva, disponibles, extras=_extras)
                if url_pago:
                    return redirect(url_pago)
                # MP fallo: el pedido pasa a "coordinar" (antes quedaba marcado como
                # mercadopago y el aviso al admin y el push decian MP).
                reserva.metodo_pago = "coordinar"
                reserva.mp_status = None
                _perd_mp = _apartar_para_pedido(reserva, disponibles, "a coordinar")
                if _perd_mp:
                    # Discos que otro cliente confirmo en el medio: salen de este pedido
                    _ids_pm = {d.id for d in _perd_mp}
                    for _it in list(reserva.items):
                        if _it.disc_id in _ids_pm:
                            db.session.delete(_it)
                    disponibles = [d for d in disponibles if d.id not in _ids_pm]
                db.session.commit()
                flash("No pudimos iniciar el pago online. Te dejamos la reserva para coordinar por mensaje.", "error")
            else:
                # "Reservar y coordinar": la pantalla y el mail dicen "queda apartado para
                # vos", asi que el disco se aparta de verdad (ya se hizo al crear el
                # pedido). Si no se concreta, cancelas la reserva y vuelve a Disponible solo.
                reserva.metodo_pago = "coordinar"
                db.session.commit()

            # Email al admin (best-effort, no rompe si falla) — con el comprobante adjunto si lo hay.
            # (Los mails se arman a partir de un disco; una orden de SOLO Mystery Box no
            # tiene disco, asi que ahi el aviso viaja por push y por la pantalla de gracias.)
            if mailer.is_configured() and (disponibles or reserva.items_efectivos):
                comp_path = None
                if reserva.comprobante_file:
                    comp_path = os.path.join(app.config["COMPROBANTES_DIR"], reserva.comprobante_file)
                try:
                    mailer.send_reserva_notification(reserva, (disponibles[0] if disponibles else None),
                                                     comprobante_path=comp_path)
                except Exception:
                    pass

            # Confirmacion al cliente (si dejo email). Transferencia = ya pago ->
            # "gracias por tu compra". Coordinar = reserva a coordinar el pago.
            if reserva.email and mailer.can_send() and (disponibles or reserva.items_efectivos):
                try:
                    if reserva.metodo_pago == "transferencia":
                        mailer.send_compra_confirmacion_cliente(reserva, disponibles, metodo="transferencia")
                    else:
                        mailer.send_reserva_confirmacion_cliente_multi(reserva, disponibles)
                except Exception:
                    pass

            # Aviso push al celu del admin (si activo los avisos en su celular).
            try:
                _met = {"transferencia": "transferencia", "mercadopago": "Mercado Pago"}.get(
                    reserva.metodo_pago or "", "a coordinar")
                _box_txt = (" · 🎁 Mystery Box (%s discos)" % box_cantidad) if hay_box else ""
                _enviar_push(
                    "Nueva reserva #%s" % reserva.id,
                    "%s reservó %s ítem(s) · $%s · %s%s" % (
                        reserva.nombre, reserva.cantidad_discos,
                        "{:,.0f}".format(reserva.total_uyu or 0).replace(",", "."), _met, _box_txt),
                    "/admin/reservas/%s" % reserva.id,
                )
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
            **_checkout_fs(ids_raw),
            **_public_context(),
        )

    @app.route("/sitemap.xml")
    def sitemap_xml():
        """Sitemap con la home, sobre y todos los discos publicados."""
        from xml.sax.saxutils import escape
        from datetime import datetime as _dt
        urls = []
        # Home, sobre y legales
        urls.append((url_for("home", _external=True), "1.0", None, None))
        urls.append((url_for("public_sobre", _external=True), "0.6", None, None))
        urls.append((url_for("public_legal", _external=True), "0.2", None, None))
        # Todas las fichas publicas: disponibles, pre-orders, reservadas y VENDIDAS (la
        # ficha vendida sigue captando busquedas del disco y muestra similares). Sin las
        # ocultas ni las que no tienen precio. Las copias del mismo disco comparten
        # direccion: va una sola vez.
        _prio = {"Disponible": "0.8", "Pre-order": "0.7", "Encargo": "0.5",
                 "Reservado": "0.6", "Vendido": "0.4"}
        discs = (Disc.query.filter(Disc.estado.in_(list(_prio)), Disc.oculto_web.isnot(True))
                 .filter((Disc.precio_uyu > 0) | (Disc.es_mystery_box.is_(True)))
                 .order_by(Disc.id).all())
        vistos = {}
        for d in discs:
            loc = url_for("public_disc", disc_id=d.id, _external=True)
            lastmod = (d.fecha_modificacion or d.fecha_ingreso or _dt.utcnow()).strftime("%Y-%m-%d")
            img = (url_for("serve_cover", filename=d.cover_local, _external=True) if d.cover_local
                   else (d.cover_url if (d.cover_url or "").startswith("http") else None))
            prev = vistos.get(loc)
            if prev is None or float(_prio[d.estado]) > float(prev[1]):
                vistos[loc] = (loc, _prio[d.estado], lastmod, img)
        urls += list(vistos.values())

        body = ['<?xml version="1.0" encoding="UTF-8"?>',
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
                'xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">']
        for loc, pri, lastmod, img in urls:
            entry = f"<url><loc>{escape(loc)}</loc>"
            if lastmod:
                entry += f"<lastmod>{lastmod}</lastmod>"
            entry += f"<priority>{pri}</priority>"
            if img:
                entry += f"<image:image><image:loc>{escape(img)}</image:loc></image:image>"
            entry += "</url>"
            body.append(entry)
        body.append("</urlset>")
        return "\n".join(body), 200, {"Content-Type": "application/xml; charset=utf-8"}

    # ----------------------------------------------------------------------
    # ADMIN: bandeja de reservas
    # ----------------------------------------------------------------------

    @app.route("/admin/reservas")
    @auth.admin_required
    def admin_reservas():
        # Entrar aca marca todas las reservas actuales como "vistas": limpia el badge
        # de aviso del nav (los pendientes se siguen viendo en la propia lista).
        try:
            from models import Setting as _S
            _maxid = db.session.query(db.func.max(Reserva.id)).scalar() or 0
            if int(_S.get("reservas_visto_hasta_id", "0") or 0) < _maxid:
                _S.set("reservas_visto_hasta_id", str(_maxid))
        except Exception:
            pass
        filtro_estado = (request.args.get("estado") or "").strip()
        f_q = (request.args.get("q") or "").strip()
        query = Reserva.query.options(selectinload(Reserva.items).selectinload(ReservaItem.disc), selectinload(Reserva.cliente)).order_by(Reserva.fecha.desc())
        if filtro_estado == "pendiente_entrega":
            # Todo lo que todavia no entregaste (menos las canceladas), sin importar si
            # esta pago o no. Es la cola de "que me falta entregar".
            query = query.filter(Reserva.entregado.isnot(True), Reserva.estado != "Cancelada")
        elif filtro_estado:
            query = query.filter(Reserva.estado == filtro_estado)
        reservas = visible_records(query.all(), 'reservas')

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
        contador_pend_entrega = Reserva.query.filter(
            Reserva.entregado.isnot(True), Reserva.estado != "Cancelada").count()

        return render_template(
            "admin_reservas.html",
            unidades=unidades,
            estados=ESTADOS_RESERVA,
            contadores=contadores,
            contador_pend_entrega=contador_pend_entrega,
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
                fecha=datetime.utcnow(), fecha_es_utc=True, pagado=(estado == "Pagada"),
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
                    # Pre-order / Encargo: el disco todavia no llego. Mientras no se pague,
                    # sigue en su estado (reservarlo lo meteria en la contabilidad como stock
                    # y "por cobrar"); se le anota el cliente para saber de quien es.
                    if d.estado in ("Pre-order", "Encargo") and estado != "Pagada":
                        if d.estado == "Encargo" and cli and not d.cliente_id:
                            d.cliente_id = cli.id
                        # Pendiente: el pre-order queda tomado por esta reserva (deja de
                        # venderse en la web). Cancelada: no se toma.
                        if estado == "Pendiente" and not d.venta_grupo:
                            d.venta_grupo = grupo
                        continue
                    if d.estado != estado_disco and d.estado != "Vendido":
                        register_status_change(d, estado_disco,
                                               nota=f"Reserva manual #{reserva.id} -> {estado}")
                    # Disco que ya habia vendido (ej: una compra vieja que se carga como
                    # registro): se le atribuye a este cliente si no tenia comprador.
                    if d.estado == "Vendido" and cli and not d.cliente_id:
                        d.cliente_id = cli.id
                    # Si la reserva es una venta (Pagada), registrar canal/metodo/grupo
                    # aunque el disco ya estuviera Vendido (lo marcaste antes de la reserva).
                    if estado == "Pagada":
                        if not d.canal_venta:
                            d.canal_venta = canal
                        if metodo and not d.metodo_pago_venta:
                            d.metodo_pago_venta = metodo
                        # Siempre (aunque sea UN disco): el grupo r<id> es lo que dice
                        # "esta venta es de esta reserva". Sin el, cancelar despues una
                        # reserva de un solo disco lo dejaba Vendido.
                        if not d.venta_grupo:
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
                                    origen=("web" if (reserva.canal or "web") == "web" else "manual"),
                                    seguro=True)
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
                            # No tocar un disco que vendio o aparto OTRA reserva/venta: bajarlo
                            # de Vendido borraria esa venta en silencio, y liberar un Reservado
                            # ajeno dejaria a la venta un disco apartado para otra persona.
                            if (disco.estado in ("Vendido", "Reservado")
                                    and disco.estado != estado_disco
                                    and not _disco_es_de_reserva(disco, reserva)):
                                continue
                            # Pre-order / Encargo: sigue en su estado hasta que se pague
                            # (no entra a la contabilidad como stock ni como por cobrar).
                            if disco.estado in ("Pre-order", "Encargo") and estado_disco != "Vendido":
                                if (disco.estado == "Encargo" and reserva.cliente_id
                                        and not disco.cliente_id):
                                    disco.cliente_id = reserva.cliente_id
                                if estado_disco == "Disponible":
                                    # Cancelada: el pre-order se suelta y vuelve a la venta.
                                    if disco.venta_grupo == f"r{reserva.id}":
                                        disco.venta_grupo = None
                                elif not disco.venta_grupo:
                                    disco.venta_grupo = f"r{reserva.id}"
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
                                # Discos de una Mystery Box: su parte del precio de la caja.
                                if getattr(item, "precio_uyu_snap", None):
                                    disco.precio_venta_uyu = item.precio_uyu_snap
                                elif disco.id in _reparto_box(reserva):
                                    disco.precio_venta_uyu = _reparto_box(reserva)[disco.id]
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

            elif accion == "editar":
                # Editar los datos de la reserva a mano (igual que al cargar una manual):
                # contacto, canal, metodo, y una NOTA libre para anotar el estado / la
                # coordinacion ("ya hable, retira el sabado", "falta que pase", etc.).
                _nom = (request.form.get("nombre") or "").strip()
                if _nom:
                    reserva.nombre = _nom[:200]
                reserva.whatsapp = (request.form.get("whatsapp") or "").strip()[:50]
                reserva.email = ((request.form.get("email") or "").strip()[:200] or None)
                reserva.nota = ((request.form.get("nota") or "").strip() or None)
                _canal = (request.form.get("canal") or "").strip()
                if _canal in dict(_CANALES_RESERVA):
                    reserva.canal = _canal
                _met = (request.form.get("metodo_pago") or "").strip()
                if _met in dict(_METODOS_RESERVA):
                    reserva.metodo_pago = (_met or None)
                db.session.commit()
                flash("Datos de la reserva actualizados.", "success")

            elif accion == "asignar_discos":
                # Mystery Box: asignar los discos reales que le vas a mandar al cliente.
                # Se agregan como items, se marcan Vendido y se descuenta el stock
                # (reusa el mismo camino de "marcar como vendido"). El ingreso del pedido
                # es el precio de la box (ya cobrado): se reparte entre los discos
                # asignados para que la contabilidad lo cuente UNA sola vez, con el costo
                # real de cada disco. Los items de la caja llevan precio 0 (el precio ya
                # lo muestra el item de la Mystery Box) para no duplicar el total.
                from models import Cliente
                ids_asig = [int(x) for x in request.form.getlist("disc_ids") if str(x).isdigit()]
                nuevos = Disc.query.filter(Disc.id.in_(ids_asig)).all() if ids_asig else []
                ya = {it.disc_id for it in reserva.items_efectivos if it.disc_id}
                nuevos = [d for d in nuevos
                          if d.id not in ya and not getattr(d, "es_mystery_box", False)]
                if not nuevos:
                    flash("Eleg&iacute; al menos un disco (nuevo) para asignar.", "warning")
                else:
                    box_total = sum((it.precio_uyu_snap or 0)
                                    for it in reserva.items_efectivos if it.es_mystery_box)
                    cli = Cliente.query.get(reserva.cliente_id) if reserva.cliente_id else None
                    n = len(nuevos)
                    if box_total > 0:
                        base = round(box_total / n)
                        shares = [base] * n
                        shares[0] += round(box_total - base * n)   # el remanente al primero
                    else:
                        shares = [(d.precio_mostrar or d.precio_uyu or 0) for d in nuevos]
                    grupo = f"r{reserva.id}"
                    # Caja todavia NO pagada: los discos quedan Reservados (apartados para
                    # este cliente) y pasan a Vendidos recien al marcar la reserva Pagada.
                    # Antes se vendian al asignarlos y la caja/contabilidad mostraban plata
                    # que todavia no habia entrado.
                    _pagada = (reserva.estado == "Pagada") or bool(reserva.pagado)
                    if not _pagada:
                        for d, share in zip(nuevos, shares):
                            db.session.add(ReservaItem(
                                reserva_id=reserva.id, disc_id=d.id,
                                artista_snap=d.artista, titulo_snap=d.titulo,
                                precio_uyu_snap=(0 if box_total > 0 else share),
                            ))
                            if d.estado not in ("Vendido", "Reservado"):
                                register_status_change(
                                    d, "Reservado", nota=f"Mystery Box reserva #{reserva.id} (apartado)")
                            if cli and not d.cliente_id:
                                d.cliente_id = cli.id
                        db.session.commit()
                        flash(f"Asignaste {n} disco(s) a la caja: quedaron Reservados. Pasan a "
                              "Vendidos cuando marques la reserva como Pagada.", "success")
                        return redirect(url_for("admin_reserva_detail", reserva_id=reserva.id))
                    for d, share in zip(nuevos, shares):
                        db.session.add(ReservaItem(
                            reserva_id=reserva.id, disc_id=d.id,
                            artista_snap=d.artista, titulo_snap=d.titulo,
                            # Con box, el precio ya lo lleva el item de la caja -> 0 aca.
                            precio_uyu_snap=(0 if box_total > 0 else share),
                        ))
                        if d.estado != "Vendido":
                            register_status_change(
                                d, "Vendido", nota=f"Mystery Box reserva #{reserva.id}")
                        d.precio_venta_uyu = share
                        d.stock = max(0, (d.stock or 1) - 1)
                        if not d.canal_venta:
                            d.canal_venta = reserva.canal or "web"
                        if not d.metodo_pago_venta:
                            _rm = (reserva.metodo_pago or "").lower()
                            if _rm == "mercadopago" or reserva.mp_status == "approved":
                                d.metodo_pago_venta = "mercadopago"
                            elif _rm in ("transferencia", "efectivo", "otro"):
                                d.metodo_pago_venta = _rm
                        if not d.venta_grupo:
                            d.venta_grupo = grupo
                        if cli and not d.cliente_id:
                            d.cliente_id = cli.id
                    # Si la caja se asigna en varias tandas, el precio de la caja se
                    # reparte entre TODOS sus discos (los de antes y los nuevos). Antes cada
                    # tanda se repartia la caja entera y el ingreso se contaba doble.
                    if box_total > 0:
                        db.session.flush()
                        db.session.expire(reserva)
                        for _did, _share in _reparto_box(reserva).items():
                            _dd = Disc.query.get(_did)
                            if _dd is not None and _dd.estado == "Vendido":
                                _dd.precio_venta_uyu = _share
                    db.session.commit()
                    flash(f"Asignaste {n} disco(s) a la reserva: quedaron Vendidos y se descont&oacute; el stock.", "success")

            elif accion == "separar_item":
                # Separar UN disco de la reserva: pasa a una reserva nueva (mismos datos del
                # cliente y mismo estado) o a otra reserva del mismo cliente en el mismo
                # estado. El disco sigue como esta (Reservado / Encargo / Vendido) pero
                # ahora "pertenece" a la otra reserva: cancelarla o borrarla lo libera, y
                # cancelar esta ya no lo toca. La sena del disco viaja con el disco.
                from models import ReservaItem, StatusHistory
                try:
                    _item_id = int(request.form.get("item_id") or 0)
                except ValueError:
                    _item_id = 0
                item = ReservaItem.query.filter_by(id=_item_id, reserva_id=reserva.id).first()
                _n_items = ReservaItem.query.filter_by(reserva_id=reserva.id).count()
                destino_raw = (request.form.get("destino") or "nueva").strip()
                dest = None
                if item is None or not item.disc_id or getattr(item, "es_mystery_box", False):
                    flash("Ese disco no se puede separar.", "error")
                elif _n_items < 2:
                    flash("La reserva tiene un solo disco: no hay nada para separar.", "warning")
                elif reserva.estado == "Cancelada":
                    flash("La reserva esta cancelada: no hace falta separar nada.", "warning")
                else:
                    if destino_raw == "nueva":
                        dest = Reserva(
                            nombre=reserva.nombre, whatsapp=reserva.whatsapp or "",
                            email=reserva.email, cliente_id=reserva.cliente_id,
                            estado=reserva.estado, pagado=bool(reserva.pagado),
                            metodo_pago=reserva.metodo_pago, canal=reserva.canal or "web",
                            nota=f"Separada de la reserva #{reserva.id}", fecha_es_utc=True,
                        )
                        db.session.add(dest)
                        db.session.flush()
                    else:
                        try:
                            dest = Reserva.query.get(int(destino_raw))
                        except ValueError:
                            dest = None
                        if (dest is None or dest.id == reserva.id or dest.estado != reserva.estado
                                or dest.estado == "Cancelada"):
                            flash("Esa reserva no sirve de destino (tiene que estar en el mismo "
                                  "estado que esta).", "error")
                            dest = None
                if dest is not None:
                    d = item.disco
                    _era_suyo = bool(d) and _disco_es_de_reserva(d, reserva)
                    item.reserva = dest   # por la relacion (si no, el 'borrar huerfanos' lo perdia)
                    if d is not None and _era_suyo:
                        _viejo = f"r{reserva.id}"
                        if d.venta_grupo == _viejo or (d.estado == "Vendido" and not d.venta_grupo):
                            d.venta_grupo = f"r{dest.id}"
                        if d.estado == "Reservado":
                            db.session.add(StatusHistory(
                                disc_id=d.id, estado="Reservado",
                                nota=f"Reserva #{dest.id} - separada de la reserva #{reserva.id}"))
                    # Reservas viejas de un solo disco guardan el disco tambien en la reserva
                    if d is not None and reserva.disc_id == d.id:
                        _otro = (ReservaItem.query.filter(ReservaItem.reserva_id == reserva.id,
                                                          ReservaItem.id != item.id,
                                                          ReservaItem.disc_id.isnot(None)).first())
                        reserva.disc_id = _otro.disc_id if _otro else None
                    db.session.commit()
                    flash(f"Separaste {item.artista_snap or ''} - {item.titulo_snap or ''}: ahora esta "
                          f"en la reserva #{dest.id}. El resto sigue en la #{reserva.id}.", "success")
                    return redirect(url_for("admin_reserva_detail", reserva_id=dest.id))
                return redirect(url_for("admin_reserva_detail", reserva_id=reserva.id))

            elif accion == "delete":
                # Revertir los discos de la reserva: los Reservados Y los Vendidos vuelven
                # a Disponible y se les limpian los datos de venta. Sin esto, borrar una
                # reserva pagada dejaba el disco 'Vendido' sumando plata y lealtad fantasma.
                # Solo se revierten los discos cuyo estado lo puso ESTA reserva. Si el disco
                # despues se vendio por otra via (Instagram, otra reserva) o lo apartaste
                # para otra persona, no se toca: antes borrar un pedido abandonado borraba
                # esa venta real de caja, contabilidad y lealtad.
                revertidos, intactos = [], []
                for item in reserva.items_efectivos:
                    disco = item.disco
                    # Pre-order que habia tomado este pedido: vuelve a ofrecerse.
                    if (disco and disco.estado in ("Pre-order", "Encargo")
                            and disco.venta_grupo == f"r{reserva.id}"):
                        disco.venta_grupo = None
                        continue
                    if not disco or disco.estado not in ("Reservado", "Vendido"):
                        continue
                    if _disco_es_de_reserva(disco, reserva):
                        _desvender_disco(disco, nota=f"Reserva #{reserva.id} eliminada")
                        revertidos.append(disco)
                    else:
                        intactos.append(disco)
                db.session.delete(reserva)
                db.session.commit()
                msg = "Reserva eliminada."
                if revertidos:
                    msg += f" {len(revertidos)} disco(s) volvieron a Disponible."
                if intactos:
                    msg += (f" {len(intactos)} disco(s) no se tocaron porque estan vendidos o "
                            "apartados por otra venta: " + ", ".join(
                                f"{d.artista} - {d.titulo}" for d in intactos[:4]) + ".")
                flash(msg, "success")
                return redirect(url_for("admin_reservas"))

            return redirect(url_for("admin_reserva_detail", reserva_id=reserva.id))

        # Detectar conflictos REALES antes de confirmar. Que un disco de esta reserva este
        # Reservado es NORMAL (lo tenes apartado para este pedido), NO es conflicto. El unico
        # riesgo real es que ya este VENDIDO por otra via (otra reserva o venta manual): eso
        # seria doble venta. Si hubiera doble reserva, salta cuando el otro lo venda.
        conflictos = []
        if reserva.estado == "Pendiente":
            _grp = f"r{reserva.id}"
            for item in reserva.items_efectivos:
                disco = item.disco
                if (disco and disco.estado in ("Pre-order", "Encargo") and disco.venta_grupo
                        and disco.venta_grupo != _grp):
                    # Pre-order (una sola copia) que ya tomo OTRO pedido.
                    conflictos.append(item)
                    continue
                if disco and disco.estado == "Vendido" and (disco.venta_grupo or "") != _grp:
                    # Si ya lo compro ESTE mismo cliente (una compra vieja cargada como
                    # registro), no es una doble venta.
                    if reserva.cliente_id and disco.cliente_id == reserva.cliente_id:
                        continue
                    conflictos.append(item)

        _par = mailer.RESERVA_ESTADO_MENSAJES.get(reserva.estado)
        mail_subject = f"{_par[0]} - pedido #{reserva.id} - Arcano Record Store" if _par else ""
        from models import Cliente
        clientes = Cliente.query.order_by(Cliente.nombre.asc()).all()
        cliente_actual_reserva = Cliente.query.get(reserva.cliente_id) if reserva.cliente_id else None
        # Mystery Box: si la reserva tiene una caja, pasar el catalogo asignable para
        # el picker de "asignar discos" (disponibles, sin la propia box).
        tiene_mbox = any(it.es_mystery_box for it in reserva.items_efectivos)
        discos_asignables = []
        if tiene_mbox:
            discos_asignables = (
                Disc.query
                .filter(Disc.estado == "Disponible", Disc.stock > 0)
                .filter((Disc.es_mystery_box.is_(None)) | (Disc.es_mystery_box == False))  # noqa: E712
                .order_by(Disc.artista.asc(), Disc.titulo.asc())
                .all()
            )
        # Reservas del mismo cliente en el mismo estado: destino posible al separar un disco
        destinos_separar = []
        if reserva.estado != "Cancelada":
            _conds = []
            if reserva.cliente_id:
                _conds.append(Reserva.cliente_id == reserva.cliente_id)
            if (reserva.whatsapp or "").strip():
                _conds.append(Reserva.whatsapp == reserva.whatsapp)
            if _conds:
                destinos_separar = (Reserva.query
                                    .filter(Reserva.id != reserva.id, Reserva.estado == reserva.estado)
                                    .filter(or_(*_conds))
                                    .order_by(Reserva.id.desc()).limit(20).all())
        return render_template(
            "admin_reserva_detail.html",
            reserva=reserva,
            estados=ESTADOS_RESERVA,
            conflictos=conflictos,
            destinos_separar=destinos_separar,
            tc=Setting.get_tc(),
            texto_mail_default=mailer.texto_estado_default(reserva.estado),
            mail_subject=mail_subject,
            clientes=clientes,
            cliente_actual_reserva=cliente_actual_reserva,
            canales=_CANALES_RESERVA,
            metodos=_METODOS_RESERVA,
            tiene_mbox=tiene_mbox,
            discos_asignables=discos_asignables,
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
            # Un campo de texto que NO viene en el formulario no se toca. Antes se
            # guardaba vacio: guardar Ajustes borraba el video del banner (que vive en
            # otro formulario) sin avisar.
            if key not in request.form:
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

    @app.route("/admin/settings/diseno", methods=["POST"])
    @auth.admin_required
    def settings_update_diseno():
        """Elige el diseno de la tienda (Actual / H / M / S) y
        el modo de los carruseles. Ruta propia para no pisar el form grande."""
        diseno = (request.form.get("public_diseno") or "").strip()
        if diseno not in ("", "tocadiscos", "fundaviva", "picture"):
            diseno = ""
        carr = (request.form.get("public_carrusel_modo") or "compacto").strip()
        if carr not in ("compacto", "normal", "carrete"):
            carr = "compacto"
        Setting.set("public_diseno", diseno)
        Setting.set("public_carrusel_modo", carr)
        modo = (request.form.get("public_modo_defecto") or "oscuro").strip()
        Setting.set("public_modo_defecto", modo if modo in ("oscuro", "claro") else "oscuro")
        flash("Diseño de la tienda actualizado.", "success")
        return redirect(url_for("settings_page") + "#diseno-tienda")

    @app.route("/admin/settings/diseno-admin", methods=["POST"])
    @auth.admin_required
    def settings_update_diseno_admin():
        """Elige el diseno del PANEL DE ADMIN (Actual / Funda / Consola / Estudio).
        Ruta propia para no pisar el form grande de Ajustes."""
        d = (request.form.get("admin_diseno") or "").strip()
        Setting.set("admin_diseno", d if d in ("", "funda", "consola", "estudio") else "")
        flash("Diseño del admin actualizado.", "success")
        resp = redirect(url_for("settings_page") + "#diseno-admin")
        resp.delete_cookie("arcano_atema", path="/admin")   # sale de la vista previa
        return resp

    @app.route("/admin/fire-sale/avisar", methods=["POST"])
    @auth.admin_required
    def fire_sale_avisar():
        """Manda el mail de aviso del Fire Sale a todos los clientes con email."""
        if not mailer.can_send():
            flash("El correo no esta configurado: no se pudo enviar el aviso.", "error")
            return redirect(url_for("settings_page"))
        cfg = fire_sale_config()
        if not cfg.get("vigente"):
            flash("El Fire Sale no est&aacute; activo (o ya venci&oacute; el reloj): activalo y "
                  "guardalo antes de avisar. No se mand&oacute; ning&uacute;n mail.", "error")
            return redirect(url_for("settings_page"))
        pct = cfg.get("pct") or int(float(Setting.get("fire_sale_pct", "10") or 10))
        if (cfg.get("alcance") or "todo") != "todo" and not (
                Disc.query.filter(Disc.en_fire_sale.is_(True), Disc.estado == "Disponible",
                                  Disc.stock > 0).first()):
            flash("El Fire Sale es solo para discos marcados, pero no hay ninguno disponible "
                  "marcado. No se mand&oacute; ning&uacute;n mail.", "error")
            return redirect(url_for("settings_page"))
        emails = [c.email for c in Cliente.query
                  .filter(Cliente.email.isnot(None), Cliente.email != "").all()]
        if not emails:
            flash("No hay clientes con email para avisar.", "warning")
            return redirect(url_for("settings_page"))

        import html as _html
        mensaje = (Setting.get("fire_sale_mensaje", "") or "").strip()
        url_tienda = mailer.SITE_URL
        alcance = cfg.get("alcance") or "todo"
        fin_txt = mailer.texto_fin_fire_sale(cfg.get("fin"), now_uy())
        asunto = f"{pct}% OFF para clientes · Fire Sale en Arcano"
        texto = (
            f"Activamos un Fire Sale con {pct}% de descuento, exclusivo para clientes con cuenta.\n"
            + (_html.unescape(fin_txt) + ".\n" if fin_txt else "") + "\n"
            + (mensaje + "\n\n" if mensaje else "")
            + "Entra a la tienda e inicia sesion para ver los precios con descuento:\n"
            + url_tienda + "\n\nArcano Record Store"
        )
        # Discos para mostrar en el mail (hasta 4, con portada): los marcados para el Fire
        # Sale si es "solo algunos"; si es toda la tienda, primero los Staff Picks y despues
        # los mas nuevos.
        _q = (Disc.query.filter(Disc.estado == "Disponible", Disc.stock > 0, Disc.precio_uyu > 0)
              .filter(Disc.es_mystery_box.isnot(True)).filter(Disc.oculto_web.isnot(True)))
        if alcance != "todo":
            _q = _q.filter(Disc.en_fire_sale.is_(True))
        _cands = _q.order_by(Disc.es_tip.desc(), Disc.fecha_ingreso.desc(), Disc.id.desc()).limit(40).all()
        discos_mail = [d for d in _cands if (d.cover_local or d.cover_url)][:4]
        html = mailer.mail_fire_sale_html(pct, fin_txt, alcance, discos_mail, mensaje=mensaje,
                                          url_tienda=url_tienda)
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
                    # Snapshot 0 = disco asignado a una Mystery Box: el precio ya lo
                    # lleva el item de la caja (antes caia al precio del disco y la box
                    # contaba doble en el gasto del cliente y en la lealtad).
                    "precio": (it.precio_uyu_snap if it.precio_uyu_snap is not None
                               else (d.precio_mostrar if d else 0)) or 0,
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
                _pre = (it.precio_uyu_snap if it.precio_uyu_snap is not None
                        else (_d.precio_mostrar if _d else 0)) or 0
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
            "admin_clientes.html", clientes=visible_records(clientes, 'clientes'), compras=compras,
            activas=activas, q=q, total=Cliente.query.count(),
            con_cuenta=con_cuenta, wa_default=Setting.get("public_whatsapp", ""),
            lealtad_activa=lcfg["activa"], premios=premios,
        )

    @app.route("/admin/clientes/<int:cliente_id>/avisos", methods=["POST"])
    @auth.admin_required
    def cliente_toggle_avisos(cliente_id):
        """Marca/desmarca si un cliente acepta recibir avisos (WhatsApp/mail)."""
        from models import Cliente
        cli = Cliente.query.get_or_404(cliente_id)
        cli.acepta_avisos = not cli.acepta_avisos
        db.session.commit()
        return jsonify({"ok": True, "acepta": cli.acepta_avisos})

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

    # ------------------------------------------------------------------
    # TAREAS: to-do de Facundo para Arcano + sugerencias automaticas
    # ------------------------------------------------------------------
    def _tareas_sugeridas():
        """Cosas que la web detecta sola y conviene resolver. No se guardan: se
        recalculan en cada visita y desaparecen cuando el problema se resuelve.
        Cada una: {titulo, detalle, url, categoria, urgente}."""
        from models import Cliente, EgresoRecurrente, Egreso
        from datetime import date as _date
        sug = []
        hoy = today_uy()

        def _add(titulo, detalle, url, categoria, urgente=False):
            sug.append({"titulo": titulo, "detalle": detalle, "url": url,
                        "categoria": categoria, "urgente": urgente})

        # 1) Reservas pagadas que todavia no entregaste
        try:
            pagadas = (Reserva.query.filter(Reserva.estado == "Pagada",
                                            Reserva.entregado.is_(False))
                       .order_by(Reserva.id.asc()).all())
            for r in pagadas[:5]:
                _add(f"Entregar la reserva #{r.id}", f"{r.nombre or 'Cliente'} · ya está pagada",
                     url_for("admin_reserva_detail", reserva_id=r.id), "Envíos", True)
            if len(pagadas) > 5:
                _add(f"Entregar {len(pagadas) - 5} reservas pagadas más", "Ya están pagadas",
                     url_for("admin_reservas"), "Envíos", True)
        except Exception:
            db.session.rollback()
        # 2) Reservas pendientes de pago (las mas viejas primero)
        try:
            pend = (Reserva.query.filter(Reserva.estado == "Pendiente")
                    .order_by(Reserva.id.asc()).all())
            for r in pend[:5]:
                try:
                    dias = (hoy - r.fecha.date()).days if r.fecha else None
                except Exception:
                    dias = None
                det = r.nombre or "Cliente"
                if dias is not None and dias >= 1:
                    det += f" · hace {dias} día{'s' if dias != 1 else ''}"
                _add(f"Confirmar el pago de la reserva #{r.id}", det,
                     url_for("admin_reserva_detail", reserva_id=r.id), "Clientes",
                     bool(dias and dias >= 7))
            if len(pend) > 5:
                _add(f"{len(pend) - 5} reservas pendientes más", "Esperando pago",
                     url_for("admin_reservas"), "Clientes")
        except Exception:
            db.session.rollback()
        # 3) Encargos que todavia no compraste (sin pedido asignado)
        try:
            encargos = visible_records(
                Disc.query.filter(Disc.estado == "Encargo", Disc.pedido_id.is_(None)).all(), "discs")
            for d in encargos[:6]:
                para = f" · para {d.comprador.nombre}" if d.comprador else ""
                _add(f"Comprar el encargo: {d.artista} – {d.titulo}", f"Todavía sin pedido{para}",
                     url_for("disc_edit", disc_id=d.id), "Compras")
        except Exception:
            db.session.rollback()
        # 4) Discos a la venta sin precio (no se ven en la tienda)
        try:
            sin_precio = visible_records(
                Disc.query.filter(Disc.estado == "Disponible", Disc.stock > 0,
                                  (Disc.precio_uyu.is_(None)) | (Disc.precio_uyu <= 0),
                                  Disc.es_mystery_box.isnot(True)).all(), "discs")
            if sin_precio:
                n = len(sin_precio)
                _add(f"Ponerle precio a {n} disco{'s' if n != 1 else ''}",
                     "Sin precio no aparecen en la tienda",
                     url_for("disc_edit", disc_id=sin_precio[0].id) if n == 1 else url_for("dashboard"),
                     "Web", True)
        except Exception:
            db.session.rollback()
        # 5) Discos a la venta sin portada
        try:
            sin_cover = visible_records(
                Disc.query.filter(Disc.estado.in_(["Disponible", "Pre-order", "Encargo"]),
                                  (Disc.cover_local.is_(None)) | (Disc.cover_local == ""),
                                  (Disc.cover_url.is_(None)) | (Disc.cover_url == ""),
                                  Disc.es_mystery_box.isnot(True)).all(), "discs")
            if sin_cover:
                n = len(sin_cover)
                _add(f"Subir la portada de {n} disco{'s' if n != 1 else ''}",
                     "Se ven sin foto en la tienda",
                     url_for("disc_edit", disc_id=sin_cover[0].id) if n == 1 else url_for("dashboard"),
                     "Web")
        except Exception:
            db.session.rollback()
        # 6) Premios de lealtad para entregar
        try:
            if _lealtad_config()["activa"]:
                for c in Cliente.query.all():
                    total = sum(e["precio"] for e in _compras_de_cliente(c))
                    if _lealtad_estado(c, total)["tiene_premio"]:
                        _add(f"Entregar el premio de lealtad a {c.nombre}", "Llegó a la meta de compras",
                             url_for("admin_cliente_detail", cliente_id=c.id), "Clientes")
        except Exception:
            db.session.rollback()
        # 7) Gastos fijos del mes sin generar
        try:
            inicio = _date(hoy.year, hoy.month, 1)
            faltan = 0
            for r in EgresoRecurrente.query.filter_by(activo=True).all():
                ya = (Egreso.query.filter(Egreso.fecha >= inicio, Egreso.categoria == r.categoria,
                                          Egreso.descripcion == r.descripcion,
                                          Egreso.monto == r.monto).first())
                if not ya:
                    faltan += 1
            if faltan:
                _add("Generar los gastos fijos del mes",
                     f"{faltan} gasto{'s' if faltan != 1 else ''} mensual{'es' if faltan != 1 else ''} sin cargar",
                     url_for("contabilidad"), "Admin")
        except Exception:
            db.session.rollback()
        return sug

    from datetime import date as _dt_date
    _date_max = _dt_date(9999, 12, 31)

    def _tareas_resumen():
        """Para el cuadrito del dashboard: conteos + las 3 mas importantes."""
        from models import Tarea
        hoy = today_uy()
        try:
            abiertas = Tarea.query.filter(Tarea.estado != "Hecha").all()
        except Exception:
            db.session.rollback()
            abiertas = []
        vencidas = [t for t in abiertas if t.fecha_limite and t.fecha_limite < hoy]
        para_hoy = [t for t in abiertas if t.fecha_limite and t.fecha_limite == hoy]
        top = sorted(abiertas, key=lambda t: (
            0 if (t.fecha_limite and t.fecha_limite <= hoy) else 1,
            -(t.prioridad or 0),
            t.fecha_limite or _date_max, t.id))[:3]
        try:
            n_sug = len(_tareas_sugeridas())
        except Exception:
            n_sug = 0
        return {"abiertas": len(abiertas), "vencidas": len(vencidas), "hoy": len(para_hoy),
                "top": top, "sugeridas": n_sug, "hoy_fecha": hoy}

    def _tarea_set_estado(t, est):
        if est == "Hecha" and t.estado != "Hecha":
            t.hecha_en = datetime.utcnow()
        elif est != "Hecha":
            t.hecha_en = None
        t.estado = est

    def _tarea_desde_form(t, form):
        """Carga los campos de la tarea desde el form. Devuelve un error o None."""
        from models import ESTADOS_TAREA
        titulo = (form.get("titulo") or "").strip()[:200]
        if not titulo:
            return "La tarea necesita un nombre."
        t.titulo = titulo
        t.detalle = (form.get("detalle") or "").strip() or None
        t.categoria = (form.get("categoria") or "").strip()[:60] or None
        est = (form.get("estado") or "").strip()
        if est in ESTADOS_TAREA:
            _tarea_set_estado(t, est)
        t.agendada = bool(form.get("agendada"))
        try:
            t.prioridad = max(0, min(4, int(form.get("prioridad") or 0)))
        except (TypeError, ValueError):
            t.prioridad = 0
        fl = (form.get("fecha_limite") or "").strip()
        try:
            t.fecha_limite = datetime.strptime(fl, "%Y-%m-%d").date() if fl else None
        except ValueError:
            return "La fecha límite no es válida."

        def _int(v):
            v = (v or "").strip()
            try:
                return max(0, int(float(v))) if v else None
            except ValueError:
                return None
        t.meta = _int(form.get("meta")) or None
        t.progreso = min(_int(form.get("progreso")) or 0, t.meta) if t.meta else None
        return None

    def _volver_tareas(t=None):
        args = {k: request.form.get("volver_" + k) for k in ("cat", "ver", "orden")}
        args = {k: v for k, v in args.items() if v}
        return redirect(url_for("tareas_page", **args) + (f"#t{t.id}" if t else ""))

    # Colores de las etiquetas de categoria (relleno tintado, como las del dashboard).
    # Cada categoria mantiene siempre el mismo color.
    _TK_TAGS = ["59, 130, 246", "168, 85, 247", "16, 185, 129", "217, 119, 6",
                "236, 72, 153", "20, 184, 166", "99, 102, 241", "234, 88, 12"]

    def _tk_tag_rgb(nombre):
        if not nombre:
            return ""
        return _TK_TAGS[sum(ord(ch) for ch in nombre.strip().lower()) % len(_TK_TAGS)]

    def _tk_dias_txt(t, hoy):
        """Texto de 'Dias restantes' + tono (late/today/soon/'')."""
        dr = t.dias_restantes(hoy)
        if dr is None:
            return "", ""
        if dr < 0:
            return (f"hace {-dr} día{'s' if -dr != 1 else ''}", "late")
        if dr == 0:
            return ("Hoy", "today")
        if dr == 1:
            return ("Mañana", "soon")
        return (f"{dr} días", "soon" if dr <= 3 else "")

    def _tk_dias_partes(t, hoy):
        """Dias restantes como (numero, unidad) para mostrarlo grande: '5' + 'días'."""
        dr = t.dias_restantes(hoy)
        if dr is None:
            return "", ""
        if dr < 0:
            return (str(-dr), "día de atraso" if dr == -1 else "días de atraso")
        if dr == 0:
            return ("Hoy", "")
        return (str(dr), "día" if dr == 1 else "días")

    def _tk_json(t):
        hoy = today_uy()
        txt, tono = _tk_dias_txt(t, hoy)
        n, u = _tk_dias_partes(t, hoy)
        return {"ok": True, "id": t.id, "estado": t.estado, "dias_txt": txt, "dias_tono": tono,
                "estado_key": "curso" if t.estado == "En curso" else ("hecha" if t.hecha else "nueva"),
                "dias_n": n, "dias_u": u,
                "avance": t.avance_pct, "progreso": t.progreso, "meta": t.meta,
                "hecha": t.hecha, "cat_rgb": _tk_tag_rgb(t.categoria),
                "duracion_txt": t.duracion_txt}

    @app.route("/admin/tareas")
    @auth.admin_required
    def tareas_page():
        from models import Tarea, CATEGORIAS_TAREA, PRIORIDADES_TAREA, ESTADOS_TAREA
        hoy = today_uy()
        vista = (request.args.get("vista") if request.args.get("vista") in ("ideas", "constancia", "notas")
                 else "tareas")
        if vista == "notas":
            from models import Nota
            notas = (Nota.query.order_by(Nota.fijada.desc(), Nota.actualizada.desc(), Nota.id.desc()).all())
            n_abiertas = Tarea.query.filter(Tarea.estado != "Hecha").count()
            return render_template("tareas.html", vista=vista, n_abiertas=n_abiertas,
                                   n_ideas_pend=_n_ideas_pend(), n_constancia=_n_constancia(),
                                   notas=notas, n_notas=len(notas),
                                   abrir_nota=request.args.get("abrir", type=int))
        if vista == "ideas":
            n_abiertas = Tarea.query.filter(Tarea.estado != "Hecha").count()
            return render_template("tareas.html", vista=vista, n_abiertas=n_abiertas,
                                   n_constancia=_n_constancia(), **_ideas_ctx())
        if vista == "constancia":
            n_abiertas = Tarea.query.filter(Tarea.estado != "Hecha").count()
            return render_template("tareas.html", vista=vista, n_abiertas=n_abiertas,
                                   n_ideas_pend=_n_ideas_pend(), n_constancia=_n_constancia(),
                                   **_constancia_ctx())
        cat = (request.args.get("cat") or "").strip()
        ver = (request.args.get("ver") or "abiertas").strip()      # abiertas | todas | hechas
        orden = (request.args.get("orden") or "dias").strip()      # dias | prioridad | nombre | creada
        todas_db = Tarea.query.all()
        filas = [t for t in todas_db if not cat or t.categoria == cat]
        if ver == "hechas":
            filas = [t for t in filas if t.hecha]
        elif ver != "todas":
            ver = "abiertas"
            filas = [t for t in filas if not t.hecha]
        if orden == "prioridad":
            filas.sort(key=lambda t: (-(t.prioridad or 0), t.fecha_limite or _date_max, t.id))
        elif orden == "nombre":
            filas.sort(key=lambda t: (t.titulo or "").lower())
        elif orden == "creada":
            filas.sort(key=lambda t: t.id, reverse=True)
        else:
            orden = "dias"
            filas.sort(key=lambda t: (t.fecha_limite or _date_max, -(t.prioridad or 0), t.id))
        n_abiertas = sum(1 for t in todas_db if not t.hecha)
        _ab = [t for t in todas_db if not t.hecha]
        resumen = {
            "pendientes": len(_ab),
            "vencidas": sum(1 for t in _ab if t.fecha_limite and t.fecha_limite < hoy),
            "semana": sum(1 for t in _ab if t.fecha_limite and 0 <= (t.fecha_limite - hoy).days <= 7),
            "en_curso": sum(1 for t in _ab if t.estado == "En curso"),
        }
        cats_usadas = sorted({t.categoria for t in todas_db if t.categoria}, key=str.lower)
        cats = CATEGORIAS_TAREA + [c for c in cats_usadas if c not in CATEGORIAS_TAREA]
        return render_template(
            "tareas.html", vista=vista, filas=filas, n_abiertas=n_abiertas, n_hechas=len(todas_db) - n_abiertas,
            n_ideas_pend=_n_ideas_pend(), n_constancia=_n_constancia(),
            sugeridas=_tareas_sugeridas(), hoy=hoy, cat=cat, ver=ver, orden=orden, cats=cats,
            cats_usadas=cats_usadas, prioridades=PRIORIDADES_TAREA, estados_tarea=ESTADOS_TAREA,
            dias_txt=_tk_dias_txt, dias_partes=_tk_dias_partes, resumen=resumen, tag_rgb=_tk_tag_rgb,
        )

    # ------------------------------------------------------------------
    # Estrategia -> Notas (v6.14): notas libres que se guardan solas
    # ------------------------------------------------------------------
    @app.route("/admin/notas/nueva", methods=["POST"])
    @auth.admin_required
    def nota_nueva():
        from models import Nota
        data = request.get_json(silent=True) or {}
        n = Nota(titulo=(str(data.get("titulo") or "").strip()[:200] or None),
                 texto=(str(data.get("texto") or "")[:200000] or None),
                 creada=datetime.utcnow(), actualizada=datetime.utcnow())
        db.session.add(n)
        db.session.commit()
        return jsonify({"ok": True, "nota": n.to_dict()})

    @app.route("/admin/notas/<int:nota_id>", methods=["POST"])
    @auth.admin_required
    def nota_guardar(nota_id):
        """Guarda lo que vino (titulo, texto y/o fijada). Lo llama el editor solo,
        mientras escribis."""
        from models import Nota
        n = Nota.query.get_or_404(nota_id)
        data = request.get_json(silent=True) or {}
        if "titulo" in data:
            n.titulo = str(data.get("titulo") or "").strip()[:200] or None
        if "texto" in data:
            n.texto = str(data.get("texto") or "")[:200000] or None
        if "fijada" in data:
            n.fijada = bool(data.get("fijada"))
        n.actualizada = datetime.utcnow()
        db.session.commit()
        return jsonify({"ok": True, "nota": n.to_dict()})

    @app.route("/admin/notas/<int:nota_id>/borrar", methods=["POST"])
    @auth.admin_required
    def nota_borrar(nota_id):
        from models import Nota
        n = Nota.query.get_or_404(nota_id)
        db.session.delete(n)
        db.session.commit()
        return jsonify({"ok": True})

    @app.route("/admin/estrategia")
    @auth.admin_required
    def estrategia_page():
        """Direccion con el nombre nuevo de la seccion (antes 'Tareas')."""
        return redirect(url_for("tareas_page", **request.args))

    @app.route("/admin/tareas/<int:tarea_id>/campo", methods=["POST"])
    @auth.admin_required
    def tarea_campo(tarea_id):
        """Edicion en la celda de la tabla (AJAX): guarda UN campo y devuelve lo recalculado."""
        from models import Tarea, ESTADOS_TAREA
        t = Tarea.query.get_or_404(tarea_id)
        data = request.get_json(silent=True) or {}
        campo = (data.get("campo") or "").strip()
        valor = data.get("valor")
        txt = ("" if valor is None else str(valor)).strip()

        def _entero(v):
            try:
                return max(0, int(float(v))) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None

        if campo == "titulo":
            if not txt:
                return jsonify({"ok": False, "error": "La tarea necesita un nombre."}), 400
            t.titulo = txt[:200]
        elif campo == "detalle":
            t.detalle = txt or None
        elif campo == "categoria":
            t.categoria = txt[:60] or None
        elif campo == "estado":
            if txt not in ESTADOS_TAREA:
                return jsonify({"ok": False, "error": "Estado inválido."}), 400
            _tarea_set_estado(t, txt)
        elif campo == "agendada":
            t.agendada = bool(valor) and txt not in ("0", "false", "False")
        elif campo == "prioridad":
            p = _entero(txt)
            t.prioridad = min(4, p or 0)
        elif campo == "fecha_limite":
            try:
                t.fecha_limite = datetime.strptime(txt, "%Y-%m-%d").date() if txt else None
            except ValueError:
                return jsonify({"ok": False, "error": "Fecha inválida."}), 400
        elif campo == "duracion":
            from models import parsear_duracion
            try:
                t.duracion_min = parsear_duracion(txt)
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
        elif campo in ("progreso", "meta"):
            n = _entero(txt)
            if campo == "meta":
                t.meta = n or None
                if t.meta is None:
                    t.progreso = None
                elif t.progreso is not None:
                    t.progreso = min(t.progreso, t.meta)
            else:
                t.progreso = n
                if t.meta and n is not None:
                    t.progreso = min(n, t.meta)
            # Llegar a la meta la marca Hecha; bajar de la meta la reabre.
            if t.meta and t.progreso is not None:
                if t.progreso >= t.meta and not t.hecha:
                    _tarea_set_estado(t, "Hecha")
                elif t.progreso < t.meta and t.hecha:
                    _tarea_set_estado(t, "En curso")
                elif 0 < t.progreso < t.meta and t.estado == "Sin empezar":
                    _tarea_set_estado(t, "En curso")
        else:
            return jsonify({"ok": False, "error": "Campo no editable."}), 400
        db.session.commit()
        return jsonify(_tk_json(t))

    # ------------------------------------------------------------------
    # IDEAS (segunda pestaña de Tareas): de contenido, de la web, del negocio...
    # ------------------------------------------------------------------
    def _ideas_ctx():
        """Datos de la pestaña 'Ideas de contenido' para el template de tareas."""
        from models import IdeaContenido, FORMATOS_IDEA, ESTADOS_IDEA
        ver_i = (request.args.get("ver") or "pendientes").strip()   # pendientes | publicadas | todas
        todas = IdeaContenido.query.order_by(IdeaContenido.id.desc()).all()
        if ver_i == "publicadas":
            ideas = [i for i in todas if i.estado == "Publicada"]
        elif ver_i == "todas":
            ideas = todas
        else:
            ver_i = "pendientes"
            # En produccion primero, despues las ideas nuevas (las mas recientes arriba)
            ideas = sorted([i for i in todas if i.estado != "Publicada"],
                           key=lambda i: (0 if i.estado == "En producción" else 1, -i.id))
        n_pend = sum(1 for i in todas if i.estado != "Publicada")
        resumen_i = {"ideas": sum(1 for i in todas if i.estado == "Idea"),
                     "produccion": sum(1 for i in todas if i.estado == "En producción"),
                     "publicadas": len(todas) - n_pend}
        return {"ideas": ideas, "ver_i": ver_i, "n_ideas_pend": n_pend, "resumen_i": resumen_i,
                "n_ideas_pub": len(todas) - n_pend,
                "formatos_idea": FORMATOS_IDEA, "estados_idea": ESTADOS_IDEA}

    def _n_constancia():
        """Metas de constancia que todavia no estan cumplidas hoy (para la pestaña)."""
        try:
            ctx = _constancia_ctx()
            return ctx["resumen_c"]["atrasadas"] + (ctx["resumen_c"]["hoy_total"] - ctx["resumen_c"]["hoy_ok"])
        except Exception:
            db.session.rollback()
            return 0

    def _n_ideas_pend():
        from models import IdeaContenido
        try:
            return IdeaContenido.query.filter(IdeaContenido.estado != "Publicada").count()
        except Exception:
            db.session.rollback()
            return 0

    def _volver_ideas():
        ver_i = request.form.get("volver_ver") or None
        return redirect(url_for("tareas_page", vista="ideas", ver=ver_i))

    @app.route("/admin/ideas/nueva", methods=["POST"])
    @auth.admin_required
    def idea_nueva():
        from models import IdeaContenido, FORMATOS_IDEA
        titulo = (request.form.get("titulo") or "").strip()[:200]
        if not titulo:
            flash("La idea necesita un texto.", "error")
            return _volver_ideas()
        fmt = (request.form.get("formato") or "").strip()
        db.session.add(IdeaContenido(titulo=titulo, formato=fmt if fmt in FORMATOS_IDEA else None))
        db.session.commit()
        flash("Idea anotada.", "success")
        return _volver_ideas()

    @app.route("/admin/ideas/<int:idea_id>/campo", methods=["POST"])
    @auth.admin_required
    def idea_campo(idea_id):
        """Edicion en la celda (AJAX), igual que las tareas."""
        from models import IdeaContenido, FORMATOS_IDEA, ESTADOS_IDEA
        i = IdeaContenido.query.get_or_404(idea_id)
        data = request.get_json(silent=True) or {}
        campo = (data.get("campo") or "").strip()
        txt = ("" if data.get("valor") is None else str(data.get("valor"))).strip()
        if campo == "titulo":
            if not txt:
                return jsonify({"ok": False, "error": "La idea necesita un texto."}), 400
            i.titulo = txt[:200]
        elif campo == "detalle":
            i.detalle = txt or None
        elif campo == "formato":
            i.formato = txt if txt in FORMATOS_IDEA else None
        elif campo == "puntaje":
            try:
                n = int(float(txt or 0))
            except ValueError:
                return jsonify({"ok": False, "error": "Puntaje inválido."}), 400
            if not 0 <= n <= 5:
                return jsonify({"ok": False, "error": "El puntaje va de 1 a 5 estrellas."}), 400
            i.puntaje = n or None
        elif campo == "estado":
            if txt not in ESTADOS_IDEA:
                return jsonify({"ok": False, "error": "Estado inválido."}), 400
            if txt == "Publicada" and i.estado != "Publicada":
                i.publicada_en = datetime.utcnow()
            elif txt != "Publicada":
                i.publicada_en = None
            i.estado = txt
        else:
            return jsonify({"ok": False, "error": "Campo no editable."}), 400
        db.session.commit()
        return jsonify({"ok": True, "id": i.id, "estado": i.estado, "estado_key": i.estado_key,
                        "hecha": i.estado == "Publicada", "puntaje": i.puntaje or 0})

    @app.route("/admin/ideas/<int:idea_id>/a-tarea", methods=["POST"])
    @auth.admin_required
    def idea_a_tarea(idea_id):
        """Convierte la idea en una tarea (categoria Contenido) y la pasa a 'En produccion'."""
        from models import IdeaContenido, Tarea
        i = IdeaContenido.query.get_or_404(idea_id)
        if i.tarea_id and Tarea.query.get(i.tarea_id):
            flash("Esa idea ya tiene su tarea creada.", "info")
            return _volver_ideas()
        nombre = (f"{i.formato}: " if i.formato else "") + i.titulo
        # Si la idea tiene formato (Reel, Historia...) es de contenido; si no, es una
        # idea general y la tarea queda sin categoria para que la elijas.
        t = Tarea(titulo=nombre[:200], categoria="Contenido" if i.formato else None,
                  estado="Sin empezar", detalle=i.detalle)
        db.session.add(t)
        db.session.flush()
        i.tarea_id = t.id
        if i.estado == "Idea":
            i.estado = "En producción"
        db.session.commit()
        flash(f"Listo: creé la tarea “{t.titulo}”.", "success")
        return _volver_ideas()

    @app.route("/admin/ideas/<int:idea_id>/borrar", methods=["POST"])
    @auth.admin_required
    def idea_borrar(idea_id):
        from models import IdeaContenido
        i = IdeaContenido.query.get_or_404(idea_id)
        db.session.delete(i)
        db.session.commit()
        flash("Idea borrada.", "success")
        return _volver_ideas()

    # ------------------------------------------------------------------
    # CONSTANCIA (tercera pestaña de Tareas): metas de frecuencia de contenido
    # ------------------------------------------------------------------
    _DIAS_CORTOS = ["L", "M", "M", "J", "V", "S", "D"]

    def _habito_estado(h, hoy, regs):
        """Estado de una meta. regs = {fecha: cantidad} de ese habito (ultimas semanas).
        Semanas de lunes a domingo."""
        from datetime import timedelta as _td
        cant = max(1, h.cantidad or 1)
        lunes = hoy - _td(days=hoy.weekday())
        semana = [lunes + _td(days=i) for i in range(7)]
        hoy_n = regs.get(hoy, 0)

        def estado_dia(d):
            n = regs.get(d, 0)
            if d > hoy:
                return "futuro"
            if h.frecuencia == "dia":
                return "ok" if n >= cant else ("parcial" if n > 0 else "no")
            return "ok" if n > 0 else "no"

        grid = [{"fecha": d.isoformat(), "dia": _DIAS_CORTOS[i], "n": regs.get(d, 0),
                 "estado": estado_dia(d), "hoy": d == hoy} for i, d in enumerate(semana)]

        if h.frecuencia == "dia":
            meta_dias = max(1, min(7, h.dias_semana or 7))
            cumplidos = sum(1 for d in semana if d <= hoy and regs.get(d, 0) >= cant)
            restantes = sum(1 for d in semana if d >= hoy)   # hoy incluido
            hoy_ok = hoy_n >= cant
            if cumplidos >= meta_dias:
                est, txt = "ok", "Semana cumplida"
            elif cumplidos + restantes - (1 if hoy_ok else 0) >= meta_dias:
                faltan = meta_dias - cumplidos
                est, txt = "camino", f"Faltan {faltan} día{'s' if faltan != 1 else ''} esta semana"
            else:
                est, txt = "atrasado", "No llegás a la meta de la semana"
            periodo = f"{cumplidos}/{meta_dias} días"
            meta_txt = f"{cant} al día · {meta_dias} día{'s' if meta_dias != 1 else ''} por semana"
            hoy_meta = cant
        elif h.frecuencia == "semana":
            hechos = sum(regs.get(d, 0) for d in semana if d <= hoy)
            if hechos >= cant:
                est, txt = "ok", "Semana cumplida"
            else:
                dias_q = 6 - hoy.weekday()
                est = "atrasado" if dias_q == 0 else "camino"
                txt = "Te queda hoy" if dias_q == 0 else f"Te quedan {dias_q} día{'s' if dias_q != 1 else ''}"
            periodo = f"{hechos}/{cant} esta semana"
            meta_txt = f"{cant} por semana"
            hoy_meta = None
        else:  # cada N dias (ventana movil)
            n_dias = max(1, h.cada_dias or 15)
            desde = hoy - _td(days=n_dias - 1)
            hechos = sum(c for d, c in regs.items() if desde <= d <= hoy)
            ultimas = sorted([d for d, c in regs.items() if c > 0 and d <= hoy], reverse=True)
            ult = ultimas[0] if ultimas else None
            if hechos >= cant and ult:
                quedan = n_dias - (hoy - ult).days
                est, txt = "ok", f"Al día · el próximo en {quedan} día{'s' if quedan != 1 else ''}"
            elif ult:
                hace = (hoy - ult).days
                est, txt = "atrasado", f"Toca: el último fue hace {hace} días"
            else:
                est, txt = "atrasado", "Toca: todavía no lo registraste"
            periodo = f"{hechos}/{cant} en {n_dias} días"
            meta_txt = f"{cant} cada {n_dias} días"
            hoy_meta = None
        return {"id": h.id, "hoy_n": hoy_n, "hoy_meta": hoy_meta, "grid": grid, "estado": est,
                "estado_txt": txt, "periodo": periodo, "meta_txt": meta_txt}

    def _habitos_registros(ids, hoy):
        """{habito_id: {fecha: cantidad}} de las ultimas 5 semanas, en una sola consulta."""
        from models import HabitoRegistro
        from datetime import timedelta as _td
        out = {i: {} for i in ids}
        if not ids:
            return out
        for r in (HabitoRegistro.query
                  .filter(HabitoRegistro.habito_id.in_(ids), HabitoRegistro.fecha >= hoy - _td(days=35))
                  .all()):
            out.setdefault(r.habito_id, {})[r.fecha] = r.cantidad or 0
        return out

    def _constancia_ctx():
        from models import HabitoContenido, FRECUENCIAS_HABITO
        hoy = today_uy()
        habitos = HabitoContenido.query.order_by(HabitoContenido.orden.asc(), HabitoContenido.id.asc()).all()
        regs = _habitos_registros([h.id for h in habitos], hoy)
        filas = [(h, _habito_estado(h, hoy, regs.get(h.id, {}))) for h in habitos]
        diarios = [e for h, e in filas if h.frecuencia == "dia"]
        resumen_c = {
            "metas": len(filas),
            "hoy_ok": sum(1 for e in diarios if e["hoy_n"] >= (e["hoy_meta"] or 1)),
            "hoy_total": len(diarios),
            "al_dia": sum(1 for _, e in filas if e["estado"] in ("ok", "camino")),
            "atrasadas": sum(1 for _, e in filas if e["estado"] == "atrasado"),
        }
        return {"habitos": filas, "resumen_c": resumen_c, "frecuencias": FRECUENCIAS_HABITO,
                "hoy": hoy}

    def _tareas_dash():
        """Tareas abiertas CON deadline para la tabla del dashboard (mas proximas primero)."""
        from models import Tarea, CATEGORIAS_TAREA, PRIORIDADES_TAREA, ESTADOS_TAREA
        try:
            hoy = today_uy()
            todas = Tarea.query.all()
            filas = sorted([t for t in todas if not t.hecha and t.fecha_limite],
                           key=lambda t: (t.fecha_limite, -(t.prioridad or 0), t.id))
            cats_usadas = sorted({t.categoria for t in todas if t.categoria}, key=str.lower)
            return {
                "filas": filas, "hoy": hoy,
                "sin_fecha": sum(1 for t in todas if not t.hecha and not t.fecha_limite),
                "vencidas": sum(1 for t in filas if t.fecha_limite < hoy),
                "sugeridas": len(_tareas_sugeridas()),
                "cats": CATEGORIAS_TAREA + [c for c in cats_usadas if c not in CATEGORIAS_TAREA],
                "prioridades": PRIORIDADES_TAREA, "estados_tarea": ESTADOS_TAREA,
                "dias_txt": _tk_dias_txt, "dias_partes": _tk_dias_partes, "tag_rgb": _tk_tag_rgb,
            }
        except Exception:
            db.session.rollback()
            return None

    def _constancia_dash():
        """Tabla de Constancia para el dashboard (None si falla o no hay metas)."""
        try:
            ctx = _constancia_ctx()
            return ctx if ctx["habitos"] else None
        except Exception:
            db.session.rollback()
            return None

    def _habito_json(h):
        hoy = today_uy()
        regs = _habitos_registros([h.id], hoy).get(h.id, {})
        d = _habito_estado(h, hoy, regs)
        d["ok"] = True
        return d

    @app.route("/admin/constancia/nuevo", methods=["POST"])
    @auth.admin_required
    def habito_nuevo():
        from models import HabitoContenido, FRECUENCIAS_HABITO
        nombre = (request.form.get("nombre") or "").strip()[:120]
        if not nombre:
            flash("Poné qué vas a publicar (ej: Stories de discos).", "error")
            return redirect(url_for("tareas_page", vista="constancia"))

        def _n(k, dflt, lo, hi):
            try:
                return max(lo, min(hi, int(request.form.get(k) or dflt)))
            except (TypeError, ValueError):
                return dflt
        frec = request.form.get("frecuencia") or "dia"
        if frec not in FRECUENCIAS_HABITO:
            frec = "dia"
        orden = (db.session.query(db.func.max(HabitoContenido.orden)).scalar() or 0) + 1
        db.session.add(HabitoContenido(nombre=nombre, cantidad=_n("cantidad", 1, 1, 50), frecuencia=frec,
                                       dias_semana=_n("dias_semana", 7, 1, 7),
                                       cada_dias=_n("cada_dias", 15, 1, 90), orden=orden))
        db.session.commit()
        flash("Meta agregada.", "success")
        return redirect(url_for("tareas_page", vista="constancia"))

    @app.route("/admin/constancia/<int:habito_id>/marcar", methods=["POST"])
    @auth.admin_required
    def habito_marcar(habito_id):
        """Suma/resta publicaciones de un dia (hoy por defecto) o marca/desmarca un dia
        entero desde la grilla de la semana. JSON: {delta: 1|-1} o {fecha, toggle: true}."""
        from models import HabitoContenido, HabitoRegistro
        h = HabitoContenido.query.get_or_404(habito_id)
        data = request.get_json(silent=True) or {}
        hoy = today_uy()
        try:
            fecha = datetime.strptime(data.get("fecha") or "", "%Y-%m-%d").date() if data.get("fecha") else hoy
        except ValueError:
            return jsonify({"ok": False, "error": "Fecha inválida."}), 400
        if fecha > hoy:
            return jsonify({"ok": False, "error": "No se puede marcar un día que todavía no llegó."}), 400
        reg = HabitoRegistro.query.filter_by(habito_id=h.id, fecha=fecha).first()
        actual = reg.cantidad if reg else 0
        meta_dia = max(1, h.cantidad or 1) if h.frecuencia == "dia" else 1
        if data.get("toggle"):
            nuevo = 0 if actual >= meta_dia else meta_dia
        else:
            try:
                delta = int(data.get("delta") or 0)
            except (TypeError, ValueError):
                delta = 0
            nuevo = max(0, min(99, actual + delta))
        if nuevo == 0:
            if reg:
                db.session.delete(reg)
        elif reg:
            reg.cantidad = nuevo
        else:
            db.session.add(HabitoRegistro(habito_id=h.id, fecha=fecha, cantidad=nuevo))
        db.session.commit()
        return jsonify(_habito_json(h))

    @app.route("/admin/constancia/<int:habito_id>/campo", methods=["POST"])
    @auth.admin_required
    def habito_campo(habito_id):
        from models import HabitoContenido, FRECUENCIAS_HABITO
        h = HabitoContenido.query.get_or_404(habito_id)
        data = request.get_json(silent=True) or {}
        campo = (data.get("campo") or "").strip()
        txt = ("" if data.get("valor") is None else str(data.get("valor"))).strip()

        def _n(lo, hi):
            try:
                return max(lo, min(hi, int(float(txt))))
            except (TypeError, ValueError):
                return None
        if campo == "titulo":
            if not txt:
                return jsonify({"ok": False, "error": "La meta necesita un nombre."}), 400
            h.nombre = txt[:120]
        elif campo == "frecuencia":
            if txt not in FRECUENCIAS_HABITO:
                return jsonify({"ok": False, "error": "Frecuencia inválida."}), 400
            h.frecuencia = txt
        elif campo in ("cantidad", "dias_semana", "cada_dias"):
            n = _n(1, {"cantidad": 50, "dias_semana": 7, "cada_dias": 90}[campo])
            if n is None:
                return jsonify({"ok": False, "error": "Poné un número."}), 400
            setattr(h, campo, n)
        else:
            return jsonify({"ok": False, "error": "Campo no editable."}), 400
        db.session.commit()
        d = _habito_json(h)
        d["recargar"] = campo == "frecuencia"   # cambia la forma de la fila
        return jsonify(d)

    @app.route("/admin/constancia/<int:habito_id>/borrar", methods=["POST"])
    @auth.admin_required
    def habito_borrar(habito_id):
        from models import HabitoContenido
        h = HabitoContenido.query.get_or_404(habito_id)
        db.session.delete(h)
        db.session.commit()
        flash("Meta borrada.", "success")
        return redirect(url_for("tareas_page", vista="constancia"))

    # ------------------------------------------------------------------
    # SUBTAREAS (checklist dentro del panel de una tarea)
    # ------------------------------------------------------------------
    def _subtareas_json(t):
        subs = [x.to_dict() for x in t.subtareas]
        return {"ok": True, "tarea_id": t.id, "subtareas": subs,
                "hechas": sum(1 for x in subs if x["hecha"]), "total": len(subs)}

    @app.route("/admin/tareas/<int:tarea_id>/subtareas", methods=["GET"])
    @auth.admin_required
    def subtareas_listar(tarea_id):
        from models import Tarea
        return jsonify(_subtareas_json(Tarea.query.get_or_404(tarea_id)))

    @app.route("/admin/tareas/<int:tarea_id>/subtareas", methods=["POST"])
    @auth.admin_required
    def subtarea_crear(tarea_id):
        from models import Tarea, Subtarea
        t = Tarea.query.get_or_404(tarea_id)
        data = request.get_json(silent=True) or {}
        texto = (data.get("texto") or "").strip()[:300]
        if not texto:
            return jsonify({"ok": False, "error": "Escribí la subtarea."}), 400
        orden = (db.session.query(db.func.max(Subtarea.orden)).filter(Subtarea.tarea_id == t.id).scalar() or 0) + 1
        db.session.add(Subtarea(tarea_id=t.id, texto=texto, orden=orden))
        db.session.commit()
        db.session.refresh(t)
        return jsonify(_subtareas_json(t))

    @app.route("/admin/subtareas/<int:sub_id>", methods=["POST"])
    @auth.admin_required
    def subtarea_editar(sub_id):
        """JSON: {hecha: bool} para tildar, {texto} para renombrar, {borrar: true} para borrar."""
        from models import Subtarea, Tarea
        sub = Subtarea.query.get_or_404(sub_id)
        tarea_id = sub.tarea_id
        data = request.get_json(silent=True) or {}
        if data.get("borrar"):
            db.session.delete(sub)
        else:
            if "hecha" in data:
                sub.hecha = bool(data.get("hecha"))
            if "texto" in data:
                texto = (data.get("texto") or "").strip()[:300]
                if not texto:
                    return jsonify({"ok": False, "error": "La subtarea no puede quedar vacía."}), 400
                sub.texto = texto
        db.session.commit()
        return jsonify(_subtareas_json(Tarea.query.get(tarea_id)))

    @app.route("/admin/tareas/nueva", methods=["POST"])
    @auth.admin_required
    def tarea_nueva():
        from models import Tarea
        t = Tarea(estado="Sin empezar")
        err = _tarea_desde_form(t, request.form)
        if err:
            flash(err, "error")
            return _volver_tareas()
        db.session.add(t)
        db.session.commit()
        flash(f"Tarea agregada: {t.titulo}", "success")
        return _volver_tareas(t)

    @app.route("/admin/tareas/<int:tarea_id>/editar", methods=["POST"])
    @auth.admin_required
    def tarea_editar(tarea_id):
        from models import Tarea
        t = Tarea.query.get_or_404(tarea_id)
        err = _tarea_desde_form(t, request.form)
        if err:
            db.session.rollback()
            flash(err, "error")
        else:
            db.session.commit()
            flash("Tarea actualizada.", "success")
        return _volver_tareas(t)

    @app.route("/admin/tareas/<int:tarea_id>/rapido", methods=["POST"])
    @auth.admin_required
    def tarea_rapido(tarea_id):
        """Acciones de un toque: cambiar estado, sumar/restar progreso."""
        from models import Tarea, ESTADOS_TAREA
        t = Tarea.query.get_or_404(tarea_id)
        accion = (request.form.get("accion") or "").strip()
        if accion in ESTADOS_TAREA:
            _tarea_set_estado(t, accion)
        elif accion in ("mas", "menos") and t.meta:
            t.progreso = max(0, min(t.meta, (t.progreso or 0) + (1 if accion == "mas" else -1)))
            if t.progreso >= t.meta:
                _tarea_set_estado(t, "Hecha")
            elif t.estado == "Hecha":
                _tarea_set_estado(t, "En curso")
            elif t.progreso > 0 and t.estado == "Sin empezar":
                _tarea_set_estado(t, "En curso")
        db.session.commit()
        destino = _ruta_interna_segura(request.form.get("next"), "/admin")
        if destino:
            return redirect(destino)
        return _volver_tareas(t)

    @app.route("/admin/tareas/<int:tarea_id>/borrar", methods=["POST"])
    @auth.admin_required
    def tarea_borrar(tarea_id):
        from models import Tarea
        t = Tarea.query.get_or_404(tarea_id)
        titulo = t.titulo
        db.session.delete(t)
        db.session.commit()
        flash(f"Tarea borrada: {titulo}", "success")
        return _volver_tareas()

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
        # Discos que se pueden agregar como compra/reserva (para el selector). Incluye
        # los Vendidos: sirve para ATRIBUIR una venta ya hecha a este cliente (si el
        # disco ya esta Vendido, "lo compro" solo le pone el cliente, no re-vende).
        discs_para_agregar = (Disc.query
                              .filter(Disc.estado.in_(["Disponible", "Pre-order", "Encargo", "Reservado", "Vendido"]))
                              .filter(Disc.es_mystery_box.isnot(True))   # la box no es un disco
                              .order_by(Disc.artista.asc(), Disc.titulo.asc()).all())
        # Discos reservados o encargados a nombre de este cliente (para ver/gestionar la sena)
        reservados_cliente = (Disc.query
                              .filter(Disc.estado.in_(["Reservado", "Encargo"]), Disc.cliente_id == cli.id)
                              .order_by(Disc.estado.desc(), Disc.fecha_reserva.desc(), Disc.id.desc()).all())
        return render_template(
            "admin_cliente_detail.html", cli=cli, compras=compras,
            reservas_activas=reservas_activas, total=total, otros=otros,
            lealtad=lealtad, discs_para_agregar=discs_para_agregar,
            reservados_cliente=reservados_cliente,
        )

    def _volver_admin(default_endpoint="dashboard", **kw):
        """Redirige a donde vino (next del form o referer) si es una URL /admin;
        si no, al endpoint por defecto. Evita open-redirects."""
        destino = (_ruta_interna_segura(request.form.get("next"), "/admin")
                   or _ruta_interna_segura(request.referrer, "/admin"))
        if destino:
            return redirect(destino)
        return redirect(url_for(default_endpoint, **kw))

    def _aplicar_sena(disc, accion, monto=None, metodo=None):
        """Aplica una accion de sena a un disco, SIN commit. Devuelve (ok, msg).
        La comparten la ruta de sena (form) y el quick-update del dashboard (AJAX).
        Acciones: cobrar/editar (marca/actualiza la sena cobrada), devolver (se la
        devolviste: sale de la caja), quedar (te la quedaste: ingreso retenido)."""
        from models import MovimientoCaja
        if accion in ("cobrar", "editar"):
            if not monto or monto <= 0:
                return False, "Pone un monto de sena valido (mayor a 0)."
            disc.sena_cobrada = True
            disc.sena_monto_uyu = round(monto, 2)
            if not disc.sena_fecha:
                disc.sena_fecha = today_uy()
            if metodo:
                disc.sena_metodo = metodo
            _f = f"$ {round(monto):,.0f}".replace(",", ".")
            return True, (f"Sena de {_f} marcada como cobrada." if accion == "cobrar"
                          else f"Sena de {_f} actualizada.")
        old = MovimientoCaja.query.filter_by(origen="sena", origen_id=disc.id).first()
        if accion == "quedar":
            m = round(disc.sena_monto_uyu or 0, 2)
            if m > 0:
                db.session.add(MovimientoCaja(
                    fecha=today_uy(), tipo="ingreso", monto_uyu=m,
                    concepto=(f"Sena retenida: {disc.artista or ''} - {disc.titulo or ''}".strip(" -"))[:300],
                    metodo=disc.sena_metodo, categoria="Sena retenida",
                    origen="manual", origen_categoria="Sena retenida", manual_override=True,
                ))
            if old:
                db.session.delete(old)
            disc.sena_cobrada = False
            return True, "Sena retenida: queda como ingreso a favor tuyo en la caja."
        if accion == "devolver":
            if old:
                db.session.delete(old)
            disc.sena_cobrada = False
            return True, "Sena devuelta al cliente: se saco de la caja."
        return False, "Accion de sena no reconocida."

    @app.route("/admin/discs/<int:disc_id>/sena", methods=["POST"])
    @auth.admin_required
    def admin_disc_sena(disc_id):
        """Marca / edita / resuelve la SENA de reserva de un disco (form POST).
        La usan la ficha del cliente y el detalle de la reserva."""
        disc = Disc.query.get_or_404(disc_id)
        accion = (request.form.get("accion") or "").strip()
        monto = _parse_monto_caja(request.form.get("monto")) if accion in ("cobrar", "editar") else None
        met = (request.form.get("metodo") or "").strip() or None
        ok, msg = _aplicar_sena(disc, accion, monto=monto, metodo=met)
        if ok:
            db.session.commit()
            flash(msg, "success")
        else:
            flash(msg, "error")
        return _volver_admin()

    @app.route("/admin/clientes/<int:cliente_id>/agregar-disco", methods=["POST"])
    @auth.admin_required
    def admin_cliente_agregar_disco(cliente_id):
        """Desde la ficha del cliente: registrar que este cliente COMPRO o RESERVO un
        disco. Compro -> lo marca Vendido a su nombre (entra a ventas/caja/contab).
        Reservo -> lo marca Reservado a su nombre, con sena opcional.
        Encargo -> lo marca Encargo a su nombre (se lo voy a traer), con sena opcional."""
        from models import Cliente
        cli = Cliente.query.get_or_404(cliente_id)
        try:
            disc_id = int(request.form.get("disc_id") or 0)
        except (TypeError, ValueError):
            disc_id = 0
        disc = Disc.query.get(disc_id) if disc_id else None
        tipo = (request.form.get("tipo") or "").strip()
        if not disc:
            flash("Elegi un disco de la lista.", "error")
            return redirect(url_for("admin_cliente_detail", cliente_id=cli.id))

        disc.cliente_id = cli.id
        if tipo == "compro":
            if disc.estado != "Vendido":
                # Venta nueva: se marca Vendido y se cargan canal/metodo del form.
                disc.canal_venta = (request.form.get("canal") or "instagram").strip() or "instagram"
                met = (request.form.get("metodo") or "").strip()
                if met:
                    disc.metodo_pago_venta = met
                register_status_change(disc, "Vendido", nota=f"Compra cargada desde ficha de {cli.nombre}")
                db.session.commit()
                flash(f"Listo: {disc.artista} - {disc.titulo} quedó como compra de {cli.nombre}.", "success")
            else:
                # Ya estaba Vendido: solo se ATRIBUYE la venta a este cliente. No se pisan
                # canal/metodo/fecha de la venta ya cargada (eso se edita en el panel de Ventas).
                db.session.commit()
                flash(f"Listo: le atribui la compra de {disc.artista} - {disc.titulo} a {cli.nombre}.", "success")
        elif tipo in ("reservo", "encargo"):
            nuevo = "Encargo" if tipo == "encargo" else "Reservado"
            if disc.estado == "Vendido":
                db.session.rollback()
                flash("Ese disco ya está vendido: no se puede reservar ni encargar.", "error")
                return redirect(url_for("admin_cliente_detail", cliente_id=cli.id))
            if disc.estado != nuevo:
                _que = "Encargo" if tipo == "encargo" else "Reserva"
                register_status_change(disc, nuevo, nota=f"{_que} cargado desde ficha de {cli.nombre}")
            sena = _parse_monto_caja(request.form.get("sena_monto"))
            if sena and sena > 0:
                disc.sena_cobrada = True
                disc.sena_monto_uyu = round(sena, 2)
                if not disc.sena_fecha:
                    disc.sena_fecha = today_uy()
                met = (request.form.get("sena_metodo") or "").strip()
                if met:
                    disc.sena_metodo = met
            db.session.commit()
            _msg = (f"Listo: {disc.artista} - {disc.titulo} quedó "
                    f"{'encargado por' if tipo == 'encargo' else 'reservado para'} {cli.nombre}")
            _msg += (f" con sena de $ {sena:,.0f}.".replace(",", ".") if (sena and sena > 0) else " (sin sena).")
            flash(_msg, "success")
        else:
            flash("Elegí si lo compró, lo reservó o te lo encargó.", "error")
        return redirect(url_for("admin_cliente_detail", cliente_id=cli.id))

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
            return redirect(url_for("home_admin"))

        if auth.is_logged_in():
            return redirect(url_for("home_admin"))

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
                next_url = _ruta_interna_segura(
                    request.args.get("next") or request.form.get("next"), "/admin")
                if next_url:
                    return redirect(next_url)
                return redirect(url_for("home_admin"))
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
