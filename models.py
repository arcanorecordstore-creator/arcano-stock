"""Modelos de base de datos para Arcano Stock."""
import re
from datetime import datetime, timedelta
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

# Uruguay esta en UTC-3 todo el ano (sin horario de verano desde 2015). El server
# corre en UTC, asi que para la "fecha de negocio" (cuando se vendio, cortes de mes)
# usamos estos helpers en vez de utcnow()/today(), que meten las ventas de la noche
# en el dia siguiente.
_UY_OFFSET = timedelta(hours=3)


def now_uy():
    """Fecha y hora actual en Uruguay (UTC-3)."""
    return datetime.utcnow() - _UY_OFFSET


def today_uy():
    """Fecha de hoy en Uruguay (UTC-3)."""
    return now_uy().date()


# ---------------------------------------------------------------------------
# Constantes de dominio
# ---------------------------------------------------------------------------

ESTADOS_DISCO = [
    "Disponible",
    "Pre-order",
    # Encargo: disco que un cliente pidio y Facundo le va a traer (acuerdo de palabra,
    # todavia nadie pago nada). En la tienda se ve y se reserva como un Pre-order;
    # en los numeros no cuenta hasta que se asigna a un pedido (igual que Pre-order).
    "Encargo",
    "Reservado",
    "Vendido",
    "Apartado para mi",
    "Descartado",
]

# Estados "por llegar": se ven en la tienda y se reservan sin stock fisico.
ESTADOS_PREVENTA = ("Pre-order", "Encargo")

_CATNO_RE = re.compile(r"^(?=[A-Z0-9.\-]*[A-Z])(?=[A-Z0-9.\-]*\d)[A-Z0-9.\-]{3,}$")


def sello_base(sello):
    """Nombre del sello sin el numero de catalogo pegado (como viene de Deejay.de):
    "Fresh Tunez TUNEZ005" -> "Fresh Tunez", "QRK015 Quirk" -> "Quirk". Solo se usa
    para AGRUPAR en el filtro de la tienda; lo guardado no se toca. Un "numero de
    catalogo" es una palabra en mayusculas con letras Y numeros (no "24 Seven")."""
    s = " ".join((sello or "").split())
    partes = s.split(" ")
    if len(partes) >= 2 and _CATNO_RE.match(partes[-1]):
        partes = partes[:-1]
    elif len(partes) >= 2 and _CATNO_RE.match(partes[0]):
        partes = partes[1:]
    return " ".join(partes)

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
# Mystery Box: producto especial (caja curada armada a mano). Precio segun la
# cantidad de discos elegida. Es la UNICA fuente de verdad de estos precios:
# el servidor los resuelve, el front nunca manda un precio. Solo cantidades 2-5.
# ---------------------------------------------------------------------------
MYSTERY_BOX_PRECIOS = {2: 2100, 3: 3000, 4: 3800, 5: 4500}


def mystery_box_precio(cantidad):
    """Precio (UYU) de una Mystery Box de N discos, o None si la cantidad no es valida."""
    try:
        return MYSTERY_BOX_PRECIOS.get(int(cantidad))
    except (TypeError, ValueError):
        return None


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
    # Ultimo dia de la oferta (v6.18). Vacio = sin fecha de fin. Pasado ese dia el disco
    # vuelve solo a su precio normal en toda la tienda.
    oferta_hasta = db.Column(db.Date, nullable=True, default=None)
    # Fire Sale: marca manual para incluir este disco cuando el alcance del
    # Fire Sale es "seleccion" (si el alcance es "todo", este flag se ignora).
    en_fire_sale = db.Column(db.Boolean, nullable=False, default=False)
    # "NO mostrar" en la tienda (v5.87): la fila existe para el admin y la contabilidad
    # (ej: la 2da copia de un disco repetido) pero no aparece en la web ni se puede
    # comprar. Cuando se vende la copia visible, se apaga y aparece esta.
    oculto_web = db.Column(db.Boolean, nullable=False, default=False)
    # Precio REALMENTE cobrado al venderse (se fija al pasar a Vendido). Para las
    # ventas web con Fire Sale/oferta, guarda lo que pago el cliente (el snapshot
    # de la reserva), asi la caja y la contabilidad cuentan la plata que entro de
    # verdad y no el precio de lista. Null en ventas viejas -> cae a precio_mostrar.
    precio_venta_uyu = db.Column(db.Float, nullable=True, default=None)
    # Direccion linda de la ficha (v6.05): arcanorecordstore.com/<slug>, armada con
    # artista + titulo. Las copias del mismo disco comparten la direccion.
    slug = db.Column(db.String(160), nullable=True, index=True)
    # Mostrar en el catalogo aunque este agotado/vendido (con sello "Agotado")
    mostrar_agotado = db.Column(db.Boolean, nullable=False, default=False)
    # Badge de recomendacion del curador (etiqueta editable en Ajustes, default "Tip").
    es_tip = db.Column(db.Boolean, nullable=False, default=False)
    # Mystery Box: producto especial (caja curada). Se muestra en el catalogo como
    # un disco mas (fijado primero) pero NO se vende como disco: al comprarse crea un
    # item de reserva SIN disc_id, asi no descuenta stock. Los discos se asignan a mano
    # despues desde la reserva. Solo existe una fila con este flag.
    es_mystery_box = db.Column(db.Boolean, nullable=False, default=False)
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
    # Comprador: a quien se le vendio (para la base de clientes / "que compro cada uno").
    # Se setea al vender (web, reserva manual o venta de IG); opcional.
    cliente_id = db.Column(db.Integer, db.ForeignKey("clientes.id", ondelete="SET NULL"),
                           nullable=True, index=True)
    comprador = db.relationship("Cliente", backref="discos_comprados", foreign_keys=[cliente_id])

    # Datos de la VENTA (se completan al vender; editables desde el panel de Ventas)
    # canal_venta: de donde vino la venta (web / instagram / presencial / otro)
    canal_venta = db.Column(db.String(20), nullable=True)
    # metodo_pago_venta: como pago el cliente (mercadopago / transferencia / efectivo / otro)
    metodo_pago_venta = db.Column(db.String(20), nullable=True)
    # venta_grupo: identificador de la TRANSACCION (varios discos vendidos juntos).
    # Web: se agrupa solo por reserva ('r<id>'). Instagram/manual: se agrupa a mano
    # desde el panel de Ventas ('g<n>'). Null = venta suelta (1 disco = 1 venta).
    venta_grupo = db.Column(db.String(40), nullable=True, index=True)

    # SENA de reserva: algunos clientes pagan una sena para reservar el disco; otros de
    # confianza reservan sin pagarla. Facundo lo marca a mano por disco. La sena cobrada
    # entra a la caja como plata real; el 'por cobrar' del disco muestra solo lo que falta;
    # al vender, la venta se cuenta NETA de la sena (para no contar esa plata dos veces).
    sena_cobrada = db.Column(db.Boolean, nullable=False, default=False)
    sena_monto_uyu = db.Column(db.Float, nullable=True)
    sena_fecha = db.Column(db.Date, nullable=True)
    sena_metodo = db.Column(db.String(20), nullable=True)

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
    def ajuste_tc_uyu(self) -> float:
        """Parte de la diferencia por tipo de cambio de SU pedido que le toca a este
        disco. El costo de cada disco se calcula con un TC aproximado (euros x TC al
        cargarlo); lo que salio de verdad de la cuenta es el costo del pedido. Esa
        diferencia se reparte en partes iguales entre los discos del pedido
        (decision de Facundo, 7/10/2026). 0 si el disco no esta en un pedido."""
        pid = getattr(self, "pedido_id", None)
        if not pid:
            return 0.0
        return ajustes_tc_por_pedido().get(pid, 0.0)

    @property
    def costo_real_uyu(self) -> float:
        """Costo real del disco = costo cargado (euros x TC) + su parte de la diferencia
        por tipo de cambio del pedido. Es el que usan la contabilidad, las ganancias y
        los margenes. costo_final_uyu (lo cargado) no se toca."""
        return round((self.costo_final_uyu or 0) + self.ajuste_tc_uyu, 2)

    @property
    def ganancia_uyu(self) -> float:
        # Usa el precio de oferta si el disco esta en oferta (es lo que se cobra),
        # no el precio de lista, asi el margen no queda inflado.
        return round((self.precio_mostrar or self.precio_uyu or 0) - self.costo_real_uyu, 2)

    @property
    def ganancia_negocio_uyu(self) -> float:
        """Ganancia con el criterio del NEGOCIO: los discos de origen 'personal' no restan
        costo (esa plata no la puso el negocio). Coincide con el KPI del panel de Ventas y
        con la Contabilidad; por eso la suma de las filas cuadra con el total."""
        costo = 0.0 if (self.origen or "arcano") == "personal" else self.costo_real_uyu
        return round(((self.precio_mostrar or self.precio_uyu or 0) - costo) * (self.copias_facturadas or 1), 2)

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
        """True si el disco esta en Pre-order o Encargo (muestra cartel y se reserva).
        Un pre-order que ya tomo un pedido web (preorder_tomado) deja de ofrecerse."""
        return self.estado in ESTADOS_PREVENTA and not self.preorder_tomado

    @property
    def preorder_tomado(self) -> bool:
        """Pre-order / Encargo que ya pidio alguien desde la web (transferencia o
        "reservar y coordinar"): queda atado a ese pedido con venta_grupo = "r<id>".
        Una fila = una copia, asi que ya no se le puede vender a otra persona."""
        return self.estado in ESTADOS_PREVENTA and bool(getattr(self, "venta_grupo", None))

    @property
    def en_carrusel_preorder(self) -> bool:
        """True si debe salir en el carrusel Pre-order del inicio:
        tiene que estar en estado Pre-order Y con el toggle prendido."""
        return (self.estado in ESTADOS_PREVENTA and bool(getattr(self, "es_preorder", False))
                and not self.preorder_tomado)

    @property
    def en_oferta(self) -> bool:
        """True si tiene un precio de oferta valido (menor al precio normal) y, si la
        oferta tiene fecha de fin, todavia no paso."""
        po = getattr(self, "precio_oferta", None)
        if not (po and po > 0 and self.precio_uyu and po < self.precio_uyu):
            return False
        hasta = getattr(self, "oferta_hasta", None)
        return not (hasta and today_uy() > hasta)

    @property
    def precio_mostrar(self) -> float:
        """Precio a cobrar: el de oferta si aplica, si no el normal.

        Para un disco YA VENDIDO que guardo su precio de venta real
        (precio_venta_uyu), devuelve ese: asi la caja y la contabilidad cuentan
        lo que efectivamente entro (con Fire Sale/oferta), no el precio de lista.
        """
        if self.estado == "Vendido" and getattr(self, "precio_venta_uyu", None) is not None:
            return self.precio_venta_uyu
        return self.precio_oferta if self.en_oferta else self.precio_uyu

    @property
    def sena_efectiva(self) -> float:
        """Monto de sena de reserva efectivamente cobrado (0 si no esta cobrada).
        Es plata real que ya entro: se descuenta del 'por cobrar' y se resta de la
        venta al concretarse (para no contar esa plata dos veces)."""
        if getattr(self, "sena_cobrada", False):
            return round(self.sena_monto_uyu or 0, 2)
        return 0.0

    @property
    def precio_falta_uyu(self) -> float:
        """Lo que todavia falta cobrar de este disco: el precio a cobrar menos la
        sena ya cobrada. Nunca negativo."""
        base = self.precio_mostrar or self.precio_uyu or 0
        return round(max(0.0, base - self.sena_efectiva), 2)

    @property
    def descuento_pct(self) -> int:
        """Porcentaje de descuento (entero) si esta en oferta."""
        if not self.en_oferta:
            return 0
        return round((1 - (self.precio_oferta / self.precio_uyu)) * 100)

    @property
    def en_fire_sale_activo(self) -> bool:
        """True si este disco esta AHORA en Fire Sale vigente (segun la config
        global + su alcance + el reloj). Los agotados no entran."""
        cfg = fire_sale_config()
        if not cfg["vigente"] or self.esta_agotado:
            return False
        if cfg["alcance"] == "todo":
            return True
        return bool(getattr(self, "en_fire_sale", False))

    @property
    def precio_fire_sale(self) -> float:
        """Precio con el descuento del Fire Sale aplicado sobre el precio normal
        a cobrar (precio_mostrar). Redondeado a peso entero."""
        cfg = fire_sale_config()
        base = self.precio_mostrar or self.precio_uyu or 0
        pct = cfg["pct"] or 0
        return round(base * (1 - pct / 100.0))

    @property
    def fire_sale_pct(self) -> int:
        """Porcentaje de descuento del Fire Sale vigente (0 si no aplica a este disco)."""
        if not self.en_fire_sale_activo:
            return 0
        return int(fire_sale_config()["pct"] or 0)

    @property
    def esta_agotado(self) -> bool:
        """True si el disco no esta disponible para comprar.
        Los pre-orders y encargos NO se consideran agotados (se pueden reservar)."""
        if self.estado in ESTADOS_PREVENTA:
            return self.preorder_tomado
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
                # Si cambia algo del Fire Sale, tirar su cache calculado
                if key.startswith("fire_sale"):
                    if hasattr(g, "_fire_sale_cfg"):
                        delattr(g, "_fire_sale_cfg")
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


