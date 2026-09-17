"""Identidad de marca para los PDF (manual de marca, set. 2026).

Una sola fuente de verdad de paleta, tipografia y logo para TODOS los informes
exportables (contabilidad, ejecutivo, pedidos), para que salgan iguales y
acordes al manual.

Manual:
  - Paleta: CREMA #FFF7EC · BEIGE #E9D1AF · BLANCO #FFFFFF · MARRON #3D3033 ·
    NEGRO #1E1C1C. Crema y negro dominan; beige y marron acompanan.
  - NO hay color de acento saturado: la marca es sobria a proposito.
  - Tipografia: Space Grotesk. Bold solo el logo; Medium para titulos en
    mayuscula con tracking; Light/Regular para cuerpo.
  - Fondo claro -> logo oscuro. Fondo oscuro -> logo blanco.

Unica excepcion al "sin acento": POSITIVO/NEGATIVO para las cifras. En un
informe contable el signo del numero tiene que leerse de un vistazo, asi que se
conservan pero en tonos apagados y terrosos que conviven con la paleta.
"""
import os

from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

_BASE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Paleta de marca
# ---------------------------------------------------------------------------
CREMA  = colors.HexColor("#FFF7EC")
BEIGE  = colors.HexColor("#E9D1AF")
BLANCO = colors.white
MARRON = colors.HexColor("#3D3033")
NEGRO  = colors.HexColor("#1E1C1C")

# Derivados (mismos tonos, distinta intensidad) para jerarquia de texto y lineas
TEXTO_2  = colors.HexColor("#6B5F60")   # marron aguado: texto secundario
TEXTO_3  = colors.HexColor("#9A8F8C")   # marron muy aguado: notas al pie
LINEA    = colors.HexColor("#E6DCCB")   # beige aguado: filetes y grillas
FILA_ALT = colors.HexColor("#FBF6EE")   # crema claro: filas alternas

# Escala tonal para SERIES de graficos y categorias. El manual no tiene acentos
# saturados, asi que las categorias se distinguen por intensidad dentro de la
# misma familia (negro -> marron -> marron medio -> beige), no por matiz.
MARRON_MEDIO = colors.HexColor("#7A6652")
SERIES = [NEGRO, MARRON, MARRON_MEDIO, BEIGE]

# Funcionales para cifras (apagados, terrosos: conviven con la paleta calida)
POSITIVO = colors.HexColor("#2F6B4F")
NEGATIVO = colors.HexColor("#8C3B33")

# ---------------------------------------------------------------------------
# Tipografia de marca
# ---------------------------------------------------------------------------
FONT, FONT_M, FONT_B = "Helvetica", "Helvetica", "Helvetica-Bold"


def registrar_fuentes():
    """Registra Space Grotesk. Si faltan los .ttf, deja Helvetica sin romper."""
    global FONT, FONT_M, FONT_B
    fdir = os.path.join(_BASE, "static", "fonts")
    pares = [("SpaceGrotesk", "SpaceGrotesk-Regular.ttf"),
             ("SpaceGrotesk-Med", "SpaceGrotesk-Medium.ttf"),
             ("SpaceGrotesk-Bold", "SpaceGrotesk-Bold.ttf")]
    try:
        for nombre, arch in pares:
            ruta = os.path.join(fdir, arch)
            if not os.path.exists(ruta):
                return FONT, FONT_M, FONT_B
            if nombre not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(nombre, ruta))
        pdfmetrics.registerFontFamily(
            "SpaceGrotesk", normal="SpaceGrotesk", bold="SpaceGrotesk-Bold",
            italic="SpaceGrotesk", boldItalic="SpaceGrotesk-Bold")
        FONT, FONT_M, FONT_B = "SpaceGrotesk", "SpaceGrotesk-Med", "SpaceGrotesk-Bold"
    except Exception:
        FONT, FONT_M, FONT_B = "Helvetica", "Helvetica", "Helvetica-Bold"
    return FONT, FONT_M, FONT_B


registrar_fuentes()


# ---------------------------------------------------------------------------
# Logo
# ---------------------------------------------------------------------------
def logo_path(sobre_oscuro=False):
    """Ruta del logo primario segun el fondo (regla del manual). None si falta."""
    arch = "logo-primary-blanco.png" if sobre_oscuro else "logo-primary-negro.png"
    ruta = os.path.join(_BASE, "static", "brand", arch)
    return ruta if os.path.exists(ruta) else None


LOGO_RATIO = 616.0 / 240.0   # proporcion real del PNG del logo primario


def logo_size(ancho):
    """(ancho, alto) respetando la proporcion del logo, sin deformarlo."""
    return ancho, ancho / LOGO_RATIO
