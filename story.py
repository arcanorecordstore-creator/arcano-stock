"""Story de Instagram de un disco, en un clic (admin -> ficha del disco).

Genera con Pillow un PNG 1080x1920 listo para subir a historias, siguiendo el
sistema de marca de los reels de catalogo (Drop Hunter) y el manual de marca:

  - Fondo NEGRO #0A0A0A con banda superior (etiqueta de la variante) y banda
    inferior (logo + arcanorecordstore.com), separadas por filetes finos.
  - Portada grande y centrada, cuadrada, con un borde fino y sutil.
  - Abajo de la portada: artista (Medium, mayusculas), titulo (Regular), sello y
    precio en pesos. Sin dos puntos, sin slogans: solo datos del disco.
  - Solo Space Grotesk Regular y Medium (nunca Bold). Paleta del manual:
    crema #FFF7EC (texto), beige #E9D1AF (etiqueta), blanco (logo).
  - Textos largos: primero se achica la letra y se parte en lineas; si aun asi
    no entra, se achica la portada. Nunca se corta un texto.

Las historias tienen la interfaz de Instagram arriba (~220 px) y abajo
(~200 px): todo lo importante queda dentro de esa zona segura.
"""
import io
import os

from PIL import Image, ImageDraw, ImageFont, ImageOps

_BASE = os.path.dirname(os.path.abspath(__file__))
_FONTS = os.path.join(_BASE, "static", "fonts")
_BRAND = os.path.join(_BASE, "static", "brand")

# ---------------------------------------------------------------------------
# Lienzo y paleta
# ---------------------------------------------------------------------------
W, H = 1080, 1920
NEGRO = (10, 10, 10)            # #0A0A0A fondo de catalogo / ventas
NEGRO_MARCA = (30, 28, 28)      # #1E1C1C (relleno de portada faltante)
CREMA = (255, 247, 236)         # #FFF7EC texto principal
BEIGE = (233, 209, 175)         # #E9D1AF etiqueta de la variante


def _a(rgb, alpha):
    """Color con transparencia (0-255) para dibujar en modo RGBA."""
    return (*rgb, alpha)


TEXTO_2 = _a(CREMA, 150)        # secundario: sello, web, precio tachado
FILETE = _a(CREMA, 60)          # lineas de las bandas
BORDE_TAPA = _a(CREMA, 46)      # borde sutil alrededor de la portada

# Zona segura de la historia y bandas
TAPA_MAX = 864                  # portada estandar (columna de todo el diseno)
TAPA_MIN = 560
Y_ETIQUETA = 280                # centro vertical de la banda superior
Y_FILETE_SUP = 340
Y_FILETE_INF = 1604
Y_LOGO = 1644                   # borde superior del logo
ALTO_LOGO = 78
AIRE = 44                       # aire entre filetes y contenido
GAP_TAPA_TEXTO = 44

# ---------------------------------------------------------------------------
# Variantes
# ---------------------------------------------------------------------------
VARIANTES = ("nuevo", "queda1", "vendido", "sale", "preorder")
NOMBRES = {
    "nuevo": "Nuevo ingreso",
    "queda1": "Queda 1",
    "vendido": "Vendido",
    "sale": "SALE",
    "preorder": "Pre-order",
}


def variantes_disponibles(disc):
    """[(clave, nombre del boton)] que tienen sentido para este disco.
    SALE solo con oferta vigente; Pre-order solo si el estado es Pre-order."""
    out = [("nuevo", "Nuevo ingreso"), ("queda1", "Queda 1"), ("vendido", "Vendido")]
    if disc.en_oferta and disc.estado != "Vendido":
        out.append(("sale", f"SALE -{disc.descuento_pct}%"))
    if disc.estado == "Pre-order":
        out.append(("preorder", "Pre-order"))
    return out


def variante_por_defecto(disc):
    """La que mas probablemente quiera Facundo segun el estado del disco."""
    claves = [k for k, _ in variantes_disponibles(disc)]
    if disc.estado == "Vendido":
        return "vendido"
    if "preorder" in claves:
        return "preorder"
    if "sale" in claves:
        return "sale"
    return "nuevo"


def _etiqueta(disc, variante):
    if variante == "sale":
        return f"SALE -{disc.descuento_pct}%"
    return NOMBRES[variante].upper()


