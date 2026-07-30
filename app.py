"""Arcano Stock - Aplicacion Flask principal."""
import csv
import io
import os
from datetime import datetime

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
    app.config["SECRET_KEY"] = os.environ.get(
        "SECRET_KEY", "arcano-local-dev-key-change-me"
    )
    app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB

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
            "cliente_actual": cliente_actual,
        }

    return app


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

ORDER_FIELDS = {
    "fecha_ingreso": Disc.fecha_ingreso,
    "precio_uyu": Disc.precio_uyu,
    "costo_final_uyu": Disc.costo_final_uyu,
    "artista": Disc.artista,
    "sello": Disc.sello,
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

    order_by = (args.get("order") or "fecha_ingreso").strip()
    direction = (args.get("dir") or "desc").strip()
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

    for d in discs:
        suma_costo_eur += (d.costo_eur or 0) * (d.stock or 1)
        suma_envio_eur += (d.envio_eur or 0) * (d.stock or 1)
        suma_costo_final_uyu += (d.costo_final_uyu or 0) * (d.stock or 1)
        suma_precio_uyu += (d.precio_uyu or 0) * (d.stock or 1)
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
    origen = _str_or_none(form.get("origen")) or "arcano"
    if origen in ("arcano", "personal"):
        disc.origen = origen
    disc.notas = _str_or_none(form.get("notas"))
    disc.descripcion_publica = _str_or_none(form.get("descripcion_publica"))
    disc.tags = _str_or_none(form.get("tags"))
    disc.destacado = form.get("destacado") == "1"
    disc.en_nuevos = form.get("en_nuevos") == "1"
    disc.cartel_ultima = form.get("cartel_ultima") == "1"
    disc.mostrar_agotado = form.get("mostrar_agotado") == "1"
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
        discs = query_discs(request.args).all()
        totals = compute_totals(discs)
        sellos = get_sellos_unicos()
        tc = Setting.get_tc()

        filtros = {
            "q": request.args.get("q", ""),
            "estado": request.args.get("estado", "todos"),
            "sello": request.args.get("sello", "todos"),
            "origen": request.args.get("origen", "todos"),
            "order": request.args.get("order", "fecha_ingreso"),
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
        )

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
                file.save(os.path.join(covers_dir, filename))
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
        allowed = {"audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/ogg", "audio/webm"}
        if media_type not in allowed:
            return jsonify({"ok": False, "error": f"Tipo no soportado: {media_type}. Usar MP3/WAV/OGG."}), 400

        ext_map = {
            "audio/mpeg": ".mp3",
            "audio/mp3": ".mp3",
            "audio/wav": ".wav",
            "audio/x-wav": ".wav",
            "audio/ogg": ".ogg",
            "audio/webm": ".webm",
        }
        ext = ext_map.get(media_type, ".mp3")

        # Borrar sample anterior si habia
        samples_dir = os.path.join(app.static_folder, "samples")
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

        # Limitar tamano a 8 MB
        MAX_BYTES = 8 * 1024 * 1024
        content = file.read()
        if len(content) > MAX_BYTES:
            return jsonify({
                "ok": False,
                "error": f"Archivo demasiado grande ({len(content)//1024} KB). Maximo: 8 MB.",
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
            "url": url_for("static", filename=f"samples/{filename}"),
        })

    @app.route("/admin/tracks/<int:track_id>/clear-sample", methods=["POST"])
    @auth.admin_required
    def tracks_clear_sample(track_id: int):
        """Borra el sample MP3 local del track."""
        t = Track.query.get_or_404(track_id)
        if t.sample_local:
            samples_dir = os.path.join(app.static_folder, "samples")
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

        elif campo in ("destacado", "en_nuevos", "cartel_ultima", "mostrar_agotado"):
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
    # Resumen / metricas
    # ----------------------------------------------------------------------

    @app.route("/admin/resumen")
    @auth.admin_required
    def resumen():
        data = metrics.get_all_metrics()
        return render_template(
            "resumen.html",
            tc=Setting.get_tc(),
            **data,
        )

    # ----------------------------------------------------------------------
    # Settings
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
            public_about=Setting.get("public_about", ""),
            public_about_curaduria=Setting.get("public_about_curaduria", ""),
            public_about_como_funciona=Setting.get("public_about_como_funciona", ""),
            public_envios=Setting.get("public_envios", ""),
            public_instagram=Setting.get("public_instagram", ""),
            public_whatsapp=Setting.get("public_whatsapp", ""),
            public_banner_url=Setting.get("public_banner_url", ""),
            public_hero_title=Setting.get("public_hero_title", ""),
            public_hero_subtitle=Setting.get("public_hero_subtitle", ""),
            public_banner_video_url=Setting.get("public_banner_video_url", ""),
            public_legal_terminos=Setting.get("public_legal_terminos", ""),
            public_legal_envios=Setting.get("public_legal_envios", ""),
            public_legal_privacidad=Setting.get("public_legal_privacidad", ""),
            public_legal_cookies=Setting.get("public_legal_cookies", ""),
            public_grid_columnas=Setting.get("public_grid_columnas", "auto"),
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
        # La URL es tipo /covers/banner_xxxx.jpg -> extraer el filename
        filename = url.rstrip("/").split("/")[-1]
        if not filename:
            return ""
        path = os.path.join(app.config["COVERS_DIR"], filename)
        if os.path.exists(path):
            return url
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
            "banner_url": _banner_url_si_existe(),
            "hero_title": Setting.get("public_hero_title", ""),
            "hero_subtitle": Setting.get("public_hero_subtitle", ""),
            "banner_video_url": Setting.get("public_banner_video_url", ""),
            "legal_terminos": Setting.get("public_legal_terminos", ""),
            "legal_envios": Setting.get("public_legal_envios", ""),
            "legal_privacidad": Setting.get("public_legal_privacidad", ""),
            "legal_cookies": Setting.get("public_legal_cookies", ""),
            "grid_columnas": Setting.get("public_grid_columnas", "auto"),
            "carrusel_velocidad": Setting.get("public_carrusel_velocidad", "7"),
            "card_tamano": Setting.get("public_card_tamano", "mediano"),
        }

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
        all_disponibles = (
            Disc.query
            .filter(
                _or(
                    # Disponibles con stock
                    (Disc.estado == "Disponible") & (Disc.stock > 0),
                    # O agotados que el admin eligio mostrar igual
                    Disc.mostrar_agotado == True,  # noqa: E712
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
        else:
            discs.sort(key=lambda d: d.fecha_ingreso or datetime.min, reverse=True)

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
        if pagina == 1 and filtros_activos == 0 and not q and orden == "nuevos":
            # --- DESTACADOS ---
            # Los marcados manualmente tienen prioridad. Si faltan para 6,
            # se completan con aleatorios que rotan cada 10 dias.
            marcados_dest = [d for d in all_disponibles if d.destacado]
            destacados = list(marcados_dest[:6])
            if len(destacados) < 6:
                import random as _random
                # Semilla estable por periodo de 10 dias
                periodo = int(datetime.utcnow().timestamp() // (10 * 86400))
                resto = [d for d in all_disponibles if not d.destacado]
                rng = _random.Random(periodo)
                rng.shuffle(resto)
                destacados += resto[: 6 - len(destacados)]

            # --- NUEVOS INGRESOS ---
            # Los marcados manualmente van primero, despues los mas recientes
            # por fecha de ingreso, hasta completar 6.
            marcados_nuevos = [d for d in all_disponibles if d.en_nuevos]
            por_fecha = sorted(
                all_disponibles,
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

        return render_template(
            "public/home.html",
            discs=discs_pagina,
            destacados=destacados,
            nuevos_ingresos=nuevos_ingresos,
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

    @app.route("/disco/<int:disc_id>")
    def public_disc(disc_id: int):
        """Ficha publica de un disco."""
        disc = Disc.query.get_or_404(disc_id)
        # No mostrar discos que no esten disponibles publicamente
        if disc.estado not in ("Disponible", "Reservado", "Vendido"):
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
        if disc.estado != "Disponible" or disc.stock <= 0:
            return render_template(
                "public/not_available.html",
                disc=disc,
                **_public_context(),
            ), 400

        if request.method == "POST":
            nombre = (request.form.get("nombre") or "").strip()
            whatsapp = (request.form.get("whatsapp") or "").strip()
            email = (request.form.get("email") or "").strip() or None
            nota = (request.form.get("nota") or "").strip() or None

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
                precio_uyu_snap=disc.precio_uyu,
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
            nombre = (request.form.get("nombre") or "").strip()
            email = (request.form.get("email") or "").strip().lower()
            whatsapp = (request.form.get("whatsapp") or "").strip() or None
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
            email = (request.form.get("email") or "").strip().lower()
            password = request.form.get("password") or ""
            cliente = Cliente.query.filter_by(email=email).first()
            if not cliente or not cliente.check_password(password):
                flash("Email o contrasena incorrectos.", "error")
                return render_template("public/cliente_login.html", form={"email": email}, **_public_context())

            auth.cliente_login(cliente.id)
            flash(f"Hola de nuevo, {cliente.nombre}!", "success")
            next_url = request.args.get("next")
            if next_url and next_url.startswith("/"):
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
        cliente = Cliente.query.get_or_404(auth.cliente_actual_id())
        reservas = Reserva.query.filter_by(cliente_id=cliente.id).order_by(Reserva.fecha.desc()).all()
        return render_template(
            "public/cliente_cuenta.html",
            cliente=cliente,
            reservas=reservas,
            **_public_context(),
        )

    @app.route("/favoritos")
    def public_favoritos():
        """Pagina de favoritos. Los IDs viven en localStorage del cliente;
        el JS pide los discos al endpoint /api/discos-por-id."""
        return render_template("public/favoritos.html", **_public_context())

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
                "precio_uyu": d.precio_uyu or 0,
                "cover": cover,
                "estado": d.estado,
                "disponible": d.estado == "Disponible" and (d.stock or 0) > 0,
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
            ids = [int(x) for x in ids_raw.split(",") if x.strip().isdigit()][:100]
            nombre = (request.form.get("nombre") or "").strip()
            whatsapp = (request.form.get("whatsapp") or "").strip()
            email = (request.form.get("email") or "").strip() or None
            nota = (request.form.get("nota") or "").strip() or None

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

            # Traer discos y validar disponibilidad
            discs = Disc.query.filter(Disc.id.in_(ids)).all() if ids else []
            by_id = {d.id: d for d in discs}
            disponibles = [by_id[i] for i in ids if i in by_id
                           and by_id[i].estado == "Disponible" and (by_id[i].stock or 0) > 0]

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
                    precio_uyu_snap=d.precio_uyu,
                )
                db.session.add(item)

            db.session.commit()

            # Email al admin (best-effort, no rompe si falla)
            if mailer.is_configured():
                try:
                    mailer.send_reserva_notification(reserva, disponibles[0])
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
            "public_discos_por_pagina",
            "public_carrusel_velocidad",
            "public_card_tamano",
        ]
        for key in keys:
            value = (request.form.get(key) or "").strip()
            # Validar discos por pagina: entre 4 y 48
            if key == "public_discos_por_pagina":
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

        Setting.set("public_banner_url", url_for("serve_cover", filename=filename))
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
            user = request.form.get("user", "").strip()
            password = request.form.get("password", "")
            if auth.check_credentials(user, password):
                auth.login(user)
                flash("Bienvenido.", "success")
                next_url = request.args.get("next") or request.form.get("next")
                if next_url and next_url.startswith("/admin"):
                    return redirect(next_url)
                return redirect(url_for("dashboard"))
            else:
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
