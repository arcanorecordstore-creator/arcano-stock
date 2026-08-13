"""Modelos de base de datos para Arcano Stock."""
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


# ---------------------------------------------------------------------------
# Constantes de dominio
# ---------------------------------------------------------------------------

ESTADOS_DISCO = [
    "Disponible",
    "Pre-order",
    "Reservado",
    "Vendido",
    "Apartado para mi",
    "Descartado",
]

# Estado del audio/digitalizacion de cada disco (columna manual del dashboard).
AUDIO_ESTADOS = ["En digital", "Grabado", "Para grabar", "F"]

CONDICIONES_DISCO = [
    "Nuevo / Sellado",
    "Usado - Mint",
    "Usado - Excelente (NM)",
    "Usado - Muy Bueno (VG+)",
    "Usado - Bueno (VG)",
]

# Valor inicial del tipo de cambio EUR -> UYU
TC_DEFAULT = 47.0


# ---------------------------------------------------------------------------
# Modelo: Disc
# ---------------------------------------------------------------------------

class Disc(db.Model):
    """Un disco de vinilo en el inventario."""

    __tablename__ = "discs"

    id = db.Column(db.Integer, primary_key=True)

    # Datos del disco
    artista = db.Column(db.String(200), nullable=False)
    titulo = db.Column(db.String(300), nullable=False)
    sello = db.Column(db.String(200), nullable=True)
    anio = db.Column(db.Integer, nullable=True)           # opcional
    formato = db.Column(db.String(50), nullable=True)     # 12", 7", LP, etc.
    pais = db.Column(db.String(80), nullable=True)        # pais de origen del release
    genero = db.Column(db.String(100), nullable=True)
    tracklist = db.Column(db.Text, nullable=True)         # texto multi-linea

    # Multimedia
    cover_url = db.Column(db.String(500), nullable=True)      # URL original
    cover_local = db.Column(db.String(500), nullable=True)    # ruta local en /static/covers
    sample_url = db.Column(db.String(500), nullable=True)     # audio sample
    source_url = db.Column(db.String(500), nullable=True)     # URL Deejay.de original

    # Costos y precios (snapshot al momento de ingreso)
    costo_eur = db.Column(db.Float, nullable=False, default=0.0)
    envio_eur = db.Column(db.Float, nullable=False, default=0.0)
    tc_usado = db.Column(db.Float, nullable=False, default=TC_DEFAULT)
    costo_final_uyu = db.Column(db.Float, nullable=False, default=0.0)
    precio_uyu = db.Column(db.Float, nullable=False, default=0.0)

    # Stock y estado
    stock = db.Column(db.Integer, nullable=False, default=1)
    estado = db.Column(db.String(50), nullable=False, default="Disponible")
    condicion = db.Column(db.String(50), nullable=True)
    # Origen del disco: "arcano" (comprado para vender) o "personal" (coleccion propia)
    origen = db.Column(db.String(20), nullable=False, default="arcano")

    # Notas privadas (no se muestran en la tienda publica)
    notas = db.Column(db.Text, nullable=True)

    # Descripcion publica (editorial, se muestra en la tienda)
    descripcion_publica = db.Column(db.Text, nullable=True)

    # Tags/badges para mostrar en la tienda (ej: "Vinyl Only, White Label, Repress")
    tags = db.Column(db.String(500), nullable=True)

    # Marcado como destacado (se muestra en la seccion Destacados de la home)
    destacado = db.Column(db.Boolean, nullable=False, default=False)
    # Marcado manual para la seccion "Nuevos ingresos" de la home
    en_nuevos = db.Column(db.Boolean, nullable=False, default=False)
    # Cartel "Nuevo": 'auto' (14 dias), 'si' (forzado), 'no' (oculto)
    cartel_nuevo = db.Column(db.String(10), nullable=False, default="auto")
    # Cartel "Ultima copia": manual, solo si el admin lo marca
    cartel_ultima = db.Column(db.Boolean, nullable=False, default=False)
    # Precio de oferta (opcional). Si esta seteado y es menor al precio, se
    # muestra el precio tachado con el de oferta al lado (SALE).
    precio_oferta = db.Column(db.Float, nullable=True, default=None)
    # Mostrar en el catalogo aunque este agotado/vendido (con sello "Agotado")
    mostrar_agotado = db.Column(db.Boolean, nullable=False, default=False)
    # Badge de recomendacion del curador (etiqueta editable en Ajustes, default "Tip").
    es_tip = db.Column(db.Boolean, nullable=False, default=False)
    # Resena de una linea, visible en la card del catalogo (estilo Hard Wax).
    resena_corta = db.Column(db.String(160), nullable=True)
    # Estado de audio/digitalizacion (columna manual del dashboard): En digital / Grabado / Para grabar / F.
    audio_estado = db.Column(db.String(20), nullable=True)
    # Mostrar en el carrusel "Pre-order" del inicio (toggle manual, como destacado).
    # Solo tiene efecto si ademas el estado es "Pre-order".
    es_preorder = db.Column(db.Boolean, nullable=False, default=False)
    # Cantidad de copias vendidas de este disco (para facturacion).
    # Permite registrar que se vendio mas de 1 copia del mismo titulo.
    copias_vendidas = db.Column(db.Integer, nullable=False, default=0)
    # Pedido/compra al que pertenece este disco (para trackear inversion)
    pedido_id = db.Column(db.Integer, db.ForeignKey("pedidos.id"), nullable=True)

    # Orden manual para la tienda (drag-and-drop en el dashboard).
    # Mayor = aparece primero. Los discos nuevos reciben el mayor (arriba de todo).
    orden = db.Column(db.Integer, nullable=True, index=True)

    # Timestamps
    fecha_ingreso = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    fecha_reserva = db.Column(db.DateTime, nullable=True)
    fecha_venta = db.Column(db.DateTime, nullable=True)
    fecha_modificacion = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    # Relacion con historial de estados
    history = db.relationship(
        "StatusHistory",
        backref="disc",
        cascade="all, delete-orphan",
        order_by="StatusHistory.fecha.desc()",
    )

    # ---- Propiedades calculadas ----

    @property
    def ganancia_uyu(self) -> float:
        # Usa el precio de oferta si el disco esta en oferta (es lo que se cobra),
        # no el precio de lista, asi el margen no queda inflado.
        return round((self.precio_mostrar or self.precio_uyu or 0) - (self.costo_final_uyu or 0), 2)

    @property
    def mostrar_cartel_nuevo(self) -> bool:
        """Decide si mostrar el cartel 'Nuevo'. 'si'=siempre, 'no'=nunca, 'auto'=14 dias."""
        cn = getattr(self, "cartel_nuevo", "auto") or "auto"
        if cn == "si":
            return True
        if cn == "no":
            return False
        # auto: ultimos 14 dias
        if self.fecha_ingreso:
            from datetime import datetime as _dt
            return (_dt.utcnow() - self.fecha_ingreso).days <= 14
        return False

    @property
    def mostrar_cartel_ultima(self) -> bool:
        """El cartel 'Ultima copia' es 100% manual."""
        return bool(getattr(self, "cartel_ultima", False))

    @property
    def mostrar_preorder(self) -> bool:
        """True si el disco esta en estado Pre-order (muestra cartel y se reserva)."""
        return self.estado == "Pre-order"

    @property
    def en_carrusel_preorder(self) -> bool:
        """True si debe salir en el carrusel Pre-order del inicio:
        tiene que estar en estado Pre-order Y con el toggle prendido."""
        return self.estado == "Pre-order" and bool(getattr(self, "es_preorder", False))

    @property
    def en_oferta(self) -> bool:
        """True si tiene un precio de oferta valido (menor al precio normal)."""
        po = getattr(self, "precio_oferta", None)
        return bool(po and po > 0 and self.precio_uyu and po < self.precio_uyu)

    @property
    def precio_mostrar(self) -> float:
        """Precio a cobrar: el de oferta si aplica, si no el normal."""
        return self.precio_oferta if self.en_oferta else self.precio_uyu

    @property
    def descuento_pct(self) -> int:
        """Porcentaje de descuento (entero) si esta en oferta."""
        if not self.en_oferta:
            return 0
        return round((1 - (self.precio_oferta / self.precio_uyu)) * 100)

    @property
    def esta_agotado(self) -> bool:
        """True si el disco no esta disponible para comprar.
        Los pre-orders NO se consideran agotados (se pueden reservar)."""
        if self.estado == "Pre-order":
            return False
        return self.estado != "Disponible" or (self.stock or 0) <= 0

    @property
    def copias_facturadas(self) -> int:
        """Cantidad de copias vendidas a efectos de facturacion.
        Usa copias_vendidas; si es 0 pero el disco esta Vendido, cuenta 1."""
        cv = getattr(self, "copias_vendidas", 0) or 0
        if cv > 0:
            return cv
        return 1 if self.estado == "Vendido" else 0

    @property
    def total_facturado_uyu(self) -> float:
        """Total facturado por este disco: precio x copias vendidas."""
        return (self.precio_mostrar or self.precio_uyu or 0) * self.copias_facturadas

    @property
    def margen_pct(self) -> float:
        base = self.precio_mostrar or self.precio_uyu or 0
        if base <= 0:
            return 0.0
        return round((self.ganancia_uyu / base) * 100, 2)

    @property
    def tags_list(self) -> list[str]:
        """Devuelve los tags como lista limpia."""
        if not self.tags:
            return []
        return [t.strip() for t in self.tags.split(",") if t.strip()]

    @property
    def tiene_sample(self) -> bool:
        """True si algun track del disco tiene sample escuchable."""
        return any(t.has_sample for t in self.tracks)

    @property
    def primer_track_sample(self):
        """Devuelve el primer Track con sample (para el boton de escuchar en el catalogo)."""
        for t in self.tracks:
            if t.has_sample:
                return t
        return None

    @property
    def tracks_con_sample(self):
        """Lista de todos los tracks escuchables (para navegar con flechas en el player)."""
        return [t for t in self.tracks if t.has_sample]

    def recalcular_costo_final(self, tc: float | None = None) -> None:
        """Recalcula costo_final_uyu usando el TC dado (o el guardado)."""
        if tc is not None:
            self.tc_usado = tc
        self.costo_final_uyu = round(
            (self.costo_eur + self.envio_eur) * self.tc_usado, 2
        )

    def to_dict(self) -> dict:
        """Serializacion simple para endpoints de debug / API."""
        return {
            "id": self.id,
            "artista": self.artista,
            "titulo": self.titulo,
            "sello": self.sello,
            "anio": self.anio,
            "formato": self.formato,
            "pais": self.pais,
            "genero": self.genero,
            "tracklist": self.tracklist,
            "cover_url": self.cover_url,
            "cover_local": self.cover_local,
            "sample_url": self.sample_url,
            "source_url": self.source_url,
            "costo_eur": self.costo_eur,
            "envio_eur": self.envio_eur,
            "tc_usado": self.tc_usado,
            "costo_final_uyu": self.costo_final_uyu,
            "precio_uyu": self.precio_uyu,
            "ganancia_uyu": self.ganancia_uyu,
            "margen_pct": self.margen_pct,
            "stock": self.stock,
            "estado": self.estado,
            "condicion": self.condicion,
            "notas": self.notas,
            "descripcion_publica": self.descripcion_publica,
            "fecha_ingreso": self.fecha_ingreso.isoformat() if self.fecha_ingreso else None,
            "fecha_reserva": self.fecha_reserva.isoformat() if self.fecha_reserva else None,
            "fecha_venta": self.fecha_venta.isoformat() if self.fecha_venta else None,
            "fecha_modificacion": self.fecha_modificacion.isoformat() if self.fecha_modificacion else None,
        }

    def __repr__(self) -> str:
        return f"<Disc {self.id}: {self.artista} - {self.titulo}>"