def ajustes_tc_por_pedido() -> dict:
    """{pedido_id: ajuste por disco en UYU}. Para cada pedido con costo real cargado
    (costo manual = lo que salio de la cuenta), la diferencia entre ese costo (+ la
    comision del banco) y la suma de los costos de sus discos se reparte en partes
    iguales entre los discos del pedido. Sin costo manual solo se reparte la comision.
    Se calcula una vez por pedido web (cache en flask.g)."""
    from flask import g, has_request_context
    if has_request_context():
        cached = getattr(g, "_ajuste_tc_cache", None)
        if cached is not None:
            return cached
    res = {}
    try:
        filas = (db.session.query(Disc.pedido_id, db.func.count(Disc.id),
                                  db.func.sum(Disc.costo_final_uyu))
                 .filter(Disc.pedido_id.isnot(None))
                 .group_by(Disc.pedido_id).all())
        por_pedido = {pid: (n or 0, float(suma or 0)) for pid, n, suma in filas}
        if por_pedido:
            for ped in Pedido.query.filter(Pedido.id.in_(list(por_pedido))).all():
                n, suma = por_pedido[ped.id]
                if n <= 0:
                    continue
                if ped.usa_costo_manual:
                    dif = (ped.costo_total_uyu or 0) - suma
                else:
                    dif = ped.comision_uyu or 0
                if dif:
                    res[ped.id] = dif / n
    except Exception:
        res = {}
    if has_request_context():
        g._ajuste_tc_cache = res
    return res


def invalidar_ajuste_tc() -> None:
    """Olvida el reparto calculado en este pedido web (despues de cambiar un costo)."""
    from flask import g, has_request_context
    if has_request_context() and hasattr(g, "_ajuste_tc_cache"):
        del g._ajuste_tc_cache


def fire_sale_config() -> dict:
    """Lee la configuracion del Fire Sale una sola vez por request.

    Devuelve un dict con:
      - activa: el toggle esta prendido
      - pct: porcentaje de descuento (int)
      - alcance: 'todo' | 'seleccion'
      - fin: datetime de fin (hora local UY) o None si no tiene reloj
      - vigente: activa Y pct>0 Y (sin reloj o el reloj todavia no vencio)
      - segundos_restantes: cuanto falta para que termine (o None)
    """
    from flask import g, has_request_context
    if has_request_context():
        cached = getattr(g, "_fire_sale_cfg", None)
        if cached is not None:
            return cached

    activa = Setting.get("fire_sale_activa", "0") == "1"
    try:
        pct = int(float(Setting.get("fire_sale_pct", "0") or 0))
    except (TypeError, ValueError):
        pct = 0
    alcance = Setting.get("fire_sale_alcance", "todo") or "todo"
    fin_raw = (Setting.get("fire_sale_fin", "") or "").strip()

    fin = None
    segundos = None
    reloj_vencido = False
    if fin_raw:
        try:
            fin = datetime.fromisoformat(fin_raw)
            ahora = now_uy()
            delta = (fin - ahora).total_seconds()
            segundos = int(delta)
            reloj_vencido = delta <= 0
        except (TypeError, ValueError):
            fin = None

    vigente = bool(activa and pct > 0 and not reloj_vencido)
    cfg = {
        "activa": activa,
        "pct": pct,
        "alcance": alcance,
        "fin": fin,
        "vigente": vigente,
        "segundos_restantes": segundos if (vigente and segundos and segundos > 0) else None,
    }
    if has_request_context():
        g._fire_sale_cfg = cfg
    return cfg


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

class EventoDisco(db.Model):
    """Visitas a la ficha y 'agregar al carrito' de cada disco (v6.19), para el embudo
    por disco del Radar. Tabla nueva (create_all). No guarda datos personales: solo
    disco, tipo y fecha. Sin bots, previsualizaciones ni recargas de la misma persona."""
    __tablename__ = "eventos_disco"

    id = db.Column(db.Integer, primary_key=True)
    disc_id = db.Column(db.Integer, db.ForeignKey("discs.id", ondelete="CASCADE"), nullable=False, index=True)
    tipo = db.Column(db.String(12), nullable=False, index=True)    # vista | carrito
    fecha = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)


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


class SearchQuery(db.Model):
    """Cada búsqueda de texto real de la tienda pública, con cuántos resultados dio.

    Alimenta el panel "Demanda no satisfecha" del Radar: lo que la gente busca y
    NO encuentra (resultados=0) es la lista de compras para el próximo pedido.
    """

    __tablename__ = "search_queries"

    id = db.Column(db.Integer, primary_key=True)
    termino = db.Column(db.String(120), nullable=False, index=True)
    resultados = db.Column(db.Integer, nullable=False, default=0)
    fecha = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)


