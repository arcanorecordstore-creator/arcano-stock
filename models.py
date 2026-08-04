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
    # Mostrar en el carrusel "Pre-order" del inicio (toggle manual, como destacado).
    # Solo tiene efecto si ademas el estado es "Pre-order".
    es_preorder = db.Column(db.Boolean, nullable=False, default=False)
    # Cantidad de copias vendidas de este disco (para facturacion).
    # Permite registrar que se vendio mas de 1 copia del mismo titulo.
    copias_vendidas = db.Column(db.Integer, nullable=False, default=0)

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
        return round(self.precio_uyu - self.costo_final_uyu, 2)

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
        if self.precio_uyu <= 0:
            return 0.0
        return round((self.ganancia_uyu / self.precio_uyu) * 100, 2)

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

    @classmethod
    def get_tc(cls) -> float:
        """Helper: devuelve el tipo de cambio EUR->UYU como float."""
        raw = cls.get("tc_eur_uyu", str(TC_DEFAULT))
        try:
            return float(raw)
        except (TypeError, ValueError):
            return TC_DEFAULT


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
# Inicializacion
# ---------------------------------------------------------------------------

def _migrate_schema(app):
    """Aplicar migraciones idempotentes a la DB existente.
    
    SQLAlchemy create_all() no agrega columnas nuevas a tablas existentes.
    Esta funcion verifica y agrega manualmente lo que falte.
    """
    from sqlalchemy import inspect, text
    
    with app.app_context():
        inspector = inspect(db.engine)
        
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
        }
        for k, v in defaults_publicos.items():
            if Setting.get(k) is None:
                Setting.set(k, v)

    # Migrar tracklists en texto a la tabla Track (fuera del with anterior porque necesita commit propio)
    _migrate_tracklists_to_tracks(app)
    # Migrar reservas viejas (un disco) al modelo multi-disco (ReservaItem)
    _migrate_reservas_a_items(app)