# ---------------------------------------------------------------------------
# Modelo: Setting (clave-valor para configuracion global)
# ---------------------------------------------------------------------------

class Setting(db.Model):
    """Configuracion global tipo clave-valor."""

    __tablename__ = "settings"

    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.String(500), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    @classmethod
    def get(cls, key: str, default: str | None = None) -> str | None:
        # Cache por request: evita leer la misma setting varias veces en un
        # mismo render (el home leia settings 21 veces). Fuera de un request
        # (scripts, shell) va directo a la DB.
        try:
            from flask import g, has_request_context
            if has_request_context():
                cache = getattr(g, "_settings_cache", None)
                if cache is None:
                    cache = {}
                    g._settings_cache = cache
                if key in cache:
                    val = cache[key]
                    return val if val is not None else default
                row = cls.query.filter_by(key=key).first()
                cache[key] = row.value if row else None
                return row.value if row else default
        except Exception:
            pass
        row = cls.query.filter_by(key=key).first()
        return row.value if row else default

    @classmethod
    def set(cls, key: str, value: str) -> None:
        row = cls.query.filter_by(key=key).first()
        if row:
            row.value = value
        else:
            row = cls(key=key, value=value)
            db.session.add(row)
        db.session.commit()
        # Invalidar el cache del request si existe
        try:
            from flask import g, has_request_context
            if has_request_context():
                cache = getattr(g, "_settings_cache", None)
                if cache is not None:
                    cache[key] = value
        except Exception:
            pass

    @classmethod
    def get_tc(cls) -> float:
        """Helper: devuelve el tipo de cambio EUR->UYU como float."""
        raw = cls.get("tc_eur_uyu", str(TC_DEFAULT))
        try:
            return float(raw)
        except (TypeError, ValueError):
            return TC_DEFAULT

    @classmethod
    def get_tc_usd(cls) -> float:
        """Tipo de cambio USD->UYU como float."""
        raw = cls.get("tc_usd_uyu", "40")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 40.0

    @classmethod
    def get_categorias_egreso(cls) -> list:
        """Lista completa de categorias de egreso (default + las que agrego el usuario)."""
        extra = cls.get("categorias_egreso_extra", "") or ""
        extras = [c.strip() for c in extra.split(",") if c.strip()]
        # Mantener orden: defaults primero, luego extras sin duplicar
        todas = list(CATEGORIAS_EGRESO_DEFAULT)
        for c in extras:
            if c not in todas:
                todas.append(c)
        return todas

    @classmethod
    def add_categoria_egreso(cls, nombre: str) -> None:
        """Agrega una categoria nueva de egreso."""
        nombre = (nombre or "").strip()
        if not nombre or nombre in cls.get_categorias_egreso():
            return
        extra = cls.get("categorias_egreso_extra", "") or ""
        extras = [c.strip() for c in extra.split(",") if c.strip()]
        extras.append(nombre)
        cls.set("categorias_egreso_extra", ",".join(extras))