class Cliente(db.Model):
    """Cuenta de cliente de la tienda publica.

    Separado del login de admin (que usa .env). Las contrasenas se guardan
    hasheadas con werkzeug. Un cliente puede tener varias reservas.
    """

    __tablename__ = "clientes"

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(200), nullable=False)
    # email/password son OPCIONALES: un cliente cargado a mano (Instagram) puede no tener
    # cuenta web. La unicidad de email (para las cuentas web) se controla en el codigo, no
    # con constraint de DB (por los NULL de los clientes manuales).
    email = db.Column(db.String(200), nullable=True, index=True)
    whatsapp = db.Column(db.String(50), nullable=True, index=True)
    instagram = db.Column(db.String(100), nullable=True)   # @usuario (sin el @ guardado)
    notas = db.Column(db.Text, nullable=True)
    origen = db.Column(db.String(20), nullable=False, default="web")  # web / manual
    password_hash = db.Column(db.String(255), nullable=True)
    fecha_registro = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    # Programa de lealtad (barra de premios). lealtad_base = gasto acumulado al
    # inicio del ciclo actual (el progreso = gasto_total - lealtad_base). Al
    # cobrar el premio del 2do hito, base += umbral2 (deja el excedente a favor).
    lealtad_base = db.Column(db.Integer, nullable=False, default=0)
    # Si ya se le entrego el premio del 1er hito en el ciclo actual.
    premio1_cobrado = db.Column(db.Boolean, nullable=False, default=False)
    # Acepta recibir avisos (nuevos ingresos, descuentos). Default True (opt-out):
    # Facundo puede desmarcar a quien pida no recibir.
    acepta_avisos = db.Column(db.Boolean, nullable=False, default=True)

    def set_password(self, raw: str) -> None:
        from werkzeug.security import generate_password_hash
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        from werkzeug.security import check_password_hash
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, raw)

    @property
    def tiene_cuenta(self) -> bool:
        """True si el cliente se registro con contrasena (cuenta web activa).
        Los cargados a mano o auto-generados por una venta anonima no tienen."""
        return bool(self.password_hash)

    @property
    def compras(self) -> list:
        """Discos que este cliente compro (vendidos y atribuidos a el)."""
        return [d for d in getattr(self, "discos_comprados", []) if d.estado == "Vendido"]

    @property
    def total_gastado(self) -> float:
        return sum((d.precio_mostrar or d.precio_uyu or 0) for d in self.compras)

    @property
    def cantidad_compras(self) -> int:
        return len(self.compras)

    @property
    def ultima_compra(self):
        fechas = [d.fecha_venta for d in self.compras if d.fecha_venta]
        return max(fechas) if fechas else None

    @property
    def tiene_cuenta_web(self) -> bool:
        return bool(self.password_hash)

    def __repr__(self) -> str:
        return f"<Cliente {self.id}: {self.nombre}>"


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


class PushSubscription(db.Model):
    """Suscripcion de un dispositivo del admin a los avisos push (Web Push).
    Cada celular/navegador donde Facundo activa los avisos guarda una fila."""
    __tablename__ = "push_subscriptions"

    id = db.Column(db.Integer, primary_key=True)
    endpoint = db.Column(db.Text, nullable=False, unique=True)
    p256dh = db.Column(db.String(200), nullable=False)
    auth = db.Column(db.String(100), nullable=False)
    creado = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


# Estados del pedido = estado del PAGO. La entrega (envio/pickup + entregado) va
# aparte, no toca la plata. La venta (disco Vendido, entra a caja/contabilidad)
# se registra al pasar a "Pagada".
ESTADOS_RESERVA = [
    "Pendiente",      # todavia no pago (el disco queda apartado / Reservado)
    "Pagada",         # pago (por Mercado Pago o transferencia) -> se marca la venta
    "Cancelada",      # se cancelo (el disco vuelve a Disponible)
]


class Reserva(db.Model):
    """Una reserva hecha por un cliente desde la tienda publica."""

    __tablename__ = "reservas"
    # None conserva el formato historico: web UTC, manual hora local.
    # Nuevas reservas siempre guardan fecha UTC, sin reescribir fechas antiguas.
    fecha_es_utc = db.Column(db.Boolean, nullable=True, default=True)

    @property
    def fecha_utc(self):
        if self.fecha is None:
            return None
        if self.fecha_es_utc is False or (self.fecha_es_utc is None and self.canal != 'web'):
            return self.fecha + _UY_OFFSET
        return self.fecha

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

    # Entrega / envio (solo en COMPRAS; las reservas a coordinar se arreglan por WhatsApp)
    entrega_tipo = db.Column(db.String(20), nullable=True)   # retiro / montevideo / interior
    envio_costo_uyu = db.Column(db.Float, nullable=False, default=0.0)
    envio_direccion = db.Column(db.String(400), nullable=True)   # Mvd: direccion; Interior: direccion/localidad
    envio_agencia = db.Column(db.String(200), nullable=True)     # solo interior
    # Estado de la ENTREGA fisica (aparte del pago): el cliente ya recibio el disco?
    entregado = db.Column(db.Boolean, nullable=False, default=False)
    # Canal por el que llego la reserva: web (automatica) / instagram / presencial / otro.
    # Al vender los discos de la reserva, este canal se copia a Disc.canal_venta -> aparece
    # en el panel de Ventas por ese canal. Las reservas web quedan en 'web'.
    canal = db.Column(db.String(20), nullable=False, default="web")
    # Agrupacion manual de pedidos: varias reservas hechas en momentos distintos que
    # comparten 'grupo' se tratan como UN pedido (tipico: unificar en un solo envio).
    # Analogo a Disc.venta_grupo. Null = reserva suelta.
    grupo = db.Column(db.String(40), nullable=True, index=True)

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
    def sena_cobrada_total(self) -> float:
        """Suma de las senas ya cobradas de los discos de esta reserva (0 si no hubo)."""
        tot = 0.0
        for it in self.items_efectivos:
            d = it.disco
            if d is not None:
                tot += d.sena_efectiva
        return round(tot, 2)

    @property
    def falta_cobrar_uyu(self) -> float:
        """Lo que el cliente todavia debe de esta reserva: total menos la sena.
        Si la reserva ya esta Pagada (o Cancelada) no debe nada."""
        if self.estado != "Pendiente":
            return 0.0
        return round(max(0.0, self.total_uyu - self.sena_cobrada_total), 2)

    @property
    def cantidad_discos(self) -> int:
        return len(self.items_efectivos)

    @property
    def total_con_envio(self) -> float:
        """Total de los discos + el envio cobrado (si lo hubo)."""
        return (self.total_uyu or 0) + (self.envio_costo_uyu or 0)

    @property
    def total_final(self) -> float:
        """Lo que el cliente paga de verdad: si pago por Mercado Pago, el total cobrado
        (discos + envio + recargo); si no, discos + envio."""
        if (self.metodo_pago or "") == "mercadopago" and self.total_cobrado_uyu:
            return round(float(self.total_cobrado_uyu), 2)
        return round(self.total_con_envio or 0, 2)

    @property
    def recargo_mp(self) -> float:
        """Recargo de Mercado Pago incluido en el total cobrado (0 si no aplica)."""
        if (self.metodo_pago or "") == "mercadopago" and self.total_cobrado_uyu:
            r = float(self.total_cobrado_uyu) - (self.total_con_envio or 0)
            return round(r, 2) if r > 0.5 else 0.0
        return 0.0

    @property
    def entrega_label(self) -> str:
        """Texto lindo de la entrega elegida (para admin/mails)."""
        if self.entrega_tipo == "retiro":
            return "Retiro en Pocitos"
        if self.entrega_tipo == "montevideo":
            return "Envio en Montevideo"
        if self.entrega_tipo == "interior":
            return "Envio al interior"
        return ""

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

    # Mystery Box: si este item es una caja curada, guarda la cantidad de discos
    # elegida y el texto del cliente (tracks/artistas/sellos que le gustan). El item
    # va SIN disc_id (los discos se asignan a mano despues), asi el flujo de venta
    # no le descuenta stock a nada.
    mbox_cantidad = db.Column(db.Integer, nullable=True)
    mbox_texto = db.Column(db.Text, nullable=True)

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

    @property
    def es_mystery_box(self) -> bool:
        """True si este item es una Mystery Box (caja curada, sin disco asignado)."""
        return self.mbox_cantidad is not None

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
# Categoria especial: NO es un gasto operativo del negocio, es plata que Facundo
# saca para si mismo. Afecta el capital al reves que un aporte (lo resta) y sale de
# la caja, pero NO baja la Ganancia real (no es un costo de vender discos).
CAT_RETIRO_PERSONAL = "Retiro personal"