def nombre_archivo(disc):
    """story-artista-titulo.png (sin tildes ni signos)."""
    from models import slugify
    s = slugify(f"{disc.artista or ''} {disc.titulo or ''}", largo=80) or f"disco-{disc.id}"
    return f"story-{s}.png"


def precio_pesos(valor):
    """1300 -> '$ 1.300'."""
    return "$ " + f"{int(round(valor or 0)):,}".replace(",", ".")


# ---------------------------------------------------------------------------
# Tipografia
# ---------------------------------------------------------------------------
_CACHE_FUENTES = {}


def _fuente(peso, tam):
    """Space Grotesk 'Regular' o 'Medium' (nunca Bold, por manual)."""
    clave = (peso, int(tam))
    f = _CACHE_FUENTES.get(clave)
    if f is None:
        f = ImageFont.truetype(os.path.join(_FONTS, f"SpaceGrotesk-{peso}.ttf"), int(tam))
        _CACHE_FUENTES[clave] = f
    return f


_CACHE_ANCHOS = {}


def _largo(texto, fuente):
    """fuente.getlength con cache (medir texto es lo mas lento de todo)."""
    clave = (id(fuente), texto)
    v = _CACHE_ANCHOS.get(clave)
    if v is None:
        if len(_CACHE_ANCHOS) > 50000:
            _CACHE_ANCHOS.clear()
        v = _CACHE_ANCHOS[clave] = fuente.getlength(texto)
    return v


def _ancho(texto, fuente, tracking=0.0):
    """Ancho del texto con espaciado entre letras (tracking en px)."""
    if not texto:
        return 0
    return _largo(texto, fuente) + tracking * (len(texto) - 1)


def _dibujar(draw, x, y_base, texto, fuente, color, tracking=0.0):
    """Dibuja sobre la linea de base y_base. Con tracking se dibuja letra por
    letra pero respetando el kerning original (posicion = largo del prefijo)."""
    if not tracking:
        draw.text((x, y_base), texto, font=fuente, fill=color, anchor="ls")
        return
    for i, ch in enumerate(texto):
        if ch == " ":
            continue
        xi = x + _largo(texto[:i], fuente) + tracking * i
        draw.text((xi, y_base), ch, font=fuente, fill=color, anchor="ls")


_PEGADOS = {"/", "&", "-", "–", "—", "·", "+", ","}


def _unidades(texto):
    """Divide el texto en unidades que conviene no separar: cada artista de una
    lista ('A / B', 'A, B') va entero si entra. Los signos sueltos ('/', '&')
    quedan pegados a la palabra anterior: ningun renglon arranca con '/'."""
    palabras = []
    for p in (texto or "").replace(" ,", ",").split():
        if palabras and p in _PEGADOS:
            palabras[-1] += " " + p
        else:
            palabras.append(p)
    unidades, actual = [], []
    for p in palabras:
        actual.append(p)
        if p.endswith(("/", ",")):
            unidades.append(actual)
            actual = []
    if actual:
        unidades.append(actual)
    return unidades


def _partir_codicioso(unidades, fuente, ancho_max, tracking):
    lineas, actual = [], ""

    def cabe(t):
        return _ancho(t, fuente, tracking) <= ancho_max

    for unidad in unidades:
        entera = " ".join(unidad)
        prueba = f"{actual} {entera}".strip()
        if cabe(prueba):
            actual = prueba
            continue
        if actual and cabe(entera):
            lineas.append(actual)
            actual = entera
            continue
        # La unidad no entra entera: se reparte por palabras
        for p in unidad:
            prueba = f"{actual} {p}".strip()
            if cabe(prueba):
                actual = prueba
                continue
            if actual:
                lineas.append(actual)
                actual = ""
            if cabe(p):
                actual = p
                continue
            trozo = ""          # palabra larguisima: se corta por letras
            for ch in p:
                if cabe(trozo + ch):
                    trozo += ch
                else:
                    lineas.append(trozo)
                    trozo = ch
            actual = trozo
    if actual:
        lineas.append(actual)
    return lineas