# ---------------------------------------------------------------------------
# Modelo: StatusHistory
# ---------------------------------------------------------------------------

class StatusHistory(db.Model):
    """Historial de cambios de estado de un disco."""

    __tablename__ = "status_history"

    id = db.Column(db.Integer, primary_key=True)
    disc_id = db.Column(
        db.Integer,
        db.ForeignKey("discs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    estado = db.Column(db.String(50), nullable=False)
    nota = db.Column(db.String(300), nullable=True)
    fecha = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "disc_id": self.disc_id,
            "estado": self.estado,
            "nota": self.nota,
            "fecha": self.fecha.isoformat() if self.fecha else None,
        }

    def __repr__(self) -> str:
        return f"<StatusHistory disc={self.disc_id} estado={self.estado}>"


# ---------------------------------------------------------------------------
# Modelo: Track (tracklist estructurado con samples)
# ---------------------------------------------------------------------------

class DiscImage(db.Model):
    """Foto adicional de un disco (galeria).

    La portada principal sigue viviendo en Disc.cover_local / cover_url.
    Estas son fotos extra (foto real del vinilo, contratapa, etiqueta, etc.).
    """

    __tablename__ = "disc_images"

    id = db.Column(db.Integer, primary_key=True)
    disc_id = db.Column(
        db.Integer,
        db.ForeignKey("discs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    posicion = db.Column(db.Integer, nullable=False, default=0)
    filename = db.Column(db.String(500), nullable=False)  # archivo en /static/covers/

    disc = db.relationship("Disc", backref=db.backref(
        "imagenes", cascade="all, delete-orphan", order_by="DiscImage.posicion"))

    def to_dict(self) -> dict:
        return {"id": self.id, "disc_id": self.disc_id,
                "posicion": self.posicion, "filename": self.filename}

    def __repr__(self) -> str:
        return f"<DiscImage disc={self.disc_id} {self.filename}>"


class Track(db.Model):
    """Un track individual dentro de un disco.

    Cada track puede tener:
    - Un audio sample local (MP3 subido manualmente)
    - Una URL de YouTube con timestamp opcional (para arrancar en el drop)
    """

    __tablename__ = "tracks"

    id = db.Column(db.Integer, primary_key=True)
    disc_id = db.Column(
        db.Integer,
        db.ForeignKey("discs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    posicion = db.Column(db.Integer, nullable=False, default=0)  # 0, 1, 2...
    lado = db.Column(db.String(10), nullable=True)   # A1, A2, B1, B2, etc.
    titulo = db.Column(db.String(300), nullable=False)
    artista = db.Column(db.String(200), nullable=True)  # opcional, si difiere del disc.artista
    duracion = db.Column(db.String(10), nullable=True)  # mm:ss opcional

    # Audio sample: prioridad MP3 local > YouTube
    sample_local = db.Column(db.String(500), nullable=True)  # nombre de archivo en /static/samples/
    youtube_url = db.Column(db.String(500), nullable=True)   # URL completa
    youtube_start = db.Column(db.Integer, nullable=True)     # segundos (para arrancar en el drop)

    fecha_ingreso = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    # Contador total de reproducciones del sample (para charts de escuchados).
    play_count = db.Column(db.Integer, nullable=False, default=0)

    disc = db.relationship("Disc", backref=db.backref(
        "tracks",
        cascade="all, delete-orphan",
        order_by="Track.posicion",
    ))

    @property
    def has_sample(self) -> bool:
        return bool(self.sample_local or self.youtube_url)

    @property
    def sample_kind(self) -> str:
        """'local' | 'youtube' | 'soundcloud' | 'none'.

        El campo youtube_url guarda la URL del track y puede ser de YouTube o
        de SoundCloud (por compatibilidad no se renombro la columna).
        """
        if self.sample_local:
            return "local"
        if self.youtube_url:
            url = self.youtube_url.lower()
            if "soundcloud.com" in url:
                return "soundcloud"
            return "youtube"
        return "none"

    @property
    def youtube_video_id(self) -> str | None:
        """Extrae el ID del video de YouTube desde la URL (soporta youtu.be/xxx, youtube.com/watch?v=xxx, etc)."""
        if not self.youtube_url:
            return None
        import re
        # Patrones comunes
        patterns = [
            r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([A-Za-z0-9_-]{11})",
        ]
        for pat in patterns:
            m = re.search(pat, self.youtube_url)
            if m:
                return m.group(1)
        return None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "disc_id": self.disc_id,
            "posicion": self.posicion,
            "lado": self.lado,
            "titulo": self.titulo,
            "artista": self.artista,
            "duracion": self.duracion,
            "sample_local": self.sample_local,
            "youtube_url": self.youtube_url,
            "youtube_start": self.youtube_start,
            "sample_kind": self.sample_kind,
            "youtube_video_id": self.youtube_video_id,
            "soundcloud_url": self.youtube_url if self.sample_kind == "soundcloud" else None,
        }

    def __repr__(self) -> str:
        return f"<Track {self.id}: {self.lado} {self.titulo}>"


def parse_tracklist_text(text: str) -> list[dict]:
    """Parsea texto plano de tracklist a lista de dicts con lado/titulo/artista.

    Soporta formatos comunes:
      "A1. Artista - Titulo"
      "A1 Artista - Titulo"
      "A1: Titulo"
      "A1 - Titulo"
      "1. Titulo"
    """
    import re
    if not text:
        return []

    resultados = []
    lines = text.replace("\r\n", "\n").split("\n")
    for pos, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue

        # Match lado (A1, A2, B1, 1, 2...)
        m = re.match(
            r"^\s*([ABCDEFGH][12345]|\d{1,2})[.\-:)\s]+(.+)$",
            line,
        )
        if not m:
            # No hay indicador de lado, tratar toda la linea como titulo
            resultados.append({
                "posicion": pos,
                "lado": None,
                "titulo": line[:300],
                "artista": None,
            })
            continue

        lado_raw = m.group(1).upper()
        resto = m.group(2).strip()

        # Si el lado es solo un numero, dejarlo como None (no es lado real)
        if lado_raw.isdigit():
            lado = None
        else:
            lado = lado_raw

        # Ver si hay separacion artista - titulo
        artista = None
        titulo = resto
        if " - " in resto:
            parts = resto.split(" - ", 1)
            artista = parts[0].strip() or None
            titulo = parts[1].strip()

        resultados.append({
            "posicion": pos,
            "lado": lado,
            "titulo": titulo[:300],
            "artista": artista[:200] if artista else None,
        })

    return resultados


# ---------------------------------------------------------------------------
# Modelo: Reserva
# ---------------------------------------------------------------------------

class PlayEvent(db.Model):
    """Registro de cada reproduccion de un sample, con fecha.

    Permite armar el chart de "lo mas escuchado del mes" con ventana real
    de 30 dias (el play_count del Track es el total historico).
    """

    __tablename__ = "play_events"

    id = db.Column(db.Integer, primary_key=True)
    track_id = db.Column(db.Integer, db.ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False, index=True)
    disc_id = db.Column(db.Integer, db.ForeignKey("discs.id", ondelete="CASCADE"), nullable=False, index=True)
    fecha = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)


class Cliente(db.Model):
    """Cuenta de cliente de la tienda publica.

    Separado del login de admin (que usa .env). Las contrasenas se guardan
    hasheadas con werkzeug. Un cliente puede tener varias reservas.
    """

    __tablename__ = "clientes"

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(200), nullable=False, unique=True, index=True)
    whatsapp = db.Column(db.String(50), nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    fecha_registro = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def set_password(self, raw: str) -> None:
        from werkzeug.security import generate_password_hash
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        from werkzeug.security import check_password_hash
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, raw)

    def __repr__(self) -> str:
        return f"<Cliente {self.id}: {self.email}>"


class ClienteFavorito(db.Model):
    """Favoritos de un cliente guardados del lado del servidor, para que
    persistan entre dispositivos (ademas del localStorage del navegador)."""

    __tablename__ = "cliente_favoritos"

    id = db.Column(db.Integer, primary_key=True)
    cliente_id = db.Column(
        db.Integer, db.ForeignKey("clientes.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    disc_id = db.Column(
        db.Integer, db.ForeignKey("discs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    fecha = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("cliente_id", "disc_id", name="uq_cliente_disc_fav"),
    )


ESTADOS_RESERVA = [
    "Pendiente",      # cliente reservo, vos todavia no confirmaste
    "Confirmada",     # vos confirmaste que se la guardas
    "Completada",     # cliente pago y retiro
    "Cancelada",      # se cancelo por alguna razon
]


class Reserva(db.Model):
    """Una reserva hecha por un cliente desde la tienda publica."""

    __tablename__ = "reservas"

    id = db.Column(db.Integer, primary_key=True)
    disc_id = db.Column(
        db.Integer,
        db.ForeignKey("discs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Cliente con cuenta (opcional; las reservas anonimas lo dejan en null)
    cliente_id = db.Column(
        db.Integer,
        db.ForeignKey("clientes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Datos del cliente
    nombre = db.Column(db.String(200), nullable=False)
    whatsapp = db.Column(db.String(50), nullable=False)
    email = db.Column(db.String(200), nullable=True)
    nota = db.Column(db.Text, nullable=True)  # opcional, mensaje del cliente

    # Snapshot del disco al momento de la reserva (por si despues se borra)
    disc_artista_snap = db.Column(db.String(200), nullable=True)
    disc_titulo_snap = db.Column(db.String(300), nullable=True)
    disc_precio_uyu_snap = db.Column(db.Float, nullable=True)

    # Estado y timestamps
    estado = db.Column(db.String(30), nullable=False, default="Pendiente")
    fecha = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    fecha_actualizacion = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    # Metodo de pago elegido y comprobante (para transferencia manual)
    metodo_pago = db.Column(db.String(20), nullable=True)  # coordinar / transferencia / mercadopago
    comprobante_file = db.Column(db.String(200), nullable=True)  # archivo del comprobante subido

    # Pago online con Mercado Pago (solo si el cliente eligio pagar por la web)
    pagado = db.Column(db.Boolean, nullable=False, default=False)
    mp_preference_id = db.Column(db.String(120), nullable=True)
    mp_payment_id = db.Column(db.String(120), nullable=True)
    mp_status = db.Column(db.String(30), nullable=True)  # approved / pending / rejected
    total_cobrado_uyu = db.Column(db.Float, nullable=True)  # total + recargo cobrado

    # Relacion (puede ser null si el disco se borro)
    disc = db.relationship("Disc", backref="reservas", foreign_keys=[disc_id])
    cliente = db.relationship("Cliente", backref=db.backref("reservas", order_by="Reserva.fecha.desc()"), foreign_keys=[cliente_id])

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "disc_id": self.disc_id,
            "nombre": self.nombre,
            "whatsapp": self.whatsapp,
            "email": self.email,
            "nota": self.nota,
            "disc_artista_snap": self.disc_artista_snap,
            "disc_titulo_snap": self.disc_titulo_snap,
            "disc_precio_uyu_snap": self.disc_precio_uyu_snap,
            "estado": self.estado,
            "fecha": self.fecha.isoformat() if self.fecha else None,
        }

    @property
    def items_efectivos(self) -> list:
        """Devuelve los items de la reserva.

        Compatibilidad: si es una reserva vieja (sin items en la tabla nueva
        pero con disc_id), devuelve un item 'virtual' construido con las
        columnas viejas. Asi el resto del codigo trata todo igual.
        """
        if self.items:
            return list(self.items)
        # Reserva vieja: construir item virtual
        if self.disc_id or self.disc_artista_snap:
            virtual = ReservaItem(
                reserva_id=self.id,
                disc_id=self.disc_id,
                artista_snap=self.disc_artista_snap,
                titulo_snap=self.disc_titulo_snap,
                precio_uyu_snap=self.disc_precio_uyu_snap,
            )
            virtual._disc_ref = self.disc
            return [virtual]
        return []

    @property
    def total_uyu(self) -> float:
        """Suma de los precios snapshot de todos los items."""
        return sum((it.precio_uyu_snap or 0) for it in self.items_efectivos)

    @property
    def cantidad_discos(self) -> int:
        return len(self.items_efectivos)

    def __repr__(self) -> str:
        return f"<Reserva {self.id}: {self.nombre} ({self.cantidad_discos} discos)>"


class ReservaItem(db.Model):
    """Un disco dentro de una reserva (para carrito multi-disco)."""

    __tablename__ = "reserva_items"

    id = db.Column(db.Integer, primary_key=True)
    reserva_id = db.Column(
        db.Integer,
        db.ForeignKey("reservas.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    disc_id = db.Column(
        db.Integer,
        db.ForeignKey("discs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Snapshot del disco al momento de la reserva
    artista_snap = db.Column(db.String(200), nullable=True)
    titulo_snap = db.Column(db.String(300), nullable=True)
    precio_uyu_snap = db.Column(db.Float, nullable=True)

    reserva = db.relationship("Reserva", backref=db.backref(
        "items", cascade="all, delete-orphan"))
    disc = db.relationship("Disc", foreign_keys=[disc_id])

    # Para items virtuales (reservas viejas) se setea manualmente
    _disc_ref = None

    @property
    def disco(self):
        """Devuelve el disco real (funciona para items reales y virtuales)."""
        if self._disc_ref is not None:
            return self._disc_ref
        return self.disc

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "reserva_id": self.reserva_id,
            "disc_id": self.disc_id,
            "artista_snap": self.artista_snap,
            "titulo_snap": self.titulo_snap,
            "precio_uyu_snap": self.precio_uyu_snap,
        }

    def __repr__(self) -> str:
        return f"<ReservaItem reserva={self.reserva_id} disc={self.disc_id}>"


# ---------------------------------------------------------------------------
# Contabilidad: egresos (gastos operativos)
# ---------------------------------------------------------------------------

# Categorias de gasto por defecto. El usuario puede agregar mas.
CATEGORIAS_EGRESO_DEFAULT = [
    "Hosting",
    "Publicidad",
    "Envios",
    "Viajes",
    "Otros",
]

MONEDAS_EGRESO = ["UYU", "USD", "EUR"]


class Egreso(db.Model):
    """Un gasto operativo del negocio (no un disco)."""
    __tablename__ = "egresos"

    id = db.Column(db.Integer, primary_key=True)
    fecha = db.Column(db.Date, nullable=False, default=lambda: datetime.utcnow().date())
    categoria = db.Column(db.String(60), nullable=False, default="Otros")
    descripcion = db.Column(db.String(300), nullable=True)
    # Monto en la moneda original
    monto = db.Column(db.Float, nullable=False, default=0.0)
    moneda = db.Column(db.String(3), nullable=False, default="UYU")
    # Tipo de cambio usado para convertir a UYU (1 si ya es UYU)
    tc_usado = db.Column(db.Float, nullable=False, default=1.0)
    # Monto convertido a UYU (se calcula al guardar)
    monto_uyu = db.Column(db.Float, nullable=False, default=0.0)
    creado = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f"<Egreso {self.categoria} {self.monto} {self.moneda}>"


class EgresoRecurrente(db.Model):
    """Plantilla de gasto que se repite cada mes (ej: hosting)."""
    __tablename__ = "egresos_recurrentes"

    id = db.Column(db.Integer, primary_key=True)
    categoria = db.Column(db.String(60), nullable=False, default="Otros")
    descripcion = db.Column(db.String(300), nullable=True)
    monto = db.Column(db.Float, nullable=False, default=0.0)
    moneda = db.Column(db.String(3), nullable=False, default="UYU")
    activo = db.Column(db.Boolean, nullable=False, default=True)
    creado = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f"<EgresoRecurrente {self.categoria} {self.monto} {self.moneda}>"


# ---------------------------------------------------------------------------
# Caja / Banco: libro de movimientos de plata
# ---------------------------------------------------------------------------

# Tipos de movimiento y metodos de cobro de la caja
TIPOS_MOV_CAJA = ["ingreso", "egreso"]
# Metodos de cobro (para los ingresos): como/donde entro la plata
METODOS_PAGO_CAJA = ["mercadopago", "transferencia", "efectivo", "otro"]
# Etiquetas legibles de los metodos
METODOS_PAGO_CAJA_LABEL = {
    "mercadopago": "Mercado Pago",
    "transferencia": "Transferencia",
    "efectivo": "Efectivo",
    "otro": "Otro",
}


class MovimientoCaja(db.Model):
    """Un movimiento de plata en la caja/banco de Arcano.

    La caja es el libro de TODOS los movimientos de dinero (entra / sale) para
    saber cuanta plata hay realmente. Cada fila puede venir de:
      - Automatico: se sincroniza solo desde lo que ya se carga en el sistema.
        origen 'venta'  -> un disco Vendido (ingreso)
        origen 'pedido' -> una compra de discos (egreso)
        origen 'gasto'  -> un egreso por categoria (egreso)
      - Manual: lo agrega Facundo a mano (aportes, retiros, ajustes). origen 'manual'.

    Todos los campos son editables por si hubo un error al cargarlos. Los
    automaticos guardan 'origen_id' (el id del Disc/Pedido/Egreso de origen) para
    no duplicarse en cada sincronizacion.
    """
    __tablename__ = "movimientos_caja"

    id = db.Column(db.Integer, primary_key=True)
    fecha = db.Column(db.Date, nullable=False, default=lambda: datetime.utcnow().date())
    tipo = db.Column(db.String(10), nullable=False, default="ingreso")  # ingreso / egreso
    monto_uyu = db.Column(db.Float, nullable=False, default=0.0)
    concepto = db.Column(db.String(300), nullable=True)
    # Para ingresos: como entro la plata (mercadopago / transferencia / efectivo / otro)
    metodo = db.Column(db.String(20), nullable=True)
    # Para egresos: en que categoria (Pedido / Hosting / etc)
    categoria = db.Column(db.String(60), nullable=True)
    # Origen: 'manual' o el auto-sincronizado desde otra tabla
    origen = db.Column(db.String(20), nullable=False, default="manual")  # manual / venta / pedido / gasto
    origen_id = db.Column(db.Integer, nullable=True, index=True)  # id del Disc/Pedido/Egreso de origen
    # True si es un aporte de capital de Facundo (plata propia que puso en el negocio).
    # Es un ingreso, pero se separa de las ventas: no es facturacion, es inversion.
    es_aporte = db.Column(db.Boolean, nullable=False, default=False)
    # Marca los aportes que son un "adelanto de Mercado Pago" (Facundo pone su plata
    # para tener liquidez mientras MP retiene el cobro). Valores: 'pendiente' (falta
    # que MP pague y hacer el retiro) / 'saldado' (MP ya pago y se hizo el retiro).
    adelanto_mp = db.Column(db.String(20), nullable=True)
    # True cuando Facundo edito a mano un movimiento AUTOMATICO. En ese caso la
    # sincronizacion deja de pisar su edicion (respeta el monto/fecha que puso).
    # Los movimientos automaticos sin tocar SI se actualizan si cambia el origen.
    manual_override = db.Column(db.Boolean, nullable=False, default=False)
    # Categoria de origen LIBRE para movimientos manuales (ej: "Efectivo mano",
    # "Regalo"). Solo aplica a los manuales; los automaticos muestran Venta/Pedido/
    # Gasto. No toca el sync (el origen sigue siendo "manual").
    origen_categoria = db.Column(db.String(60), nullable=True)
    creado = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    @property
    def es_manual(self) -> bool:
        return (self.origen or "manual") == "manual"

    @property
    def signo(self) -> int:
        return 1 if self.tipo == "ingreso" else -1

    @property
    def monto_con_signo(self) -> float:
        return round((self.monto_uyu or 0) * self.signo, 2)

    @property
    def metodo_label(self) -> str:
        return METODOS_PAGO_CAJA_LABEL.get(self.metodo or "", "")

    @property
    def origen_label(self) -> str:
        return {
            "manual": "Manual",
            "venta": "Venta",
            "pedido": "Pedido",
            "gasto": "Gasto",
        }.get(self.origen or "manual", "Manual")

    @property
    def origen_display(self) -> str:
        """Lo que se muestra en la columna Origen: la categoria propia si es un
        manual con categoria puesta; si no, la etiqueta estandar."""
        if self.es_manual and self.origen_categoria:
            return self.origen_categoria
        return self.origen_label

    def __repr__(self):
        return f"<MovimientoCaja {self.tipo} {self.monto_uyu} ({self.origen})>"


# Proveedores de pedidos
PROVEEDORES_PEDIDO = ["Deejay.de", "Viaje", "Otro"]


class Pedido(db.Model):
    """Un pedido/compra de discos al por mayor (para trackear la inversion)."""
    __tablename__ = "pedidos"

    id = db.Column(db.Integer, primary_key=True)
    numero = db.Column(db.String(40), nullable=True)      # numero de pedido (texto libre)
    fecha = db.Column(db.Date, nullable=True, default=lambda: datetime.utcnow().date())
    cant_discos = db.Column(db.Integer, nullable=False, default=0)   # cuantos discos declara el pedido
    costo_eur = db.Column(db.Float, nullable=False, default=0.0)     # costo total en euros
    costo_usd = db.Column(db.Float, nullable=False, default=0.0)     # costo total en dolares
    # Costo real en UYU segun lo que debito el banco (opcional).
    # Si esta seteado, se usa en vez del calculo automatico.
    costo_uyu_manual = db.Column(db.Float, nullable=True)
    proveedor = db.Column(db.String(40), nullable=False, default="Deejay.de")
    notas = db.Column(db.Text, nullable=True)
    # Costos informativos del pedido (envio y servicio de la web, en EUR). NO entran en
    # ningun calculo: el costo total lo pone Facundo a mano (costo_usd / costo_uyu_manual).
    envio_eur = db.Column(db.Float, nullable=True)
    servicio_eur = db.Column(db.Float, nullable=True)
    creado = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    # Relacion con los discos asignados a este pedido
    discos = db.relationship("Disc", backref="pedido", lazy=True)

    @property
    def costo_total_uyu(self) -> float:
        """Costo total del pedido en UYU. Si hay un monto manual (lo que
        debito el banco), se usa ese; si no, se calcula desde la moneda del pedido."""
        if self.costo_uyu_manual is not None:
            return round(self.costo_uyu_manual, 2)
        return self.costo_total_uyu_calculado

    @property
    def costo_total_uyu_calculado(self) -> float:
        """Costo teorico calculado (sin el manual). El EUR y el USD son el MISMO costo
        en dos monedas: NO se suman. Si hay USD (lo que cobro la tarjeta) se usa ese;
        si no, se usa el EUR (precio de Deejay)."""
        tc_eur = Setting.get_tc()
        tc_usd = Setting.get_tc_usd()
        if self.costo_usd:
            return round(self.costo_usd * tc_usd, 2)
        return round((self.costo_eur or 0) * tc_eur, 2)

    @property
    def usa_costo_manual(self) -> bool:
        """True si el costo total viene de un valor manual, no del calculo."""
        return self.costo_uyu_manual is not None

    @property
    def discos_asignados(self) -> int:
        """Cuantos discos estan realmente vinculados a este pedido."""
        return len(self.discos)

    @property
    def discos_vendidos(self) -> int:
        """Cuantos discos del pedido ya se vendieron."""
        return sum(1 for d in self.discos if d.estado == "Vendido")

    @property
    def recaudado_uyu(self) -> float:
        """Lo recaudado por las ventas de los discos de este pedido."""
        total = 0.0
        for d in self.discos:
            if d.estado == "Vendido":
                total += (d.precio_mostrar or d.precio_uyu or 0) * d.copias_facturadas
        return round(total, 2)

    @property
    def recuperado(self) -> bool:
        """True si lo recaudado ya supero el costo total del pedido."""
        return self.recaudado_uyu >= self.costo_total_uyu and self.costo_total_uyu > 0

    @property
    def falta_recuperar_uyu(self) -> float:
        """Cuanto falta recaudar para cubrir el costo del pedido."""
        return round(max(0, self.costo_total_uyu - self.recaudado_uyu), 2)

    @property
    def resultado_uyu(self) -> float:
        """Ganancia (+) o perdida (-) del pedido hasta ahora:
        lo recaudado menos el costo total del pedido."""
        return round(self.recaudado_uyu - self.costo_total_uyu, 2)

    @property
    def resultado_potencial_uyu(self) -> float:
        """Ganancia proyectada si se vendiera TODO el stock del pedido
        a precio de lista, menos el costo total."""
        potencial = 0.0
        for d in self.discos:
            if d.estado == "Vendido":
                potencial += (d.precio_mostrar or d.precio_uyu or 0) * d.copias_facturadas
            else:
                potencial += (d.precio_mostrar or d.precio_uyu or 0) * (d.stock or 0)
        return round(potencial - self.costo_total_uyu, 2)

    @property
    def margen_pct(self) -> float:
        """Margen porcentual sobre lo invertido (resultado / costo * 100).
        Positivo = ganancia, negativo = perdida hasta ahora."""
        if self.costo_total_uyu <= 0:
            return 0.0
        return round(self.resultado_uyu / self.costo_total_uyu * 100, 1)

    @property
    def margen_potencial_pct(self) -> float:
        """Margen porcentual proyectado si se vende todo el stock."""
        if self.costo_total_uyu <= 0:
            return 0.0
        return round(self.resultado_potencial_uyu / self.costo_total_uyu * 100, 1)

    @property
    def dias_recuperacion(self):
        """Dias desde la fecha del pedido hasta que se recupero (o None si no)."""
        if not self.recuperado or not self.fecha:
            return None
        # Fecha de la ultima venta que completo la recuperacion
        vendidos = sorted(
            [d for d in self.discos if d.estado == "Vendido" and d.fecha_venta],
            key=lambda d: d.fecha_venta,
        )
        if not vendidos:
            return None
        # Acumular hasta cubrir el costo
        acum = 0.0
        for d in vendidos:
            acum += (d.precio_mostrar or d.precio_uyu or 0) * d.copias_facturadas
            if acum >= self.costo_total_uyu:
                delta = d.fecha_venta.date() - self.fecha
                return max(0, delta.days)
        return None

    @property
    def rotacion_pct(self) -> float:
        """Porcentaje de discos vendidos sobre los asignados."""
        if self.discos_asignados <= 0:
            return 0.0
        return round(self.discos_vendidos / self.discos_asignados * 100, 0)

    @property
    def rotacion_semaforo(self) -> str:
        """'verde' si vendio >=70%, 'amarillo' 30-70%, 'rojo' <30%."""
        r = self.rotacion_pct
        if r >= 70:
            return "verde"
        elif r >= 30:
            return "amarillo"
        return "rojo"

    @property
    def costo_por_disco_uyu(self) -> float:
        """Costo promedio por disco del pedido."""
        n = self.cant_discos or self.discos_asignados
        if n <= 0:
            return 0.0
        return round(self.costo_total_uyu / n, 2)

    def __repr__(self):
        return f"<Pedido {self.numero} {self.cant_discos} discos>"


# ---------------------------------------------------------------------------
# Inicializacion
# ---------------------------------------------------------------------------

from sqlalchemy import event as _sa_event
from sqlalchemy.engine import Engine as _SA_Engine


@_sa_event.listens_for(_SA_Engine, "connect")
def _sqlite_fk_pragma(dbapi_conn, connection_record):
    """Activa la verificacion de FOREIGN KEYS en SQLite (viene APAGADA por
    defecto). Sin esto los ON DELETE CASCADE y las FK no se respetan a nivel
    base de datos. Es best-effort: si el driver no es SQLite, se ignora."""
    try:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()
    except Exception:
        pass


@_sa_event.listens_for(Disc, "before_insert")
def _disc_asignar_orden(mapper, connection, target):
    """Disco nuevo -> arriba de todo en la tienda: orden = max(orden) actual + 1."""
    if getattr(target, "orden", None) is not None:
        return
    try:
        from sqlalchemy import text as _text
        maxo = connection.execute(_text("SELECT COALESCE(MAX(orden), 0) FROM discs")).scalar()
        target.orden = (maxo or 0) + 1
    except Exception:
        pass


def _migrate_schema(app):
    """Aplicar migraciones idempotentes a la DB existente.
    
    SQLAlchemy create_all() no agrega columnas nuevas a tablas existentes.
    Esta funcion verifica y agrega manualmente lo que falte.
    """
    from sqlalchemy import inspect, text
    
    with app.app_context():
        inspector = inspect(db.engine)

        # Columnas nuevas en la tabla movimientos_caja
        if inspector.has_table("movimientos_caja"):
            _mcols = [c["name"] for c in inspector.get_columns("movimientos_caja")]
            if "es_aporte" not in _mcols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE movimientos_caja ADD COLUMN es_aporte BOOLEAN DEFAULT 0"))
                    conn.commit()
                print("Migracion: columna es_aporte agregada a movimientos_caja")
            if "adelanto_mp" not in _mcols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE movimientos_caja ADD COLUMN adelanto_mp VARCHAR(20)"))
                    conn.commit()
                print("Migracion: columna adelanto_mp agregada a movimientos_caja")
            if "manual_override" not in _mcols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE movimientos_caja ADD COLUMN manual_override BOOLEAN DEFAULT 0"))
                    conn.commit()
                print("Migracion: columna manual_override agregada a movimientos_caja")
            if "origen_categoria" not in _mcols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE movimientos_caja ADD COLUMN origen_categoria VARCHAR(60)"))
                    conn.commit()
                print("Migracion: columna origen_categoria agregada a movimientos_caja")

        # Campos de pago Mercado Pago en la tabla reservas
        if inspector.has_table("reservas"):
            _rcols = [c["name"] for c in inspector.get_columns("reservas")]
            _radds = {
                "pagado": "BOOLEAN DEFAULT 0",
                "mp_preference_id": "VARCHAR(120)",
                "mp_payment_id": "VARCHAR(120)",
                "mp_status": "VARCHAR(30)",
                "total_cobrado_uyu": "FLOAT",
                "metodo_pago": "VARCHAR(20)",
                "comprobante_file": "VARCHAR(200)",
            }
            for _col, _ddl in _radds.items():
                if _col not in _rcols:
                    with db.engine.connect() as conn:
                        conn.execute(text(f"ALTER TABLE reservas ADD COLUMN {_col} {_ddl}"))
                        conn.commit()
                    print(f"Migracion: columna {_col} agregada a reservas")

        # Verificar columna descripcion_publica en discs
        if inspector.has_table("discs"):
            cols = [c["name"] for c in inspector.get_columns("discs")]
            if "descripcion_publica" not in cols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN descripcion_publica TEXT"))
                    conn.commit()
                print("Migracion: columna descripcion_publica agregada a discs")
            if "tags" not in cols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN tags VARCHAR(500)"))
                    conn.commit()
                print("Migracion: columna tags agregada a discs")
            if "pais" not in cols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN pais VARCHAR(80)"))
                    conn.commit()
                print("Migracion: columna pais agregada a discs")
            if "destacado" not in cols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN destacado BOOLEAN DEFAULT 0"))
                    conn.commit()
                print("Migracion: columna destacado agregada a discs")
            if "en_nuevos" not in cols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN en_nuevos BOOLEAN DEFAULT 0"))
                    conn.commit()
                print("Migracion: columna en_nuevos agregada a discs")
            if "cartel_nuevo" not in cols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN cartel_nuevo VARCHAR(10) DEFAULT 'auto'"))
                    conn.commit()
                print("Migracion: columna cartel_nuevo agregada a discs")
            if "cartel_ultima" not in cols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN cartel_ultima BOOLEAN DEFAULT 0"))
                    conn.commit()
                print("Migracion: columna cartel_ultima agregada a discs")
            if "precio_oferta" not in cols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN precio_oferta FLOAT"))
                    conn.commit()
                print("Migracion: columna precio_oferta agregada a discs")
            if "mostrar_agotado" not in cols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN mostrar_agotado BOOLEAN DEFAULT 0"))
                    conn.commit()
                print("Migracion: columna mostrar_agotado agregada a discs")
            if "es_preorder" not in cols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN es_preorder BOOLEAN DEFAULT 0"))
                    conn.commit()
                print("Migracion: columna es_preorder agregada a discs")
            if "copias_vendidas" not in cols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN copias_vendidas INTEGER DEFAULT 0"))
                    conn.commit()
                print("Migracion: columna copias_vendidas agregada a discs")
            if "pedido_id" not in cols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN pedido_id INTEGER"))
                    conn.commit()
                print("Migracion: columna pedido_id agregada a discs")
            # Para discos ya Vendidos sin copias registradas, asumir 1 copia
            with db.engine.connect() as conn:
                res = conn.execute(text(
                    "UPDATE discs SET copias_vendidas = 1 "
                    "WHERE estado = 'Vendido' AND (copias_vendidas IS NULL OR copias_vendidas = 0)"
                ))
                conn.commit()
                if res.rowcount:
                    print(f"Migracion: {res.rowcount} discos vendidos con copias_vendidas=1 por defecto")
            # Convertir discos "En camino" (estado viejo) en estado "Pre-order"
            with db.engine.connect() as conn:
                res = conn.execute(text(
                    "UPDATE discs SET estado = 'Pre-order' WHERE estado = 'En camino'"
                ))
                conn.commit()
                if res.rowcount:
                    print(f"Migracion: {res.rowcount} discos 'En camino' pasados a estado 'Pre-order'")
            # Los que estaban marcados es_preorder=1 con estado Disponible: pasarlos
            # a estado Pre-order (antes el flag significaba "es pre-order")
            with db.engine.connect() as conn:
                res = conn.execute(text(
                    "UPDATE discs SET estado = 'Pre-order' "
                    "WHERE es_preorder = 1 AND estado = 'Disponible'"
                ))
                conn.commit()
                if res.rowcount:
                    print(f"Migracion: {res.rowcount} discos con flag pre-order pasados a estado 'Pre-order'")
            if "origen" not in cols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN origen VARCHAR(20) DEFAULT 'arcano'"))
                    conn.commit()
                print("Migracion: columna origen agregada a discs")

        # Columna costo_uyu_manual en pedidos (monto real del banco)
        if inspector.has_table("pedidos"):
            pcols = [c["name"] for c in inspector.get_columns("pedidos")]
            if "costo_uyu_manual" not in pcols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE pedidos ADD COLUMN costo_uyu_manual FLOAT"))
                    conn.commit()
                print("Migracion: columna costo_uyu_manual agregada a pedidos")
            if "envio_eur" not in pcols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE pedidos ADD COLUMN envio_eur FLOAT"))
                    conn.commit()
                print("Migracion: columna envio_eur agregada a pedidos")
            if "servicio_eur" not in pcols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE pedidos ADD COLUMN servicio_eur FLOAT"))
                    conn.commit()
                print("Migracion: columna servicio_eur agregada a pedidos")

        # Columnas es_tip y resena_corta en discs (curaduria visible)
        if inspector.has_table("discs"):
            dcols = [c["name"] for c in inspector.get_columns("discs")]
            if "es_tip" not in dcols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN es_tip BOOLEAN DEFAULT 0"))
                    conn.commit()
                print("Migracion: columna es_tip agregada a discs")
            if "resena_corta" not in dcols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN resena_corta VARCHAR(160)"))
                    conn.commit()
                print("Migracion: columna resena_corta agregada a discs")
            if "audio_estado" not in dcols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN audio_estado VARCHAR(20)"))
                    conn.commit()
                print("Migracion: columna audio_estado agregada a discs")
            if "orden" not in dcols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN orden INTEGER"))
                    conn.commit()
                print("Migracion: columna orden agregada a discs")
            # Backfill del orden manual: los que no tienen orden toman su id
            # (los de id mas alto = mas nuevos = quedan arriba). Idempotente.
            with db.engine.connect() as conn:
                res = conn.execute(text("UPDATE discs SET orden = id WHERE orden IS NULL"))
                conn.commit()
                if res.rowcount:
                    print(f"Migracion: orden inicial asignado a {res.rowcount} discos")

        # Columna play_count en tracks (contador de escuchas)
        if inspector.has_table("tracks"):
            tcols = [c["name"] for c in inspector.get_columns("tracks")]
            if "play_count" not in tcols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE tracks ADD COLUMN play_count INTEGER DEFAULT 0"))
                    conn.commit()
                print("Migracion: columna play_count agregada a tracks")

        # Columna cliente_id en reservas (para cuentas de cliente)
        if inspector.has_table("reservas"):
            rcols = [c["name"] for c in inspector.get_columns("reservas")]
            if "cliente_id" not in rcols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE reservas ADD COLUMN cliente_id INTEGER"))
                    conn.commit()
                print("Migracion: columna cliente_id agregada a reservas")


def _migrate_tracklists_to_tracks(app):
    """Migra tracklists en texto plano a la tabla Track para discos que no tengan tracks aún.

    Se ejecuta una sola vez efectiva: si un disco ya tiene tracks, no se toca.
    """
    with app.app_context():
        try:
            discs_con_texto = Disc.query.filter(
                Disc.tracklist.isnot(None),
                Disc.tracklist != "",
            ).all()
        except Exception:
            return

        migrados = 0
        for disc in discs_con_texto:
            # Si ya tiene tracks en la tabla nueva, saltear
            if disc.tracks:
                continue
            items = parse_tracklist_text(disc.tracklist)
            for item in items:
                t = Track(
                    disc_id=disc.id,
                    posicion=item["posicion"],
                    lado=item["lado"],
                    titulo=item["titulo"],
                    artista=item["artista"],
                )
                db.session.add(t)
            migrados += 1
        if migrados:
            db.session.commit()
            print(f"Migracion: {migrados} disco(s) migraron su tracklist a la tabla Track")


def _migrate_reservas_a_items(app):
    """Convierte reservas viejas (un disco por columna) a ReservaItem.

    Por cada reserva que tenga disc_id o snapshot pero no tenga items,
    crea un ReservaItem con esos datos. Idempotente.
    """
    with app.app_context():
        try:
            reservas = Reserva.query.all()
        except Exception:
            return
        migradas = 0
        for r in reservas:
            if r.items:
                continue  # ya migrada
            if r.disc_id or r.disc_artista_snap:
                item = ReservaItem(
                    reserva_id=r.id,
                    disc_id=r.disc_id,
                    artista_snap=r.disc_artista_snap,
                    titulo_snap=r.disc_titulo_snap,
                    precio_uyu_snap=r.disc_precio_uyu_snap,
                )
                db.session.add(item)
                migradas += 1
        if migradas:
            db.session.commit()
            print(f"Migracion: {migradas} reserva(s) viejas convertidas a items")


def init_db(app) -> None:
    """Crea tablas si no existen y siembra settings por defecto."""
    db.init_app(app)
    _migrate_schema(app)
    with app.app_context():
        db.create_all()
        # Settings por defecto
        if Setting.get("tc_eur_uyu") is None:
            Setting.set("tc_eur_uyu", str(TC_DEFAULT))

        # Settings publicos (textos editables que se muestran en la tienda)
        defaults_publicos = {
            "public_tagline": "Vinilos de electronica seleccionados a mano. Montevideo.",
            "public_about": (
                "Arcano es una tienda de discos especializada en musica electronica: "
                "house, techno, minimal y progressive. Cada disco que llega a Arcano "
                "fue elegido cuidadosamente. Operamos desde Montevideo, Uruguay."
            ),
            "public_about_curaduria": "",
            "public_about_como_funciona": (
                "Reserva online tu disco. Coordinamos pago y entrega por mensaje. "
                "Hacemos envios a todo Uruguay."
            ),
            "public_envios": (
                "Coordinamos envio o retiro por mensaje directo. "
                "Hacemos envios a todo Uruguay."
            ),
            "public_instagram": "arcanorecordstore",
            "public_whatsapp": "",  # con codigo de pais, sin +, ej: 59899123456
            "api_cost_total_usd": "0",
            "api_calls_count": "0",
            "tc_usd_uyu": "40",
            "categorias_egreso_extra": "",
        }
        for k, v in defaults_publicos.items():
            if Setting.get(k) is None:
                Setting.set(k, v)

    # Migrar tracklists en texto a la tabla Track (fuera del with anterior porque necesita commit propio)
    _migrate_tracklists_to_tracks(app)
    # Migrar reservas viejas (un disco) al modelo multi-disco (ReservaItem)
    _migrate_reservas_a_items(app)