CATEGORIAS_EGRESO_DEFAULT = [
    "Hosting",
    "Publicidad",
    "Envios",
    "Viajes",
    "Otros",
    CAT_RETIRO_PERSONAL,
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
            "sena": "Sena",
            "pedido": "Pedido",
            "gasto": "Gasto",
            "envio": "Envio",
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
    # Comisión del banco (en UYU) que se cobró por el pago del pedido. SUMA al total:
    # es plata real que saliste pagando además del costo de los discos.
    comision_banco_uyu = db.Column(db.Float, nullable=True)
    # Link del seguimiento del paquete (DHL, UPS, correo...), v6.01
    seguimiento_url = db.Column(db.String(500), nullable=True)
    creado = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    # Relacion con los discos asignados a este pedido
    discos = db.relationship("Disc", backref="pedido", lazy=True)

    @property
    def costo_pedido_uyu(self) -> float:
        """Costo de los discos del pedido en UYU, SIN la comisión del banco. Si hay un
        monto manual (lo que debitó el banco por los discos), se usa ese; si no, se
        calcula desde la moneda del pedido."""
        if self.costo_uyu_manual is not None:
            return round(self.costo_uyu_manual, 2)
        return self.costo_total_uyu_calculado

    @property
    def comision_uyu(self) -> float:
        """Comisión del banco en UYU (0 si no se cargó)."""
        return round(self.comision_banco_uyu or 0.0, 2)

    @property
    def costo_total_uyu(self) -> float:
        """Costo TOTAL real del pedido en UYU = costo del pedido + comisión del banco.
        Es lo que realmente saliste pagando; alimenta la caja, el resultado y la
        recuperación."""
        return round(self.costo_pedido_uyu + self.comision_uyu, 2)

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
    def discos_para_recuperar(self) -> dict:
        """Cuantos discos del pedido hay que vender para recuperar lo que costo.

        - total: cuantos hacen falta en total (los ya vendidos + los que faltan).
        - faltan: cuantos quedan por vender, calculado con el precio PROMEDIO de los
          que todavia no se vendieron (es una estimacion: depende de cuales salgan).
        - rango: (minimo, maximo) si vendes primero los mas caros / los mas baratos.
        - alcanza: False si ni vendiendo todo lo que queda se cubre el costo.
        Cuenta solo lo que esta a la venta (no Vendido, no Descartado, no
        'Apartado para mi') y con precio puesto; una unidad por copia en stock."""
        import math
        costo = self.costo_total_uyu or 0
        vendidos = self.discos_vendidos
        falta = self.falta_recuperar_uyu
        precios = []
        for d in self.discos:
            if d.estado in ("Vendido", "Descartado", "Apartado para mi"):
                continue
            precio = d.precio_mostrar or d.precio_uyu or 0
            if precio > 0:
                precios += [precio] * max(1, d.stock or 0)
        res = {"faltan": 0, "total": vendidos, "rango": None, "alcanza": True,
               "a_la_venta": len(precios), "promedio": 0, "sin_datos": costo <= 0}
        if costo <= 0 or falta <= 0:
            return res
        if not precios:
            res.update(alcanza=False, faltan=None, total=None)
            return res
        prom = sum(precios) / len(precios)
        res["promedio"] = round(prom)
        if sum(precios) < falta:
            res.update(alcanza=False, faltan=None, total=None,
                       quedaria=round(falta - sum(precios)))
            return res

        def _cuantos(lista):
            acum = 0.0
            for i, p in enumerate(lista, 1):
                acum += p
                if acum >= falta:
                    return i
            return len(lista)
        faltan = min(len(precios), math.ceil(falta / prom))
        minimo = _cuantos(sorted(precios, reverse=True))
        maximo = _cuantos(sorted(precios))
        res.update(faltan=faltan, total=vendidos + faltan,
                   rango=(minimo, maximo) if minimo != maximo else None)
        return res

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
# Tareas de Facundo para Arcano (to-do del admin)
# ---------------------------------------------------------------------------

ESTADOS_TAREA = ["Sin empezar", "En curso", "Hecha"]
PRIORIDADES_TAREA = {0: "Sin prioridad", 1: "Baja", 2: "Media", 3: "Alta", 4: "Urgente"}
CATEGORIAS_TAREA = ["Compras", "Contenido", "Instagram", "Web", "Envíos", "Clientes", "Admin"]


def formato_duracion(minutos) -> str:
    """Minutos -> texto corto: 45 -> '45 min', 120 -> '2 h', 90 -> '1 h 30'."""
    if not minutos:
        return ""
    h, m = divmod(int(minutos), 60)
    if not h:
        return f"{m} min"
    return f"{h} h" + (f" {m:02d}" if m else "")


def parsear_duracion(texto):
    """Texto de duracion -> minutos (int), None si esta vacio, o ValueError si no
    se entiende. Acepta: '45', '45 min', '45m', '2h', '2 hs', '2 horas', '1.5 h',
    '1,5h', '1h30', '1 h 30 min', '1:30'. Un numero solo se toma como minutos."""
    import re as _re
    s = (texto or "").strip().lower().replace(",", ".")
    if not s:
        return None
    m = _re.fullmatch(r"(\d+):(\d{1,2})", s)
    if m:
        total = int(m.group(1)) * 60 + int(m.group(2))
    else:
        m = _re.fullmatch(
            r"(?:(\d+(?:\.\d+)?)\s*(?:h|hs|hr|hrs|hora|horas)\.?)?\s*(?:y\s*)?"
            r"(?:(\d+)\s*(?:m|min|mins|minuto|minutos)?\.?)?", s)
        if not m or (m.group(1) is None and m.group(2) is None):
            raise ValueError("No entendí la duración. Escribila como 45 min, 2 h o 1 h 30.")
        total = round(float(m.group(1) or 0) * 60) + int(m.group(2) or 0)
    if total > 1000 * 60:
        raise ValueError("La duración es demasiado larga.")
    return total or None


class Tarea(db.Model):
    """Una tarea pendiente para Arcano. Tabla nueva (create_all), no toca nada mas."""
    __tablename__ = "tareas"

    id = db.Column(db.Integer, primary_key=True)
    titulo = db.Column(db.String(200), nullable=False)
    detalle = db.Column(db.Text, nullable=True)
    categoria = db.Column(db.String(60), nullable=True)
    estado = db.Column(db.String(20), nullable=False, default="Sin empezar")
    agendada = db.Column(db.Boolean, nullable=False, default=False)
    prioridad = db.Column(db.Integer, nullable=False, default=0)   # 0-4
    fecha_limite = db.Column(db.Date, nullable=True)
    progreso = db.Column(db.Integer, nullable=True)   # ej: 3 (de meta)
    meta = db.Column(db.Integer, nullable=True)       # ej: 5
    creada = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    hecha_en = db.Column(db.DateTime, nullable=True)
    duracion_min = db.Column(db.Integer, nullable=True)   # duracion estimada en minutos (v5.70)

    subtareas = db.relationship("Subtarea", backref="tarea", cascade="all, delete-orphan",
                                order_by="Subtarea.orden, Subtarea.id", lazy="select")

    @property
    def hecha(self) -> bool:
        return self.estado == "Hecha"

    @property
    def subtareas_resumen(self):
        """(hechas, total) de las subtareas, o None si no tiene."""
        subs = self.subtareas
        if not subs:
            return None
        return (sum(1 for x in subs if x.hecha), len(subs))

    def dias_restantes(self, hoy):
        """Dias hasta la fecha limite (negativo = vencida). None si no tiene fecha."""
        if not self.fecha_limite:
            return None
        return (self.fecha_limite - hoy).days

    @property
    def duracion_txt(self) -> str:
        """Duracion estimada legible: '45 min', '2 h', '1 h 30'."""
        return formato_duracion(self.duracion_min)

    @property
    def avance_pct(self):
        """% de avance si tiene meta (progreso / meta), si no None."""
        if not self.meta or self.meta <= 0:
            return None
        return max(0, min(100, round((self.progreso or 0) / self.meta * 100)))

    def __repr__(self):
        return f"<Tarea {self.id} {self.titulo!r} {self.estado}>"


class Subtarea(db.Model):
    """Paso de una tarea (checklist dentro del panel de la tarea). Tabla nueva."""
    __tablename__ = "subtareas"

    id = db.Column(db.Integer, primary_key=True)
    tarea_id = db.Column(db.Integer, db.ForeignKey("tareas.id", ondelete="CASCADE"), nullable=False, index=True)
    texto = db.Column(db.String(300), nullable=False)
    hecha = db.Column(db.Boolean, nullable=False, default=False)
    orden = db.Column(db.Integer, nullable=False, default=0)
    creada = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def to_dict(self):
        return {"id": self.id, "texto": self.texto, "hecha": bool(self.hecha)}


FORMATOS_IDEA = ["Reel", "Historia", "Posteo", "Carrusel", "Unboxing", "Otro"]
ESTADOS_IDEA = ["Idea", "En producción", "Publicada"]


class Nota(db.Model):
    """Nota libre de Estrategia (v6.14): cuentas, ideas sueltas, lo que se te ocurra.
    Tabla nueva (create_all), no toca nada mas. Se guarda sola mientras escribis."""
    __tablename__ = "notas"

    id = db.Column(db.Integer, primary_key=True)
    titulo = db.Column(db.String(200), nullable=True)
    texto = db.Column(db.Text, nullable=True)
    fijada = db.Column(db.Boolean, nullable=False, default=False)
    creada = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    actualizada = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    @property
    def titulo_mostrar(self) -> str:
        """El titulo, o la primera linea del texto si no le pusiste titulo."""
        if (self.titulo or "").strip():
            return self.titulo.strip()
        for ln in (self.texto or "").splitlines():
            if ln.strip():
                return ln.strip().lstrip("#-*• ").strip()[:120]
        return "Nota sin título"

    @property
    def vista_previa(self) -> str:
        """Unas palabras del texto (sin la linea que ya se usa como titulo)."""
        lineas = [ln.strip() for ln in (self.texto or "").splitlines() if ln.strip()]
        if not (self.titulo or "").strip() and lineas:
            lineas = lineas[1:]
        return " · ".join(lineas)[:160]

    def to_dict(self) -> dict:
        return {
            "id": self.id, "titulo": self.titulo or "", "texto": self.texto or "",
            "fijada": bool(self.fijada), "titulo_mostrar": self.titulo_mostrar,
            "vista_previa": self.vista_previa,
            "actualizada": self.actualizada.isoformat() if self.actualizada else None,
        }


class PreguntaIA(db.Model):
    """Estrategia -> Preguntar: cada pregunta que Facundo le hizo a la IA sobre sus
    datos, con la respuesta (texto con formato simple). Sirve de historial para volver
    a verlas y para el tope de uso por hora. Tabla nueva (create_all), no toca nada mas.
    Solo se guardan las preguntas que llegaron a la IA (con respuesta o con error)."""
    __tablename__ = "preguntas_ia"

    id = db.Column(db.Integer, primary_key=True)
    fecha = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    pregunta = db.Column(db.Text, nullable=False)
    respuesta = db.Column(db.Text, nullable=True)        # None si fallo
    error = db.Column(db.String(300), nullable=True)     # motivo si fallo
    tokens_entrada = db.Column(db.Integer, nullable=False, default=0)
    tokens_salida = db.Column(db.Integer, nullable=False, default=0)
    costo_usd = db.Column(db.Float, nullable=False, default=0.0)
    segundos = db.Column(db.Float, nullable=True)

    @property
    def ok(self) -> bool:
        return bool(self.respuesta) and not self.error


class IdeaContenido(db.Model):
    """Idea para crear contenido (redes). Tabla nueva (create_all), no toca nada mas."""
    __tablename__ = "ideas_contenido"

    id = db.Column(db.Integer, primary_key=True)
    titulo = db.Column(db.String(200), nullable=False)
    detalle = db.Column(db.Text, nullable=True)
    formato = db.Column(db.String(30), nullable=True)
    estado = db.Column(db.String(20), nullable=False, default="Idea")
    creada = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    publicada_en = db.Column(db.DateTime, nullable=True)
    tarea_id = db.Column(db.Integer, nullable=True)   # tarea creada a partir de la idea
    puntaje = db.Column(db.Integer, nullable=True)    # 1 a 5 estrellas (que tan buena es); None = sin puntuar

    @property
    def estado_key(self) -> str:
        return {"Idea": "nueva", "En producción": "curso", "Publicada": "hecha"}.get(self.estado, "nueva")

    def __repr__(self):
        return f"<IdeaContenido {self.id} {self.titulo!r} {self.estado}>"


FRECUENCIAS_HABITO = {"dia": "Por día", "semana": "Por semana", "dias": "Cada N días"}


class HabitoContenido(db.Model):
    """Meta de constancia de contenido (ej: '2 videos de discos al dia x 4 dias').
    frecuencia: 'dia' (cantidad por dia, dias_semana dias por semana), 'semana'
    (cantidad por semana) o 'dias' (cantidad cada `cada_dias` dias)."""
    __tablename__ = "habitos_contenido"

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), nullable=False)
    cantidad = db.Column(db.Integer, nullable=False, default=1)
    frecuencia = db.Column(db.String(10), nullable=False, default="dia")
    dias_semana = db.Column(db.Integer, nullable=False, default=7)
    cada_dias = db.Column(db.Integer, nullable=False, default=15)
    orden = db.Column(db.Integer, nullable=False, default=0)
    creado = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    registros = db.relationship("HabitoRegistro", backref="habito", cascade="all, delete-orphan",
                                lazy="select")