def _partir(texto, fuente, ancho_max, tracking=0.0, equilibrar=True):
    """Parte el texto en lineas sin perder nada, y las equilibra: busca el ancho
    mas angosto que mantiene la misma cantidad de renglones, para que no quede
    una palabra sola abajo ('Dreams &' / 'Nightmares')."""
    unidades = _unidades(texto)
    lineas = _partir_codicioso(unidades, fuente, ancho_max, tracking)
    if len(lineas) < 2 or not equilibrar:
        return lineas

    def cortes(ls):
        # renglones que terminan a mitad de un nombre (no en '/' ni ',')
        return sum(1 for ln in ls[:-1] if not ln.endswith(("/", ",")))

    base = cortes(lineas)
    lo, hi, mejor = int(ancho_max * 0.5), int(ancho_max), lineas
    while hi - lo > 4:
        medio = (lo + hi) // 2
        prueba = _partir_codicioso(unidades, fuente, medio, tracking)
        if len(prueba) <= len(lineas) and cortes(prueba) <= base:
            mejor, hi = prueba, medio
        else:
            lo = medio
    return mejor


class _Bloque:
    """Un texto ya resuelto: lineas, fuente y metrica para medir y dibujar."""

    def __init__(self, texto, peso, tam_max, tam_min, max_lineas, ancho,
                 color, interlineado, tracking_em=0.0):
        self.color = color
        self.lineas, self.fuente, self.tracking = [], None, 0.0
        tam = tam_max
        while True:
            f = _fuente(peso, tam)
            tr = tam * tracking_em
            n = len(_partir(texto, f, ancho, tr, equilibrar=False))
            if n <= max_lineas or tam <= tam_min:
                break
            tam -= 2
        lineas = _partir(texto, f, ancho, tr)       # equilibrado, una sola vez
        self.lineas, self.fuente, self.tracking = lineas, f, tr
        self.paso = round(tam * interlineado)          # distancia entre bases
        asc, desc = f.getmetrics()
        self.asc = round(tam * 0.74)                   # alto de mayusculas aprox.
        self.alto = self.asc + self.paso * (len(lineas) - 1) + round(desc * 0.35)

    def dibujar(self, draw, x, y_top):
        y = y_top + self.asc
        for ln in self.lineas:
            _dibujar(draw, x, y, ln, self.fuente, self.color, self.tracking)
            y += self.paso
        return y_top + self.alto


# ---------------------------------------------------------------------------
# Portada
# ---------------------------------------------------------------------------
def _abrir_portada(disc, covers_dir, static_dir):
    """Devuelve la portada como imagen RGB, o None si no hay / no se pudo leer.
    Orden: archivo local (COVERS_DIR, igual que serve_cover) -> URL externa
    (con timeout corto) -> None (placeholder)."""
    from werkzeug.utils import safe_join

    candidatos = []
    if disc.cover_local:
        p = safe_join(covers_dir, disc.cover_local)
        if p:
            candidatos.append(p)
    url = (disc.cover_url or "").strip()
    # Portadas guardadas como ruta propia del sitio
    if url.startswith("/covers/"):
        p = safe_join(covers_dir, url[len("/covers/"):])
        if p:
            candidatos.append(p)
    elif url.startswith("/static/"):
        p = safe_join(static_dir, url[len("/static/"):])
        if p:
            candidatos.append(p)

    for p in candidatos:
        try:
            if os.path.isfile(p):
                with Image.open(p) as im:
                    return _a_rgb(im)
        except Exception:
            pass

    if url.startswith(("http://", "https://")):
        try:
            import requests
            r = requests.get(url, timeout=(3, 5), stream=True,
                             headers={"User-Agent": "Mozilla/5.0 (Arcano story)"})
            r.raise_for_status()
            datos = io.BytesIO()
            for trozo in r.iter_content(64 * 1024):
                datos.write(trozo)
                if datos.tell() > 12 * 1024 * 1024:   # tope de seguridad
                    raise ValueError("portada demasiado grande")
            datos.seek(0)
            with Image.open(datos) as im:
                return _a_rgb(im)
        except Exception:
            return None
    return None


def _a_rgb(im):
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        fondo = Image.new("RGB", im.size, NEGRO_MARCA)
        fondo.paste(im, mask=im.getchannel("A"))
        return fondo
    return im.convert("RGB")