class HabitoRegistro(db.Model):
    """Cuantas publicaciones de un habito se hicieron un dia dado."""
    __tablename__ = "habitos_registro"
    __table_args__ = (db.UniqueConstraint("habito_id", "fecha", name="uq_habito_fecha"),)

    id = db.Column(db.Integer, primary_key=True)
    habito_id = db.Column(db.Integer, db.ForeignKey("habitos_contenido.id", ondelete="CASCADE"), nullable=False, index=True)
    fecha = db.Column(db.Date, nullable=False)
    cantidad = db.Column(db.Integer, nullable=False, default=0)


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


@_sa_event.listens_for(Disc, "before_insert")
@_sa_event.listens_for(Disc, "before_update")
def _disc_autofecha_venta(mapper, connection, target):
    """Si el disco queda en estado 'Vendido' sin fecha de venta, se la ponemos
    sola (la del momento). Cubre TODOS los caminos: form manual, quick-edit,
    bulk, completar reserva, venta manual de IG. Si ya tiene fecha, se respeta
    (Facundo puede fijar una fecha distinta a mano)."""
    if getattr(target, "estado", None) == "Vendido" and not getattr(target, "fecha_venta", None):
        target.fecha_venta = now_uy()


# --------------------------------------------------------------------------
# Direcciones lindas de los discos (v6.05): arcanorecordstore.com/artista-titulo
# --------------------------------------------------------------------------
# Primeros tramos de las rutas de la app (carrito, sobre, admin...): una ficha nunca
# puede usar esas direcciones. Los completa create_app al registrar las rutas.
SLUGS_RESERVADOS = {"admin", "static", "disco", "cuenta", "carrito", "checkout", "sobre",
                    "legal", "favoritos", "reservar", "api", "mp", "sitemap.xml", "robots.txt"}


class DiscSlugViejo(db.Model):
    """Direcciones viejas de una ficha (si cambiaste el artista o el titulo): siguen
    funcionando y llevan a la direccion nueva, asi no se rompen links ya compartidos
    ni lo que Google ya tiene guardado."""
    __tablename__ = "disc_slugs_viejos"
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(160), nullable=False, index=True)
    disc_id = db.Column(db.Integer, db.ForeignKey("discs.id", ondelete="CASCADE"), nullable=False)
    fecha = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


def slugify(texto, largo=90):
    """'DJ Gamba - Transform Your Consciousness' -> 'dj-gamba-transform-your-consciousness'.
    Minusculas, sin tildes ni signos, guiones entre palabras."""
    import unicodedata
    t = unicodedata.normalize("NFKD", str(texto or ""))
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.replace("&", " and ").lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    if len(t) > largo:
        t = t[:largo].rsplit("-", 1)[0] or t[:largo]
    return t


def _slug_base(artista, titulo):
    s = slugify(f"{artista or ''} {titulo or ''}")
    if s and (s in SLUGS_RESERVADOS or s.isdigit()):
        s += "-vinilo"
    return s


def _slug_libre(connection, disc_id, base, sello):
    """Direccion para este disco. Las copias del MISMO disco (mismo artista, titulo y
    sello) comparten la direccion; un disco distinto que se llama igual suma el sello
    (y si hiciera falta, un numero)."""
    from sqlalchemy import text as _t
    mio = sello_base(sello or "").strip().lower()
    candidatos = [base]
    _sl = slugify(sello_base(sello or ""))
    if _sl:
        candidatos.append(f"{base}-{_sl}")
    candidatos += [f"{base}-{n}" for n in range(2, 60)]
    for c in candidatos:
        filas = connection.execute(_t("SELECT sello FROM discs WHERE slug = :s AND id != :i"),
                                   {"s": c, "i": disc_id or -1}).fetchall()
        if not filas or any(sello_base(f[0] or "").strip().lower() == mio for f in filas):
            return c
    return f"{base}-{disc_id or 'x'}"


def asignar_slugs_faltantes(app):
    """Al arrancar (idempotente): arma la direccion de los discos que no la tienen."""
    from sqlalchemy import text as _t
    with app.app_context():
        with db.engine.begin() as conn:
            filas = conn.execute(_t("SELECT id, artista, titulo, sello FROM discs "
                                    "WHERE slug IS NULL ORDER BY id")).fetchall()
            n = 0
            for did, artista, titulo, sello in filas:
                base = _slug_base(artista, titulo)
                if not base:
                    continue
                conn.execute(_t("UPDATE discs SET slug = :s WHERE id = :i"),
                             {"s": _slug_libre(conn, did, base, sello), "i": did})
                n += 1
            if n:
                print(f"Migracion: {n} disco(s) con su direccion nueva (slug)")


@_sa_event.listens_for(Disc, "before_insert")
@_sa_event.listens_for(Disc, "before_update")
def _disc_slug(mapper, connection, target):
    """Arma (o actualiza) la direccion de la ficha cuando se crea el disco o cambia su
    artista, titulo o sello. La direccion vieja queda guardada y redirige a la nueva."""
    from sqlalchemy import inspect as _insp, text as _t
    st = _insp(target)
    cambio = (target.slug is None
              or st.attrs.artista.history.has_changes()
              or st.attrs.titulo.history.has_changes()
              or st.attrs.sello.history.has_changes())
    if not cambio:
        return
    base = _slug_base(target.artista, target.titulo)
    if not base:
        return
    nuevo = _slug_libre(connection, target.id, base, target.sello)
    viejo = st.attrs.slug.history.deleted[0] if st.attrs.slug.history.deleted else target.slug
    if target.id and viejo and viejo != nuevo:
        connection.execute(_t("INSERT INTO disc_slugs_viejos (slug, disc_id, fecha) "
                              "VALUES (:s, :d, :f)"), {"s": viejo, "d": target.id, "f": datetime.utcnow()})
    target.slug = nuevo


def _migrate_schema(app):
    """Aplicar migraciones idempotentes a la DB existente.
    
    SQLAlchemy create_all() no agrega columnas nuevas a tablas existentes.
    Esta funcion verifica y agrega manualmente lo que falte.
    """
    from sqlalchemy import inspect, text
    
    with app.app_context():
        inspector = inspect(db.engine)

        # Indices en las columnas que mas se filtran (v5.79). Idempotente
        # (IF NOT EXISTS) y no toca datos: solo acelera las consultas cuando crezca
        # el catalogo (estado del disco, fecha de venta, pedido, comprador...).
        _indices = {
            "discs": ["estado", "fecha_venta", "pedido_id", "cliente_id", "venta_grupo"],
            "reservas": ["estado", "cliente_id"],
            "movimientos_caja": ["origen", "fecha"],
        }
        for _tabla, _cols in _indices.items():
            if not inspector.has_table(_tabla):
                continue
            _existentes = {c["name"] for c in inspector.get_columns(_tabla)}
            for _col in _cols:
                if _col in _existentes:
                    with db.engine.begin() as conn:
                        conn.execute(text(
                            f'CREATE INDEX IF NOT EXISTS ix_{_tabla}_{_col} ON "{_tabla}" ("{_col}")'))

        # Discos vendidos con stock > 0 (v5.86): una fila = una copia, vendida no
        # queda stock. Algunos caminos viejos de "vender" no lo bajaban y el pie de
        # Stock los sumaba. Al des-vender, el stock vuelve a 1 solo (register_status_change).
        if inspector.has_table("discs"):
            with db.engine.begin() as conn:
                _r = conn.execute(text("UPDATE discs SET stock = 0 WHERE estado = 'Vendido' AND stock > 0"))
                if _r.rowcount:
                    print(f"Migracion: {_r.rowcount} disco(s) vendido(s) quedaron con stock 0")

        # Pixel de Meta: cambio de ID pedido por Facundo (16/10/2026, v6.17). El ID vive en
        # Ajustes (settings.meta_pixel_id), no en el codigo ni en el hosting. Solo reemplaza
        # el ID viejo EXACTO: es idempotente y no pisa un ID que cargues despues en Ajustes.
        if inspector.has_table("settings"):
            with db.engine.begin() as conn:
                _rpx = conn.execute(
                    text("UPDATE settings SET value = :nuevo WHERE key = 'meta_pixel_id' AND value = :viejo"),
                    {"nuevo": "1938172937362871", "viejo": "1674775404435874"})
                if _rpx.rowcount:
                    print("Migracion: ID del pixel de Meta cambiado a 1938172937362871")

        # Eventos por disco (v6.19): se guardan 400 dias, despues se borran solos.
        if inspector.has_table("eventos_disco"):
            with db.engine.begin() as conn:
                conn.execute(text("DELETE FROM eventos_disco WHERE fecha < datetime('now', '-400 days')"))

        # Duracion estimada de las tareas (v5.70)
        if inspector.has_table("tareas"):
            _tcols = [c["name"] for c in inspector.get_columns("tareas")]
            if "duracion_min" not in _tcols:
                with db.engine.begin() as conn:
                    conn.execute(text("ALTER TABLE tareas ADD COLUMN duracion_min INTEGER"))
                print("Migracion: columna duracion_min agregada a tareas")

        # Puntaje (1-5 estrellas) de las ideas (v5.68)
        if inspector.has_table("ideas_contenido"):
            _icols = [c["name"] for c in inspector.get_columns("ideas_contenido")]
            if "puntaje" not in _icols:
                with db.engine.begin() as conn:
                    conn.execute(text("ALTER TABLE ideas_contenido ADD COLUMN puntaje INTEGER"))
                print("Migracion: columna puntaje agregada a ideas_contenido")

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
            if "fecha_es_utc" not in _rcols:
                with db.engine.begin() as conn:
                    conn.execute(text("ALTER TABLE reservas ADD COLUMN fecha_es_utc BOOLEAN"))
            _radds = {
                "pagado": "BOOLEAN DEFAULT 0",
                "mp_preference_id": "VARCHAR(120)",
                "mp_payment_id": "VARCHAR(120)",
                "mp_status": "VARCHAR(30)",
                "total_cobrado_uyu": "FLOAT",
                "metodo_pago": "VARCHAR(20)",
                "comprobante_file": "VARCHAR(200)",
                "entrega_tipo": "VARCHAR(20)",
                "envio_costo_uyu": "FLOAT DEFAULT 0",
                "envio_direccion": "VARCHAR(400)",
                "envio_agencia": "VARCHAR(200)",
                "entregado": "BOOLEAN DEFAULT 0",
                "canal": "VARCHAR(20) DEFAULT 'web'",
                "grupo": "VARCHAR(40)",
            }
            for _col, _ddl in _radds.items():
                if _col not in _rcols:
                    with db.engine.connect() as conn:
                        conn.execute(text(f"ALTER TABLE reservas ADD COLUMN {_col} {_ddl}"))
                        conn.commit()
                    print(f"Migracion: columna {_col} agregada a reservas")

        # Mystery Box (v5.32): columnas del item de caja curada en reserva_items
        if inspector.has_table("reserva_items"):
            _ricols = [c["name"] for c in inspector.get_columns("reserva_items")]
            for _col, _ddl in {"mbox_cantidad": "INTEGER", "mbox_texto": "TEXT"}.items():
                if _col not in _ricols:
                    with db.engine.connect() as conn:
                        conn.execute(text(f"ALTER TABLE reserva_items ADD COLUMN {_col} {_ddl}"))
                        conn.commit()
                    print(f"Migracion: columna {_col} agregada a reserva_items")

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
            if "oferta_hasta" not in cols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN oferta_hasta DATE"))
                    conn.commit()
                print("Migracion: columna oferta_hasta agregada a discs")
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
            if "oculto_web" not in cols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN oculto_web BOOLEAN NOT NULL DEFAULT 0"))
                    conn.commit()
                print("Migracion: columna oculto_web agregada a discs")
            if "en_fire_sale" not in cols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN en_fire_sale BOOLEAN DEFAULT 0"))
                    conn.commit()
                print("Migracion: columna en_fire_sale agregada a discs")
            if "precio_venta_uyu" not in cols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN precio_venta_uyu FLOAT"))
                    conn.commit()
                print("Migracion: columna precio_venta_uyu agregada a discs")
            # Ventas sin el precio cobrado guardado (v6.04, decision de Facundo 12/10/2026):
            # se fija como definitivo el precio que tienen hoy (oferta si la hay, si no el
            # de lista), que es lo que se cobro. Asi cambiar despues el precio de un disco
            # vendido ya no mueve los ingresos de meses pasados. Idempotente: solo toca los
            # vendidos que todavia no tienen precio de venta (las ventas nuevas lo guardan
            # solas al pasar a Vendido).
            with db.engine.begin() as conn:
                _rpv = conn.execute(text(
                    "UPDATE discs SET precio_venta_uyu = CASE "
                    "WHEN precio_oferta > 0 AND precio_uyu > 0 AND precio_oferta < precio_uyu "
                    "THEN precio_oferta ELSE precio_uyu END "
                    "WHERE estado = 'Vendido' AND precio_venta_uyu IS NULL AND precio_uyu IS NOT NULL"))
                if _rpv.rowcount:
                    print(f"Migracion: {_rpv.rowcount} venta(s) con su precio de venta fijado")
            if "slug" not in cols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN slug VARCHAR(160)"))
                    conn.commit()
                print("Migracion: columna slug agregada a discs")
            with db.engine.begin() as conn:
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_discs_slug ON discs (slug)"))
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
            # NOTA (v4.13): ACA habia una migracion que hacia
            #   UPDATE discs SET estado='Pre-order' WHERE es_preorder=1 AND estado='Disponible'
            # Era un arreglo de datos de UNA vez (cuando el flag es_preorder cambio de
            # significado). Pero corria en CADA arranque y pisaba el estado: si marcabas
            # un disco Pre-order (con el toggle es_preorder prendido) como Disponible, el
            # proximo deploy lo devolvia a Pre-order. Hoy es_preorder solo significa
            # "mostrar en el carrusel Pre-order" (solo aplica si estado==Pre-order), asi
            # que el estado manda. SE ELIMINA: no volver a agregar esta migracion.
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
            if "comision_banco_uyu" not in pcols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE pedidos ADD COLUMN comision_banco_uyu FLOAT"))
                    conn.commit()
                print("Migracion: columna comision_banco_uyu agregada a pedidos")
            if "seguimiento_url" not in pcols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE pedidos ADD COLUMN seguimiento_url VARCHAR(500)"))
                    conn.commit()
                print("Migracion: columna seguimiento_url agregada a pedidos")

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
            if "canal_venta" not in dcols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN canal_venta VARCHAR(20)"))
                    conn.commit()
                print("Migracion: columna canal_venta agregada a discs")
            if "metodo_pago_venta" not in dcols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN metodo_pago_venta VARCHAR(20)"))
                    conn.commit()
                print("Migracion: columna metodo_pago_venta agregada a discs")
                # Backfill UNA SOLA VEZ (recien creada la columna): copiar el metodo de
                # la caja al disco. IMPORTANTE: esto va ADENTRO del if de creacion, NO en
                # cada arranque; si no, revierte un blanqueo manual del metodo hecho en el
                # panel de Ventas (mismo tipo de bug que la migracion de Pre-order de v4.13).
                try:
                    with db.engine.connect() as conn:
                        conn.execute(text(
                            "UPDATE discs SET metodo_pago_venta = ("
                            "  SELECT m.metodo FROM movimientos_caja m"
                            "  WHERE m.origen='venta' AND m.origen_id=discs.id"
                            "    AND m.metodo IS NOT NULL AND m.metodo != '' LIMIT 1) "
                            "WHERE estado='Vendido' AND (metodo_pago_venta IS NULL OR metodo_pago_venta='') "
                            "AND EXISTS (SELECT 1 FROM movimientos_caja m2 "
                            "  WHERE m2.origen='venta' AND m2.origen_id=discs.id"
                            "    AND m2.metodo IS NOT NULL AND m2.metodo != '')"
                        ))
                        conn.commit()
                except Exception:
                    pass
            if "venta_grupo" not in dcols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN venta_grupo VARCHAR(40)"))
                    conn.commit()
                print("Migracion: columna venta_grupo agregada a discs")
            if "cliente_id" not in dcols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN cliente_id INTEGER"))
                    conn.commit()
                print("Migracion: columna cliente_id (comprador) agregada a discs")
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
            # Sena de reserva (v4.64): 4 columnas
            if "sena_cobrada" not in dcols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN sena_cobrada BOOLEAN DEFAULT 0"))
                    conn.commit()
                print("Migracion: columna sena_cobrada agregada a discs")
            if "sena_monto_uyu" not in dcols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN sena_monto_uyu FLOAT"))
                    conn.commit()
                print("Migracion: columna sena_monto_uyu agregada a discs")
            if "sena_fecha" not in dcols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN sena_fecha DATE"))
                    conn.commit()
                print("Migracion: columna sena_fecha agregada a discs")
            if "sena_metodo" not in dcols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN sena_metodo VARCHAR(20)"))
                    conn.commit()
                print("Migracion: columna sena_metodo agregada a discs")
            # Mystery Box (v5.32): flag del producto especial
            if "es_mystery_box" not in dcols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE discs ADD COLUMN es_mystery_box BOOLEAN DEFAULT 0"))
                    conn.commit()
                print("Migracion: columna es_mystery_box agregada a discs")

        # Columna play_count en tracks (contador de escuchas)
        if inspector.has_table("tracks"):
            tcols = [c["name"] for c in inspector.get_columns("tracks")]
            if "play_count" not in tcols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE tracks ADD COLUMN play_count INTEGER DEFAULT 0"))
                    conn.commit()
                print("Migracion: columna play_count agregada a tracks")

        # Seed del marcador de "reservas vistas" (v4.65): que el badge del nav arranque en
        # 0 tras el deploy y solo avise por reservas NUEVAS (id mayor al maximo actual).
        # Solo si nunca se seteo (idempotente).
        if inspector.has_table("settings") and inspector.has_table("reservas"):
            with db.engine.connect() as conn:
                _row = conn.execute(text("SELECT 1 FROM settings WHERE key='reservas_visto_hasta_id'")).fetchone()
                if not _row:
                    _mx = conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM reservas")).scalar() or 0
                    conn.execute(text("INSERT INTO settings (key, value, updated_at) VALUES ('reservas_visto_hasta_id', :v, CURRENT_TIMESTAMP)"), {"v": str(_mx)})
                    conn.commit()
                    print(f"Seed: reservas_visto_hasta_id = {_mx}")

        # Reconstruir la tabla clientes: email y password pasan a OPCIONALES (clientes de
        # Instagram cargados a mano no tienen cuenta) y se suman instagram/notas/origen.
        # SQLite no permite cambiar NOT NULL con ALTER, asi que se rehace la tabla. Se
        # corre UNA sola vez (guardado por 'instagram not in cols'). Preserva ids y datos.
        if inspector.has_table("clientes"):
            _ccols = [c["name"] for c in inspector.get_columns("clientes")]
            if "instagram" not in _ccols:
                raw = db.engine.raw_connection()
                try:
                    cur = raw.cursor()
                    cur.execute("PRAGMA foreign_keys=OFF")
                    cur.executescript(
                        """
                        CREATE TABLE clientes_new (
                            id INTEGER PRIMARY KEY,
                            nombre VARCHAR(200) NOT NULL,
                            email VARCHAR(200),
                            whatsapp VARCHAR(50),
                            instagram VARCHAR(100),
                            notas TEXT,
                            origen VARCHAR(20) DEFAULT 'web',
                            password_hash VARCHAR(255),
                            fecha_registro DATETIME
                        );
                        INSERT INTO clientes_new (id, nombre, email, whatsapp, password_hash, fecha_registro, origen)
                            SELECT id, nombre, email, whatsapp, password_hash, fecha_registro, 'web' FROM clientes;
                        DROP TABLE clientes;
                        ALTER TABLE clientes_new RENAME TO clientes;
                        CREATE INDEX ix_clientes_email ON clientes(email);
                        CREATE INDEX ix_clientes_whatsapp ON clientes(whatsapp);
                        """
                    )
                    cur.execute("PRAGMA foreign_keys=ON")
                    raw.commit()
                    print("Migracion: tabla clientes reconstruida (email/password opcionales + instagram/notas/origen)")
                finally:
                    raw.close()

        # Columnas del programa de lealtad en clientes (ALTER simple, idempotente)
        if inspector.has_table("clientes"):
            _ccols2 = [c["name"] for c in inspector.get_columns("clientes")]
            if "lealtad_base" not in _ccols2:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE clientes ADD COLUMN lealtad_base INTEGER DEFAULT 0"))
                    conn.commit()
                print("Migracion: columna lealtad_base agregada a clientes")
            if "premio1_cobrado" not in _ccols2:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE clientes ADD COLUMN premio1_cobrado BOOLEAN DEFAULT 0"))
                    conn.commit()
                print("Migracion: columna premio1_cobrado agregada a clientes")
            if "acepta_avisos" not in _ccols2:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE clientes ADD COLUMN acepta_avisos BOOLEAN DEFAULT 1"))
                    conn.commit()
                print("Migracion: columna acepta_avisos agregada a clientes")

        # Columna cliente_id en reservas (para cuentas de cliente)
        if inspector.has_table("reservas"):
            rcols = [c["name"] for c in inspector.get_columns("reservas")]
            if "cliente_id" not in rcols:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE reservas ADD COLUMN cliente_id INTEGER"))
                    conn.commit()
                print("Migracion: columna cliente_id agregada a reservas")
            # Rediseño de estados (v4.37): Confirmada -> Pendiente, Completada -> Pagada.
            # Idempotente: despues de la 1ra corrida ya no quedan esos estados.
            with db.engine.connect() as conn:
                r1 = conn.execute(text(
                    "UPDATE reservas SET estado='Pendiente' WHERE estado='Confirmada'"))
                r2 = conn.execute(text(
                    "UPDATE reservas SET estado='Pagada', entregado=1 WHERE estado='Completada'"))
                conn.commit()
                _n = (r1.rowcount or 0) + (r2.rowcount or 0)
                if _n:
                    print(f"Migracion: {_n} reserva(s) migradas a los estados nuevos (Pendiente/Pagada)")

        # Mystery Box (v5.32): crear la fila del producto especial si no existe.
        # Idempotente: solo puede haber una. Facundo despues le cambia la foto/textos
        # desde el dashboard como a cualquier disco.
        if inspector.has_table("discs"):
            try:
                existe = Disc.query.filter_by(es_mystery_box=True).first()
                if existe is None:
                    _max_orden = db.session.query(db.func.max(Disc.orden)).scalar() or 0
                    box = Disc(
                        artista="Arcano",
                        titulo="Mystery Box",
                        estado="Disponible",
                        origen="arcano",
                        stock=1,          # no es inventario: nunca se descuenta
                        precio_uyu=float(min(MYSTERY_BOX_PRECIOS.values())),
                        es_mystery_box=True,
                        orden=_max_orden + 1000,   # arranca primera en la grilla
                        descripcion_publica=(
                            "Una selección sorpresa que armamos nosotros. Elegís cuántos "
                            "discos querés y nos contás qué te gusta; nosotros elegimos los "
                            "vinilos y los descubrís al abrir la caja."
                        ),
                    )
                    db.session.add(box)
                    db.session.commit()
                    print("Seed: Mystery Box creada")
                elif (existe.descripcion_publica or "").startswith("Una box curada"):
                    # La descripcion ahora se muestra arriba del todo en la ficha, asi que
                    # se reescribe mejor. Solo si sigue siendo la sembrada (cualquiera de
                    # las variantes viejas): NO pisa un texto editado desde el admin.
                    existe.descripcion_publica = (
                        "Una selección sorpresa que armamos nosotros. Elegís cuántos "
                        "discos querés y nos contás qué te gusta; nosotros elegimos los "
                        "vinilos y los descubrís al abrir la caja.")
                    if (existe.stock or 0) > 1:
                        existe.stock = 1
                    db.session.commit()
                    print("Migracion: descripcion de la Mystery Box actualizada")
                elif (existe.stock or 0) > 1:
                    # La box no es inventario: un stock alto es una bomba para cualquier
                    # metrica que multiplique por stock (valor de venta, ganancia
                    # potencial). Con 1 alcanza: nada se lo descuenta nunca.
                    existe.stock = 1
                    db.session.commit()
                    print("Migracion: stock de la Mystery Box normalizado a 1")
            except Exception:
                db.session.rollback()

        # Condicion "Nuevo / Sellado" para TODO el catalogo (pedido de Facundo, 24/9/2026).
        # Excepciones: el disco 170 (Jamie McHugh - Dropout, usado de Discogs, queda como
        # lo cargo: "Usado - Mint") y la Mystery Box (no es un disco).
        # Corre UNA sola vez (flag en settings) y antes guarda la condicion anterior de
        # cada disco que cambia (agrupada por condicion) para poder volver atras.
        if inspector.has_table("discs") and inspector.has_table("settings"):
            try:
                _FLAG = "migra_condicion_nuevo_20260924"
                if (Setting.query.filter_by(key=_FLAG).first() is None):
                    import json as _json
                    _NUEVO = "Nuevo / Sellado"
                    _EXCLUIR = {170}
                    antes = {}
                    cambiados = 0
                    for d in Disc.query.order_by(Disc.id).all():
                        if d.id in _EXCLUIR or getattr(d, "es_mystery_box", False):
                            continue
                        if d.condicion != _NUEVO:
                            antes.setdefault(d.condicion or "", []).append(d.id)
                            d.condicion = _NUEVO
                            cambiados += 1
                    db.session.add(Setting(key="backup_condiciones_20260924",
                                           value=_json.dumps(antes, ensure_ascii=False)))
                    db.session.add(Setting(key=_FLAG, value="1"))
                    db.session.commit()
                    print(f"Migracion: {cambiados} discos pasados a condicion '{_NUEVO}'")
            except Exception:
                db.session.rollback()

        # Constancia (v5.54): carga UNA vez las metas que Facundo tenia anotadas.
        # Se pueden editar o borrar desde Tareas > Constancia; no se vuelven a crear.
        if inspector.has_table("settings"):
            try:
                _FLAG = "seed_constancia_v554"
                if Setting.query.filter_by(key=_FLAG).first() is None:
                    # Las tablas nuevas se crean despues (create_all en init_db): crearlas ya.
                    HabitoContenido.__table__.create(db.engine, checkfirst=True)
                    HabitoRegistro.__table__.create(db.engine, checkfirst=True)
                    if HabitoContenido.query.count() == 0:
                        for i, (nom, cant, frec, dsem, cada) in enumerate([
                            ("Videos de discos", 2, "dia", 4, 15),
                            ("Stories de discos", 3, "dia", 7, 15),
                            ("Reel tocando", 1, "semana", 7, 15),
                            ("Contenido diferente", 1, "dias", 7, 15),
                        ]):
                            db.session.add(HabitoContenido(nombre=nom, cantidad=cant, frecuencia=frec,
                                                           dias_semana=dsem, cada_dias=cada, orden=i))
                    db.session.add(Setting(key=_FLAG, value="1"))
                    db.session.commit()
            except Exception:
                db.session.rollback()

        # Reparacion (v5.45): hasta v5.44, crear/poner Pendiente una reserva pasaba sus
        # discos en ENCARGO a "Reservado", y eso los metia en la contabilidad (stock sin
        # vender + por cobrar). Se devuelven a Encargo SOLO los discos que siguen Reservado
        # y cuyo ultimo cambio fue esa reserva viniendo de Encargo. De paso, a los discos
        # YA vendidos de esas reservas sin comprador se les pone el cliente de la reserva
        # (compra vieja cargada como registro). Corre una sola vez (flag en settings).
        if inspector.has_table("discs") and inspector.has_table("status_history"):
            try:
                _FLAG = "repara_encargo_reservado_v545"
                if Setting.query.filter_by(key=_FLAG).first() is None:
                    import re as _re
                    revertidos = []
                    reservas_tocadas = set()
                    for d in Disc.query.filter(Disc.estado == "Reservado").all():
                        hist = (StatusHistory.query.filter_by(disc_id=d.id)
                                # por id (orden real de carga): las fechas mezclan hora UY y UTC
                                .order_by(StatusHistory.id.desc())
                                .limit(2).all())
                        if len(hist) < 2:
                            continue
                        ult, prev = hist[0], hist[1]
                        nota = ult.nota or ""
                        if not (ult.estado == "Reservado" and prev.estado == "Encargo"
                                and nota.startswith("Reserva")):
                            continue
                        m = _re.search(r"#(\d+)", nota)
                        res = Reserva.query.get(int(m.group(1))) if m else None
                        d.estado = "Encargo"
                        if res and res.cliente_id and not d.cliente_id:
                            d.cliente_id = res.cliente_id
                        db.session.add(StatusHistory(
                            disc_id=d.id, estado="Encargo",
                            nota="Vuelve a Encargo (la reserva no lo cambia hasta que se pague)"))
                        revertidos.append(d.id)
                        if res:
                            reservas_tocadas.add(res)
                    for res in reservas_tocadas:
                        if not res.cliente_id:
                            continue
                        for it in res.items_efectivos:
                            dd = getattr(it, "disco", None)
                            if dd and dd.estado == "Vendido" and not dd.cliente_id:
                                dd.cliente_id = res.cliente_id
                    db.session.add(Setting(key=_FLAG, value=",".join(map(str, revertidos)) or "-"))
                    db.session.commit()
                    if revertidos:
                        print(f"Reparacion: {len(revertidos)} discos vuelven a Encargo: {revertidos}")
            except Exception:
                db.session.rollback()


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