def _cuadrada(img, lado):
    """Portada cuadrada de lado x lado. Si es casi cuadrada se recorta al
    centro; si es muy rectangular entra completa sobre negro de marca."""
    w, h = img.size
    if w and h and 0.9 <= w / h <= 1.1:
        return ImageOps.fit(img, (lado, lado), Image.LANCZOS, centering=(0.5, 0.5))
    img = img.copy()
    img.thumbnail((lado, lado), Image.LANCZOS)
    lienzo = Image.new("RGB", (lado, lado), NEGRO_MARCA)
    lienzo.paste(img, ((lado - img.width) // 2, (lado - img.height) // 2))
    return lienzo


def _placeholder(lado):
    """Sin portada: cuadrado negro de marca con el vinilo beige al centro."""
    lienzo = Image.new("RGB", (lado, lado), NEGRO_MARCA)
    try:
        with Image.open(os.path.join(_BRAND, "logo-vinilo-beige.png")) as logo:
            logo = logo.convert("RGBA")
            alto = int(lado * 0.36)
            ancho = round(logo.width * alto / logo.height)
            logo = logo.resize((ancho, alto), Image.LANCZOS)
            lienzo.paste(logo, ((lado - ancho) // 2, (lado - alto) // 2), logo)
    except Exception:
        pass
    return lienzo


def _logo(alto):
    with Image.open(os.path.join(_BRAND, "logo-primary-blanco.png")) as logo:
        logo = logo.convert("RGBA")
        ancho = round(logo.width * alto / logo.height)
        return logo.resize((ancho, alto), Image.LANCZOS)


# ---------------------------------------------------------------------------
# Composicion
# ---------------------------------------------------------------------------
def _bloques_texto(disc, variante, ancho, escala=1.0):
    """Arma los bloques de texto de abajo de la portada para un ancho dado.
    Devuelve (lista de (bloque, aire_antes), fila_precio | None)."""
    artista = (disc.artista or "").strip().upper() or "SIN ARTISTA"
    titulo = (disc.titulo or "").strip()
    sello = " · ".join(p for p in [(disc.sello or "").strip(),
                                   str(disc.anio or "").strip()] if p)

    def t(n):
        return max(20, round(n * escala))

    bloques = [(_Bloque(artista, "Medium", t(60), t(40), 2, ancho, CREMA, 1.14, 0.03), 0)]
    if titulo:
        bloques.append((_Bloque(titulo, "Regular", t(46), t(34), 3, ancho, CREMA, 1.2), t(20)))
    if sello:
        bloques.append((_Bloque(sello, "Regular", t(30), t(24), 2, ancho, TEXTO_2, 1.25, 0.02),
                        t(18)))

    precio = None
    if variante != "vendido":
        actual = disc.precio_mostrar or 0
        if actual > 0:
            lista = disc.precio_uyu if (disc.en_oferta and disc.precio_uyu) else None
            precio = _fila_precio(actual, lista, ancho, t(60))
            precio["aire"] = t(36)
    return bloques, precio


def _fila_precio(actual, lista, ancho, tam):
    """Precio a cobrar (Medium) y, si hay oferta, el de lista tachado al lado."""
    while True:
        f = _fuente("Medium", tam)
        f2 = _fuente("Regular", round(tam * 0.62))
        t1 = precio_pesos(actual)
        t2 = precio_pesos(lista) if lista else ""
        total = f.getlength(t1) + ((28 + f2.getlength(t2)) if t2 else 0)
        if total <= ancho or tam <= 36:
            break
        tam -= 2
    return {"t1": t1, "t2": t2, "f1": f, "f2": f2, "asc": round(tam * 0.74),
            "alto": round(tam * 0.74) + 4}


def _alto_texto(bloques, precio):
    alto = sum(b.alto + aire for b, aire in bloques)
    if precio:
        alto += precio["aire"] + precio["alto"]
    return alto


def generar_story(disc, variante, covers_dir, static_dir):
    """PNG 1080x1920 (bytes) de la story del disco en la variante pedida."""
    if variante not in VARIANTES:
        raise ValueError("variante invalida")

    lienzo = Image.new("RGB", (W, H), NEGRO)
    draw = ImageDraw.Draw(lienzo, "RGBA")

    # --- Tamano de la portada: la mas grande que deje entrar todo el texto.
    #     Antes de achicar la tapa se prueba la letra un poco mas chica. ---
    zona = (Y_FILETE_INF - AIRE) - (Y_FILETE_SUP + AIRE)

    def armar(lado, escala):
        bloques, precio = _bloques_texto(disc, variante, lado, escala)
        alto = _alto_texto(bloques, precio)
        return lado + GAP_TAPA_TEXTO + alto <= zona, bloques, precio, alto

    lado = TAPA_MAX
    for escala in (1.0, 0.93, 0.86):
        entra, bloques, precio, alto_txt = armar(lado, escala)
        if entra:
            break
    else:
        # Ni con la letra mas chica: la tapa mas grande que deje entrar el texto
        # (busqueda binaria; con texto enorme queda la tapa minima igual).
        lo, hi = TAPA_MIN, TAPA_MAX
        mejor = armar(TAPA_MIN, 0.86) + (TAPA_MIN,)
        while hi - lo > 8:
            medio = (lo + hi) // 2
            prueba = armar(medio, 0.86)
            if prueba[0]:
                lo, mejor = medio, prueba + (medio,)
            else:
                hi = medio
        _, bloques, precio, alto_txt, lado = mejor
    x0 = (W - lado) // 2                       # columna comun de todo el diseno
    x1 = x0 + lado

    # --- Banda superior: etiqueta de la variante + genero ---
    f_et = _fuente("Medium", 38)
    tr_et = 38 * 0.2
    etiqueta = _etiqueta(disc, variante)
    base_et = Y_ETIQUETA + round(38 * 0.37)
    _dibujar(draw, x0, base_et, etiqueta, f_et, BEIGE, tr_et)
    genero = (disc.genero or "").split(",")[0].split("/")[0].strip().upper()
    if genero:
        libre = x1 - (x0 + _ancho(etiqueta, f_et, tr_et)) - 40
        tam = 26
        while tam > 18 and _ancho(genero, _fuente("Regular", tam), tam * 0.16) > libre:
            tam -= 1
        f_g = _fuente("Regular", tam)
        w_g = _ancho(genero, f_g, tam * 0.16)
        if w_g <= libre:
            _dibujar(draw, x1 - w_g, base_et, genero, f_g, TEXTO_2, tam * 0.16)
    draw.line([(x0, Y_FILETE_SUP), (x1, Y_FILETE_SUP)], fill=FILETE, width=1)

    # --- Portada: siempre a la misma altura (en una tanda de stories la tapa
    #     no "salta"); si sobra lugar queda abajo del texto ---
    y_tapa = Y_FILETE_SUP + AIRE
    portada = _abrir_portada(disc, covers_dir, static_dir)
    tapa = _cuadrada(portada, lado) if portada is not None else _placeholder(lado)
    lienzo.paste(tapa, (x0, y_tapa))
    draw.rectangle([x0 - 1, y_tapa - 1, x1, y_tapa + lado], outline=BORDE_TAPA, width=1)

    # --- Textos ---
    y = y_tapa + lado + GAP_TAPA_TEXTO
    for b, aire in bloques:
        y = b.dibujar(draw, x0, y + aire)
    if precio:
        y += precio["aire"]
        base = y + precio["asc"]
        draw.text((x0, base), precio["t1"], font=precio["f1"], fill=CREMA, anchor="ls")
        if precio["t2"]:
            x2 = x0 + precio["f1"].getlength(precio["t1"]) + 28
            draw.text((x2, base), precio["t2"], font=precio["f2"], fill=TEXTO_2, anchor="ls")
            # tachado a media altura de las cifras
            tope_cifra = precio["f2"].getbbox("0", anchor="ls")[1]   # negativo
            y_t = round(base + tope_cifra * 0.5)
            draw.line([(x2 - 2, y_t), (x2 + precio["f2"].getlength(precio["t2"]) + 2, y_t)],
                      fill=TEXTO_2, width=3)

    # --- Banda inferior: logo + web ---
    draw.line([(x0, Y_FILETE_INF), (x1, Y_FILETE_INF)], fill=FILETE, width=1)
    logo = _logo(ALTO_LOGO)
    lienzo.paste(logo, (x0, Y_LOGO), logo)
    f_web = _fuente("Regular", 28)
    tr_web = 28 * 0.06
    web = "arcanorecordstore.com"
    base_web = Y_LOGO + ALTO_LOGO // 2 + round(28 * 0.37)
    _dibujar(draw, x1 - _ancho(web, f_web, tr_web), base_web, web, f_web, TEXTO_2, tr_web)

    out = io.BytesIO()
    lienzo.save(out, "PNG", compress_level=6)
    return out.getvalue()