_COMO_FUNCIONA_VIEJO = ("Reserva online tu disco. Coordinamos pago y entrega por mensaje. "
                        "Hacemos envios a todo Uruguay.")
_COMO_FUNCIONA_NUEVO = ("Elegís tus discos y pagás online con Mercado Pago o por transferencia, "
                        "o los reservás y coordinamos el pago por mensaje.\n\n"
                        "Retiro en Pocitos sin costo, envío en Montevideo ($50, gratis desde 3 "
                        "discos) o al interior por agencia.")
_ENVIOS_VIEJO = "Coordinamos envio o retiro por mensaje directo. Hacemos envios a todo Uruguay."
_ENVIOS_NUEVO = ("Retiro en Pocitos sin costo. Envío en Montevideo $50 (gratis desde 3 discos). "
                 "Al interior por agencia, lo pagás al recibir.")


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
            "public_tagline": "Vinilos de electronica con curaduria propia. Montevideo.",
            "public_about": (
                "Arcano es una tienda de discos especializada en musica electronica: "
                "house, techno, minimal y progressive. Cada disco que llega a Arcano "
                "fue elegido cuidadosamente. Operamos desde Montevideo, Uruguay."
            ),
            "public_about_curaduria": "",
            "public_about_como_funciona": _COMO_FUNCIONA_NUEVO,
            "public_envios": _ENVIOS_NUEVO,
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
        # Textos por defecto viejos (de cuando todo se coordinaba por mensaje): si siguen
        # EXACTAMENTE como venian de fabrica, se actualizan a como funciona hoy (MP,
        # transferencia, envio $50). Si los editaste en Ajustes, no se tocan.
        for k, viejo, nuevo in (
                ("public_about_como_funciona", _COMO_FUNCIONA_VIEJO, _COMO_FUNCIONA_NUEVO),
                ("public_envios", _ENVIOS_VIEJO, _ENVIOS_NUEVO)):
            if (Setting.get(k) or "").strip() == viejo:
                Setting.set(k, nuevo)

    # Migrar tracklists en texto a la tabla Track (fuera del with anterior porque necesita commit propio)
    _migrate_tracklists_to_tracks(app)
    # Migrar reservas viejas (un disco) al modelo multi-disco (ReservaItem)
    _migrate_reservas_a_items(app)
