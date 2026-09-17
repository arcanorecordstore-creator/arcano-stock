"""Informe EJECUTIVO de gestion de Arcano (PDF).

Un solo PDF, con prolijidad de presentacion ejecutiva, que junta TODA la parte de
Gestion del admin: Resumen ejecutivo + Ventas + Contabilidad + Caja/Banco + Pedidos +
Radar de demanda. Cada seccion trae numeros grandes (tarjetas KPI), tablas, graficos
y una descripcion en palabras de la situacion.

Se arma con reportlab (misma libreria que informe.py / informe_excel.py). El modulo es
"puro presentacion": lee los datos de metrics + los modelos; lo unico que recibe de
afuera es el resumen de la caja (que se calcula dentro de la app, en _resumen_caja).

Entrada principal: generar_pdf_ejecutivo(periodo, desde, hasta, resumen_caja) -> BytesIO
"""

import io
import os
from datetime import datetime, timedelta

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak, KeepTogether,
)
from reportlab.graphics.shapes import Drawing
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

import metrics
from models import Disc, Pedido, Reserva, Setting

# ---------------------------------------------------------------------------
# Identidad de marca (manual set. 2026): tipografia, paleta y logo salen de
# marca_pdf, unica fuente de verdad compartida con los otros PDF.
# ---------------------------------------------------------------------------
import marca_pdf as MARCA

FONT, FONT_M, FONT_B = MARCA.FONT, MARCA.FONT_M, MARCA.FONT_B

# Se mantienen los nombres historicos (no cambia una linea de layout) pero
# apuntan a los colores del manual.
INK    = MARCA.NEGRO           # negro de marca
INK2   = MARCA.MARRON          # marron de marca
GREEN  = MARCA.POSITIVO        # positivo
RED    = MARCA.NEGATIVO        # negativo
GOLD   = MARCA.MARRON_MEDIO    # destacado / staff (paso tonal, sin acento saturado)
BLUE   = MARCA.MARRON          # coleccion personal / secundario
GREY   = MARCA.TEXTO_2
GREY2  = MARCA.TEXTO_3
LINE   = MARCA.LINEA
PANEL  = MARCA.CREMA           # crema de marca
PANEL2 = MARCA.BEIGE
ROWALT = MARCA.FILA_ALT
PAPER  = MARCA.BLANCO
BEIGE  = MARCA.BEIGE           # solo sobre fondo oscuro (nunca beige sobre blanco)

PAGE_W, PAGE_H = A4
MARGIN = 16 * mm
CONTENT_W = PAGE_W - 2 * MARGIN   # ~178mm

_styles = getSampleStyleSheet()
# autoLeading='max': cada renglon toma el interlineado del texto MAS GRANDE que tiene
# (asi el numero grande de las tarjetas KPI no queda pegado a la etiqueta y al subtexto).
H1    = ParagraphStyle("eH1", parent=_styles["Title"], fontName=FONT_B, fontSize=26, leading=31, textColor=INK, spaceAfter=0)
LEAD  = ParagraphStyle("eLead", parent=_styles["Normal"], fontName=FONT, fontSize=11, leading=18, textColor=INK2)
BODY  = ParagraphStyle("eBody", parent=_styles["Normal"], fontName=FONT, fontSize=9.5, leading=16, textColor=INK)
SMALL = ParagraphStyle("eSmall", parent=_styles["Normal"], fontName=FONT, fontSize=8, leading=13, textColor=GREY)
CARDL = ParagraphStyle("eCardL", parent=_styles["Normal"], fontName=FONT, fontSize=9, leading=14, autoLeading="max")
NUMST = ParagraphStyle("eNum", parent=CARDL, alignment=1, autoLeading="off")  # 1 = centrado


# ---------------------------------------------------------------------------
# Formato de numeros
# ---------------------------------------------------------------------------
def _m(n):
    """$ 12.345 (miles con punto, es-UY). Sin decimales."""
    try:
        return "$ " + "{:,.0f}".format(round(n or 0)).replace(",", ".")
    except (TypeError, ValueError):
        return "$ 0"


def _n(n):
    try:
        return "{:,.0f}".format(round(n or 0)).replace(",", ".")
    except (TypeError, ValueError):
        return "0"


def _kfmt(v):
    """Etiqueta corta para ejes: 12.500 -> 12k."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ""
    if abs(v) >= 1000:
        return "%gk" % round(v / 1000, 1)
    return "%g" % round(v)


def _pct(n):
    try:
        return "%.0f%%" % (n or 0)
    except (TypeError, ValueError):
        return "0%"


def _hx(col):
    """Color reportlab -> '#rrggbb' para usar en <font color=...> de un Paragraph."""
    try:
        return "#" + col.hexval()[2:]
    except Exception:
        return str(col)


def _delta_txt(pct):
    """'+12%' verde / '-8%' rojo (como HTML de color) o '' si no hay dato."""
    if pct is None:
        return None, GREY
    if pct > 0:
        return "+%d%% vs periodo anterior" % pct, GREEN
    if pct < 0:
        return "%d%% vs periodo anterior" % pct, RED
    return "igual que el periodo anterior", GREY


# ---------------------------------------------------------------------------
# Bloques reutilizables (tarjetas, secciones, tablas, graficos)
# ---------------------------------------------------------------------------
def kpi_row(cards, total_w=CONTENT_W):
    """Fila de tarjetas KPI. cards: [{label, value, color, sub, subcolor}]."""
    n = len(cards) or 1
    w = total_w / n
    cells = []
    for c in cards:
        html = ('<font size=7 color="%s">%s</font><br/>'
                '<font size=16 color="%s"><b>%s</b></font>'
                % (_hx(GREY), c["label"].upper(), _hx(INK), c["value"]))
        sub = c.get("sub")
        if sub:
            col = c.get("subcolor", GREY)
            html += '<br/><font size=7.5 color="%s">%s</font>' % (_hx(col), sub)
        cells.append(Paragraph(html, CARDL))
    t = Table([cells], colWidths=[w] * n)
    st = [
        ("BACKGROUND", (0, 0), (-1, -1), PANEL),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 11),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("INNERGRID", (0, 0), (-1, -1), 3, PAPER),   # separacion blanca entre tarjetas
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]
    for i, c in enumerate(cards):
        st.append(("LINEABOVE", (i, 0), (i, 0), 2.5, c.get("color", INK)))
    t.setStyle(TableStyle(st))
    return t


def section(num, title, subtitle=None):
    """Encabezado de seccion: numero en cuadro negro + titulo + regla."""
    numbox = Table([[Paragraph('<font color="white" size=11><b>%s</b></font>' % num, NUMST)]],
                   colWidths=[8.5 * mm], rowHeights=[8.5 * mm])
    numbox.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), INK), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0), ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    title_p = Paragraph('<font size=14.5 color="%s"><b>%s</b></font>' % (_hx(INK), title), CARDL)
    row = Table([[numbox, title_p]], colWidths=[10.5 * mm, CONTENT_W - 10.5 * mm])
    row.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (0, 0), 0),
        ("LEFTPADDING", (1, 0), (1, 0), 3), ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    parts = [row, HRFlowable(width="100%", thickness=1.2, color=INK, spaceBefore=5,
                             spaceAfter=(3 if subtitle else 8))]
    if subtitle:
        parts.append(Paragraph(subtitle, SMALL))
        parts.append(Spacer(1, 7))
    return KeepTogether(parts)


def sub_h(text):
    return Paragraph('<font size=10.5 color="%s"><b>%s</b></font>' % (_hx(INK), text), BODY)


def dtable(header, rows, widths, aligns=None, color_map=None, zebra=True, fs=8.5):
    """Tabla de datos con cabecera negra. color_map: {(r,c): color} para pintar celdas."""
    data = [header] + rows
    t = Table(data, colWidths=widths, repeatRows=1)
    st = [
        ("FONT", (0, 0), (-1, 0), FONT_B, fs), ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("TEXTCOLOR", (0, 0), (-1, 0), PAPER), ("FONT", (0, 1), (-1, -1), FONT, fs),
        ("TEXTCOLOR", (0, 1), (-1, -1), INK),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]
    if zebra:
        st.append(("ROWBACKGROUNDS", (0, 1), (-1, -1), [PAPER, ROWALT]))
    if aligns:
        for col, al in aligns.items():
            st.append(("ALIGN", (col, 0), (col, -1), al))
    if color_map:
        for (r, c), col in color_map.items():
            st.append(("TEXTCOLOR", (c, r), (c, r), col))
    t.setStyle(TableStyle(st))
    return t


def bar_chart(labels, series, series_colors, width=CONTENT_W, height=46 * mm):
    """Grafico de barras (una o varias series) con estetica limpia."""
    d = Drawing(width, height)
    bc = VerticalBarChart()
    bc.x = 13 * mm
    bc.y = 9 * mm
    bc.width = width - 18 * mm
    bc.height = height - 15 * mm
    bc.data = series
    bc.groupSpacing = 4
    bc.barSpacing = 0.4
    # Escala robusta: contempla valores NEGATIVOS (ej: margen de pedido en perdida).
    # Antes valueMin era fijo en 0 y las barras negativas se iban de la caja del grafico.
    allvals = [v for s in series for v in (s or [])] or [0]
    maxv, minv = max(allvals), min(allvals)
    top = maxv * 1.15 if maxv > 0 else 0
    bot = minv * 1.15 if minv < 0 else 0
    if top == bot:  # todo cero
        top = 1
    bc.valueAxis.valueMax = top
    bc.valueAxis.valueMin = bot
    bc.valueAxis.strokeColor = LINE
    bc.valueAxis.labels.fontName = FONT
    bc.valueAxis.labels.fontSize = 6.5
    bc.valueAxis.labels.fillColor = GREY
    bc.valueAxis.labelTextFormat = _kfmt
    bc.valueAxis.gridStrokeColor = LINE
    bc.valueAxis.visibleGrid = True
    bc.categoryAxis.categoryNames = labels
    bc.categoryAxis.labels.fontName = FONT
    bc.categoryAxis.labels.fontSize = 6.3
    bc.categoryAxis.labels.fillColor = GREY
    bc.categoryAxis.labels.dy = -1
    bc.categoryAxis.strokeColor = LINE
    for i, col in enumerate(series_colors):
        bc.bars[i].fillColor = col
        bc.bars[i].strokeColor = None
    d.add(bc)
    return d


def legend(items):
    """items: [(color, texto)] -> fila con cuadraditos de color reales (Space Grotesk
    no trae el glifo del cuadrado unicode, asi que se dibujan con celdas de tabla)."""
    cells, widths = [], []
    for col, txt in items:
        sw = Table([[""]], colWidths=[3.2 * mm], rowHeights=[3.2 * mm])
        sw.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), col)]))
        cells.append(sw)
        cells.append(Paragraph('<font size=8 color="%s">%s</font>' % (_hx(GREY), txt), SMALL))
        widths.append(3.2 * mm)
        widths.append(min(46 * mm, 6 * mm + len(txt) * 1.7 * mm))
    t = Table([cells], colWidths=widths, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING", (1, 0), (1, 0), 3), ("LEFTPADDING", (3, 0), (3, 0), 3),
    ]))
    return t


# ---------------------------------------------------------------------------
# Datos de VENTAS (autocontenido: replica la logica del panel de Ventas)
# ---------------------------------------------------------------------------
_CANALES = [("web", "Web"), ("instagram", "Instagram"), ("presencial", "Presencial"),
            ("otro", "Otro"), (None, "Sin definir")]
_METODOS = [("mercadopago", "Mercado Pago"), ("transferencia", "Transferencia"),
            ("efectivo", "Efectivo"), ("otro", "Otro"), (None, "Sin definir")]


def _ventas_maps():
    web_ids, metodo_res, cliente_res = set(), {}, {}
    try:
        for r in Reserva.query.all():
            met = ("mercadopago" if (r.pagado or r.mp_status == "approved" or r.metodo_pago == "mercadopago")
                   else ("transferencia" if r.metodo_pago == "transferencia" else None))
            for it in r.items_efectivos:
                if it.disc_id:
                    web_ids.add(it.disc_id)
                    if met:
                        metodo_res.setdefault(it.disc_id, met)
                    cliente_res.setdefault(it.disc_id, r.nombre)
    except Exception:
        pass
    return web_ids, metodo_res, cliente_res


def _ventas_data(periodo, desde, hasta):
    from datetime import datetime as _dt
    inicio, fin = metrics._rango_ventas(periodo, desde, hasta)
    inicio_dt = _dt(inicio.year, inicio.month, inicio.day)
    fin_dt = _dt(fin.year, fin.month, fin.day)
    web_ids, metodo_res, cliente_res = _ventas_maps()

    vendidos = (
        Disc.query.filter(Disc.estado == "Vendido")
        .filter(Disc.fecha_venta >= inicio_dt, Disc.fecha_venta < fin_dt)
        .order_by(Disc.fecha_venta.desc(), Disc.id.desc()).all()
    )

    def canal_de(d):
        return d.canal_venta or ("web" if d.id in web_ids else None)

    def metodo_de(d):
        return d.metodo_pago_venta or metodo_res.get(d.id)

    kpis = metrics.kpis_de_discos(vendidos)

    # periodo anterior (misma duracion) para comparar
    dur = fin_dt - inicio_dt
    prev = (
        Disc.query.filter(Disc.estado == "Vendido")
        .filter(Disc.fecha_venta >= inicio_dt - dur, Disc.fecha_venta < inicio_dt).all()
    )
    kprev = metrics.kpis_de_discos(prev)

    def _delta(cur, pv):
        return round((cur - pv) / pv * 100) if pv else None
    comp = {k: _delta(kpis[k], kprev[k]) for k in ("ventas", "facturado", "ganancia")}

    def _agrupar(keyfn, defs):
        acc = {}
        for d in vendidos:
            k = keyfn(d)
            a = acc.setdefault(k, {"discos": 0, "monto": 0.0})
            a["discos"] += d.copias_facturadas
            a["monto"] += (d.precio_mostrar or d.precio_uyu or 0) * d.copias_facturadas
        total = sum(a["monto"] for a in acc.values()) or 1
        out = [{"label": label, "discos": acc[k]["discos"], "monto": round(acc[k]["monto"]),
                "pct": round(acc[k]["monto"] / total * 100)} for k, label in defs if k in acc]
        return sorted(out, key=lambda x: x["monto"], reverse=True)

    por_canal = _agrupar(canal_de, _CANALES)
    por_metodo = _agrupar(metodo_de, _METODOS)

    # tendencia diaria (ultimos min(dur,30) dias, sin dibujar futuro)
    ndias = max(7, min(dur.days, 30))
    base_fin = min(fin_dt.date(), _dt.utcnow().date() + timedelta(days=1))
    dias = [base_fin - timedelta(days=i + 1) for i in range(ndias)][::-1]
    tmap = {dd: 0.0 for dd in dias}
    for d in vendidos:
        fv = d.fecha_venta.date() if d.fecha_venta else None
        if fv in tmap:
            tmap[fv] += (d.precio_mostrar or d.precio_uyu or 0) * d.copias_facturadas
    trend = [(dd, round(tmap[dd])) for dd in dias]

    # top sellos / artistas del periodo
    def _top(attr, limit=5):
        acc = {}
        for d in vendidos:
            k = (getattr(d, attr) or "Sin dato").strip() or "Sin dato"
            a = acc.setdefault(k, {"u": 0, "monto": 0.0})
            a["u"] += d.copias_facturadas
            a["monto"] += (d.precio_mostrar or d.precio_uyu or 0) * d.copias_facturadas
        out = [{"nombre": k, "u": v["u"], "monto": round(v["monto"])} for k, v in acc.items()]
        return sorted(out, key=lambda x: x["monto"], reverse=True)[:limit]

    return {
        "inicio": inicio.strftime("%d/%m/%Y"), "fin": (fin - timedelta(days=1)).strftime("%d/%m/%Y"),
        "kpis": kpis, "comp": comp, "por_canal": por_canal, "por_metodo": por_metodo,
        "trend": trend, "top_sellos": _top("sello"), "top_artistas": _top("artista"),
        "vendidos": vendidos, "cliente_de": cliente_res, "canal_de": canal_de, "metodo_de": metodo_de,
    }


# ---------------------------------------------------------------------------
# Nombres legibles de periodo
# ---------------------------------------------------------------------------
_PER_LABEL = {
    "hoy": "Hoy", "7dias": "Ultimos 7 dias", "mes_actual": "Este mes",
    "mes_pasado": "Mes pasado", "anio": "Este ano", "todo": "Todo el historial",
    "custom": "Periodo elegido",
}
# Categorias: se distinguen por intensidad dentro de la paleta (no por matiz).
# El verde/rojo queda reservado SOLO para el signo de las cifras.
_CANAL_COL = {"Web": INK, "Instagram": GOLD, "Presencial": BLUE, "Otro": GREY2, "Sin definir": GREY2}
_METODO_COL = {"Mercado Pago": INK, "Transferencia": BLUE, "Efectivo": GOLD, "Otro": GREY2, "Sin definir": GREY2}


# ---------------------------------------------------------------------------
# Chrome de pagina (encabezado + pie con numero de pagina)
# ---------------------------------------------------------------------------
def _draw_footer(canvas, doc):
    canvas.saveState()
    canvas.setFont(FONT, 7.5)
    canvas.setFillColor(GREY2)
    canvas.drawString(MARGIN, 9 * mm, "Arcano Record Store  ·  Montevideo  ·  numeros en pesos uruguayos (UYU)")
    canvas.drawRightString(PAGE_W - MARGIN, 9 * mm, "Pagina %d" % doc.page)
    canvas.setStrokeColor(LINE)
    canvas.setLineWidth(0.5)
    canvas.line(MARGIN, 12 * mm, PAGE_W - MARGIN, 12 * mm)
    canvas.restoreState()


def _first_page(canvas, doc):
    # Banda negra full-bleed arriba con el logo primario en blanco (regla del
    # manual: fondo oscuro -> logo blanco).
    canvas.saveState()
    band_h = 46 * mm
    canvas.setFillColor(INK)
    canvas.rect(0, PAGE_H - band_h, PAGE_W, band_h, fill=1, stroke=0)

    lw, lh = MARCA.logo_size(52 * mm)
    logo = MARCA.logo_path(sobre_oscuro=True)
    if logo:
        canvas.drawImage(logo, MARGIN, PAGE_H - 10 * mm - lh, width=lw, height=lh,
                         mask="auto", preserveAspectRatio=True, anchor="sw")
    else:
        canvas.setFillColor(PAPER)
        canvas.setFont(FONT_B, 30)
        canvas.drawString(MARGIN, PAGE_H - 24 * mm, "ARCANO")
    canvas.setFont(FONT_M, 8.5)
    canvas.setFillColor(BEIGE)
    canvas.drawString(MARGIN + 1, PAGE_H - 36 * mm, "M O N T E V I D E O   ·   U R U G U A Y")
    canvas.setStrokeColor(BEIGE)
    canvas.setLineWidth(1)
    canvas.line(MARGIN, PAGE_H - 40 * mm, MARGIN + 22 * mm, PAGE_H - 40 * mm)
    canvas.restoreState()
    _draw_footer(canvas, doc)


def _later_pages(canvas, doc):
    canvas.saveState()
    lw, lh = MARCA.logo_size(19 * mm)
    logo = MARCA.logo_path(sobre_oscuro=False)
    if logo:
        canvas.drawImage(logo, MARGIN, PAGE_H - 11.5 * mm, width=lw, height=lh,
                         mask="auto", preserveAspectRatio=True, anchor="sw")
    else:
        canvas.setFont(FONT_B, 8)
        canvas.setFillColor(INK)
        canvas.drawString(MARGIN, PAGE_H - 11 * mm, "ARCANO")
    canvas.setFont(FONT, 8)
    canvas.setFillColor(GREY)
    canvas.drawString(MARGIN + 23 * mm, PAGE_H - 11 * mm, "Informe ejecutivo de gestion")
    canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - 11 * mm, doc._per_label)
    canvas.setStrokeColor(LINE)
    canvas.setLineWidth(0.5)
    canvas.line(MARGIN, PAGE_H - 13 * mm, PAGE_W - MARGIN, PAGE_H - 13 * mm)
    canvas.restoreState()
    _draw_footer(canvas, doc)


# ---------------------------------------------------------------------------
# PDF principal
# ---------------------------------------------------------------------------
def generar_pdf_ejecutivo(periodo="mes_actual", desde=None, hasta=None, resumen_caja=None):
    per_label = _PER_LABEL.get(periodo, periodo)
    hoy_txt = datetime.now().strftime("%d/%m/%Y")

    # ---- Datos ----
    ventas = _ventas_data(periodo, desde, hasta)
    conta = metrics.contabilidad(periodo, desde, hasta)
    recup = metrics.recuperacion_inversion()
    frenado = metrics.stock_frenado()
    por_cobrar = metrics.dinero_por_cobrar()
    seis_m = metrics.ingresos_vs_egresos_6m()
    arc_per = metrics.arcano_vs_personal_6m()
    mets = metrics.get_all_metrics()
    pedidos = Pedido.query.order_by(Pedido.fecha.desc()).all()
    rc = resumen_caja or {}

    out = io.BytesIO()
    doc = SimpleDocTemplate(
        out, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=18 * mm, bottomMargin=16 * mm, title="Arcano - Informe ejecutivo",
        author="Arcano Record Store",
    )
    doc._per_label = per_label
    E = []

    # =====================================================================
    # PORTADA + RESUMEN EJECUTIVO
    # =====================================================================
    E.append(Spacer(1, 34 * mm))   # despeja la banda negra del canvas
    E.append(Paragraph("Informe ejecutivo de gestion", H1))
    E.append(Spacer(1, 3))
    E.append(Paragraph("Periodo: <b>%s</b> &nbsp;(%s a %s) &nbsp;·&nbsp; generado el %s"
                       % (per_label, conta["inicio"], conta["fin"], hoy_txt), LEAD))
    E.append(HRFlowable(width="100%", thickness=2, color=INK, spaceBefore=10, spaceAfter=14))

    k = ventas["kpis"]
    gan_pct = conta["comparacion"]["var_ganancia_pct"] if conta.get("comparacion") else None
    gtxt, gcol = _delta_txt(gan_pct)
    E.append(kpi_row([
        {"label": "Ganancia real del periodo", "value": _m(conta["ganancia_real_uyu"]),
         "color": (GREEN if conta["ganancia_real_uyu"] >= 0 else RED), "sub": gtxt, "subcolor": gcol},
        {"label": "Facturado (ventas)", "value": _m(k["facturado"]),
         "color": INK, "sub": "%s unidades · %s ventas" % (_n(k["unidades"]), _n(k["ventas"]))},
        {"label": "Plata en caja hoy", "value": _m(rc.get("disponible", 0)),
         "color": BLUE, "sub": "en el banco, a hoy"},
    ]))
    E.append(Spacer(1, 8))
    E.append(kpi_row([
        {"label": "En mercaderia (stock)", "value": _m(rc.get("mercaderia_uyu", 0)),
         "color": GOLD, "sub": "%s discos sin vender" % _n(rc.get("mercaderia_count", 0))},
        {"label": "Inversion recuperada", "value": _pct(recup.get("pct_real", 0)),
         "color": GREEN, "sub": ("recuperaste todo" if recup.get("recuperado_total") else "de tus pedidos")},
        {"label": "Por cobrar (reservas)", "value": _m(por_cobrar.get("total_uyu", 0)),
         "color": (RED if por_cobrar.get("titulos") else GREY2),
         "sub": "%s disco(s) reservados" % _n(por_cobrar.get("titulos", 0))},
    ]))
    E.append(Spacer(1, 14))

    # Narrativa de situacion
    E.append(sub_h("La situacion en pocas palabras"))
    E.append(Spacer(1, 4))
    frases = []
    if k["ventas"]:
        f = ("En <b>%s</b> se vendieron <b>%s discos</b> en <b>%s ventas</b> por <b>%s</b>, "
             "con una ganancia de <b>%s</b> (margen %s). El ticket promedio fue <b>%s</b>."
             % (per_label.lower(), _n(k["unidades"]), _n(k["ventas"]), _m(k["facturado"]),
                _m(k["ganancia"]), _pct(k["margen"]), _m(k["ticket"])))
        if gan_pct is not None:
            f += (" La ganancia real %s <b>%d%%</b> respecto al periodo anterior."
                  % ("subio" if gan_pct >= 0 else "bajo", abs(gan_pct)))
        frases.append(f)
    else:
        frases.append("En %s no se registraron ventas todavia." % per_label.lower())

    f2 = ("Hoy el negocio tiene <b>%s</b> en el banco y <b>%s</b> metidos en mercaderia "
          "(%s discos de Arcano sin vender). " % (_m(rc.get("disponible", 0)),
          _m(rc.get("mercaderia_uyu", 0)), _n(rc.get("mercaderia_count", 0))))
    if recup.get("recuperado_total"):
        f2 += "Ya recuperaste toda la inversion de tus pedidos: lo que vendas de ahora en mas es ganancia limpia."
    elif recup.get("pedidos_count"):
        f2 += ("Recuperaste el <b>%s</b> de lo invertido en pedidos; te faltan vender <b>%s discos</b> (%s)."
               % (_pct(recup.get("pct_real", 0)), _n(recup.get("discos_sin_vender", 0)), _m(recup.get("falta", 0))))
    frases.append(f2)

    if frenado.get("frenados_count"):
        frases.append("Atencion: hay <b>%s discos</b> parados hace mas de %s dias, con <b>%s</b> quietos en stock. "
                      "Conviene empujarlos o rematarlos." % (_n(frenado["frenados_count"]),
                      _n(frenado["umbral"]), _m(frenado["plata_frenada"])))
    if rc.get("adelanto_pendiente_total"):
        frases.append("De tu bolsillo pusiste <b>%s</b> como adelanto de Mercado Pago; los recuperas cuando MP libere esas ventas."
                      % _m(rc["adelanto_pendiente_total"]))
    for fr in frases:
        E.append(Paragraph(fr, BODY))
        E.append(Spacer(1, 5))

    # =====================================================================
    # 1. VENTAS
    # =====================================================================
    E.append(PageBreak())
    E.append(section("1", "Ventas", "Rendimiento comercial del periodo: cuanto se vendio, por que canal y con que "
                     "forma de pago. Una venta puede tener varios discos."))
    ct, ccol = _delta_txt(ventas["comp"]["facturado"])
    E.append(kpi_row([
        {"label": "Ventas", "value": _n(k["ventas"]), "color": INK, "sub": "transacciones"},
        {"label": "Unidades", "value": _n(k["unidades"]), "color": INK, "sub": "discos vendidos"},
        {"label": "Facturado", "value": _m(k["facturado"]), "color": GREEN, "sub": ct, "subcolor": ccol},
        {"label": "Ganancia", "value": _m(k["ganancia"]), "color": GREEN, "sub": "margen %s" % _pct(k["margen"])},
        {"label": "Ticket prom.", "value": _m(k["ticket"]), "color": BLUE, "sub": "por venta"},
    ]))
    E.append(Spacer(1, 12))

    if ventas["trend"] and any(m for _, m in ventas["trend"]):
        E.append(sub_h("Facturado por dia"))
        E.append(Spacer(1, 4))
        tr = ventas["trend"]
        # aclarar etiquetas: mostrar 1 de cada N para que no se amontonen
        step = max(1, len(tr) // 12)
        labels = [(dd.strftime("%d/%m") if i % step == 0 else "") for i, (dd, _) in enumerate(tr)]
        E.append(bar_chart(labels, [[m for _, m in tr]], [GREEN], height=40 * mm))
        E.append(Spacer(1, 10))

    # Canal + metodo, lado a lado
    def _dist_table(titulo, filas, colmap):
        rows, cmap = [], {}
        for i, r in enumerate(filas, start=1):
            rows.append([r["label"], _n(r["discos"]), _m(r["monto"]), _pct(r["pct"])])
            cmap[(i, 0)] = colmap.get(r["label"], INK)
        body = dtable([titulo, "Disc.", "Monto", "%"], rows or [["Sin datos", "", "", ""]],
                      [34 * mm, 14 * mm, 26 * mm, 14 * mm],
                      aligns={1: "CENTER", 2: "RIGHT", 3: "RIGHT"}, color_map=cmap)
        return body

    dual = Table([[_dist_table("Por canal", ventas["por_canal"], _CANAL_COL),
                   _dist_table("Por forma de pago", ventas["por_metodo"], _METODO_COL)]],
                 colWidths=[CONTENT_W / 2 - 3, CONTENT_W / 2 - 3])
    dual.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (0, 0), 6)]))
    E.append(dual)
    E.append(Spacer(1, 12))

    # Top sellos / artistas
    if ventas["top_sellos"]:
        ts = [[s["nombre"][:30], _n(s["u"]), _m(s["monto"])] for s in ventas["top_sellos"][:4]]
        ta = [[a["nombre"][:30], _n(a["u"]), _m(a["monto"])] for a in ventas["top_artistas"][:4]]
        tsT = dtable(["Sello mas vendido", "Disc.", "Monto"], ts, [40 * mm, 14 * mm, 26 * mm],
                     aligns={1: "CENTER", 2: "RIGHT"})
        taT = dtable(["Artista mas vendido", "Disc.", "Monto"], ta, [40 * mm, 14 * mm, 26 * mm],
                     aligns={1: "CENTER", 2: "RIGHT"})
        dual2 = Table([[tsT, taT]], colWidths=[CONTENT_W / 2 - 3, CONTENT_W / 2 - 3])
        dual2.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                   ("RIGHTPADDING", (0, 0), (0, 0), 6)]))
        E.append(dual2)
        E.append(Spacer(1, 12))

    # Ultimas ventas (envuelto en KeepTogether para no dejar filas huerfanas)
    uv = [sub_h("Ultimas ventas del periodo"), Spacer(1, 4)]
    vd = ventas["vendidos"][:6]
    if vd:
        rows = []
        canal_lbl = dict(_CANALES)
        for d in vd:
            nombre = ("%s - %s" % (d.artista or "", d.titulo or ""))[:38]
            cli = ventas["cliente_de"].get(d.id, "-")
            canal = canal_lbl.get(ventas["canal_de"](d), "-")
            rows.append([d.fecha_venta.strftime("%d/%m") if d.fecha_venta else "-", nombre,
                         (cli or "-")[:18], canal, _m(d.precio_mostrar or d.precio_uyu or 0)])
        uv.append(dtable(["Fecha", "Disco", "Cliente", "Canal", "Precio"], rows,
                         [15 * mm, 66 * mm, 34 * mm, 26 * mm, 27 * mm],
                         aligns={4: "RIGHT"}))
    else:
        uv.append(Paragraph("No hubo ventas en este periodo.", BODY))
    E.append(KeepTogether(uv))

    # =====================================================================
    # 2. CONTABILIDAD
    # =====================================================================
    E.append(PageBreak())
    E.append(section("2", "Contabilidad", "La ganancia real descuenta el costo de los discos de Arcano (vendidos y en "
                     "stock) y los gastos operativos. Separa lo que aporta tu coleccion personal."))
    E.append(kpi_row([
        {"label": "Ingresos (ventas)", "value": _m(conta["ingresos_uyu"]), "color": GREEN},
        {"label": "Egresos (gastos)", "value": _m(conta["egresos_uyu"]), "color": RED},
        {"label": "Ganancia bruta", "value": _m(conta["ganancia_bruta_uyu"]), "color": INK,
         "sub": "ventas - costo vendidos"},
        {"label": "Ganancia real", "value": _m(conta["ganancia_real_uyu"]),
         "color": (GREEN if conta["ganancia_real_uyu"] >= 0 else RED), "sub": gtxt, "subcolor": gcol},
    ]))
    E.append(Spacer(1, 12))

    E.append(sub_h("Por origen: negocio vs coleccion personal"))
    E.append(Spacer(1, 4))
    E.append(kpi_row([
        {"label": "Ingresos de Arcano", "value": _m(conta["ingresos_arcano_uyu"]), "color": INK},
        {"label": "Aporte de tu coleccion", "value": _m(conta["ingresos_personal_uyu"]), "color": BLUE,
         "sub": "%s discos personales" % _n(conta["unidades_personal"])},
        {"label": "Stock Arcano sin vender", "value": _m(conta["costo_stock_no_vendido_uyu"]), "color": GOLD,
         "sub": "%s titulos" % _n(conta["stock_no_vendido_titulos"])},
    ]))
    E.append(Spacer(1, 6))
    if conta["ingresos_personal_uyu"]:
        E.append(Paragraph("De los ingresos, <b>%s</b> vienen de tu coleccion personal: no le costaron nada al negocio, "
                           "asi que suman como aporte tuyo." % _m(conta["ingresos_personal_uyu"]), BODY))
        E.append(Spacer(1, 8))

    # Grafico 6 meses (mirada de caja)
    if seis_m:
        E.append(sub_h("Plata que entro vs salio (ultimos 6 meses)"))
        E.append(Paragraph("Mirada de caja: los egresos incluyen los pedidos (compras de stock). El neto es entro menos salio.", SMALL))
        E.append(Spacer(1, 4))
        labels = [m["label"] for m in seis_m]
        E.append(bar_chart(labels, [[round(m["ingresos_uyu"]) for m in seis_m],
                                     [round(m["egresos_uyu"]) for m in seis_m]], [GREEN, RED], height=42 * mm))
        E.append(legend([(GREEN, "Ingresos"), (RED, "Egresos (con pedidos)")]))
        E.append(Spacer(1, 10))

    # Gastos por categoria
    if conta["egresos_por_categoria"]:
        E.append(sub_h("Gastos del periodo por categoria"))
        E.append(Spacer(1, 4))
        rows = [[c or "Otros", _m(mnt)] for c, mnt in conta["egresos_por_categoria"]]
        E.append(dtable(["Categoria", "Monto"], rows, [130 * mm, 48 * mm], aligns={1: "RIGHT"}))

    # =====================================================================
    # 3. CAJA / BANCO
    # =====================================================================
    E.append(PageBreak())
    E.append(section("3", "Caja / Banco", "Foto de la plata a hoy: lo que tenes en el banco, lo retenido en Mercado "
                     "Pago, lo metido en mercaderia y el capital que pusiste vos."))
    E.append(kpi_row([
        {"label": "Plata en caja hoy", "value": _m(rc.get("disponible", 0)), "color": GREEN, "sub": "en el banco"},
        {"label": "En Mercado Pago", "value": _m(rc.get("por_adelantar", 0)), "color": BLUE,
         "sub": "sin adelantar (paga ~30 dias)"},
        {"label": "En mercaderia", "value": _m(rc.get("mercaderia_uyu", 0)), "color": GOLD,
         "sub": "%s discos" % _n(rc.get("mercaderia_count", 0))},
        {"label": "Capital que pusiste", "value": _m(rc.get("capital_permanente", 0)), "color": INK,
         "sub": "aportes tuyos"},
    ]))
    E.append(Spacer(1, 12))

    pm = rc.get("por_metodo", {}) or {}
    if any(pm.values()):
        E.append(sub_h("Ventas cobradas por forma de pago (historico)"))
        E.append(Spacer(1, 4))
        _pm_lbl = [("mercadopago", "Mercado Pago", BLUE), ("transferencia", "Transferencia", GREEN),
                   ("efectivo", "Efectivo", GOLD), ("otro", "Otro", GREY2), ("sin", "Sin especificar", GREY2)]
        rows, cmap = [], {}
        i = 1
        for key, lbl, col in _pm_lbl:
            if pm.get(key):
                rows.append([lbl, _m(pm[key])])
                cmap[(i, 0)] = col
                i += 1
        E.append(dtable(["Forma de pago", "Cobrado"], rows, [130 * mm, 48 * mm],
                        aligns={1: "RIGHT"}, color_map=cmap))
        E.append(Spacer(1, 12))

    # Adelantos pendientes
    adl = rc.get("adelantos_pendientes") or []
    if adl:
        E.append(sub_h("Adelantos de Mercado Pago a recuperar"))
        E.append(Spacer(1, 4))
        rows = [[(m.fecha.strftime("%d/%m/%y") if m.fecha else "-"), _m(m.monto_uyu or 0)] for m in adl]
        rows.append(["TOTAL", _m(rc.get("adelanto_pendiente_total", 0))])
        E.append(dtable(["Adelantado el", "Monto"], rows, [130 * mm, 48 * mm], aligns={1: "RIGHT"}))
        E.append(Spacer(1, 12))

    # Ultimos movimientos
    filas = (rc.get("filas") or [])[:10]
    if filas:
        E.append(sub_h("Ultimos movimientos"))
        E.append(Spacer(1, 4))
        rows, cmap = [], {}
        for i, f in enumerate(filas, start=1):
            m = f["mov"]
            signo = "+" if m.tipo == "ingreso" else "−"
            rows.append([m.fecha.strftime("%d/%m/%y") if m.fecha else "-", (m.concepto or "-")[:44],
                         "%s %s" % (signo, _m(m.monto_uyu or 0).replace("$ ", "")), _m(f["saldo"])])
            cmap[(i, 2)] = GREEN if m.tipo == "ingreso" else RED
        E.append(dtable(["Fecha", "Concepto", "Monto", "Saldo"], rows,
                        [18 * mm, 96 * mm, 30 * mm, 34 * mm],
                        aligns={2: "RIGHT", 3: "RIGHT"}, color_map=cmap))

    # =====================================================================
    # 4. PEDIDOS
    # =====================================================================
    E.append(PageBreak())
    E.append(section("4", "Pedidos", "Compras de stock a proveedores (Deejay.de, viajes). Muestra cuanto costo cada "
                     "pedido, cuanto recuperaste vendiendo y como viene la rotacion."))
    if pedidos:
        con_ventas = [p for p in pedidos if p.discos_vendidos > 0 and p.costo_total_uyu > 0]
        inv_total = sum(p.costo_total_uyu for p in pedidos)
        rec_total = sum(p.recaudado_uyu for p in pedidos)
        res_total = rec_total - inv_total
        E.append(kpi_row([
            {"label": "Pedidos", "value": _n(len(pedidos)), "color": INK},
            {"label": "Invertido", "value": _m(inv_total), "color": RED},
            {"label": "Recuperado", "value": _m(rec_total), "color": GREEN},
            {"label": "Resultado", "value": _m(res_total), "color": (GREEN if res_total >= 0 else RED),
             "sub": ("ganando" if res_total >= 0 else "todavia en rojo")},
        ]))
        E.append(Spacer(1, 12))

        # grafico margen por pedido (los que tienen ventas)
        if con_ventas:
            top = sorted(con_ventas, key=lambda p: p.fecha or datetime.min)[-8:]
            labels = [(p.numero or "-")[:8] for p in top]
            E.append(sub_h("Margen por pedido (%)"))
            E.append(Spacer(1, 4))
            E.append(bar_chart(labels, [[round(p.margen_pct or 0) for p in top]], [BLUE], height=42 * mm))
            E.append(Spacer(1, 10))

        rows, cmap = [], {}
        sem_col = {"verde": GREEN, "amarillo": GOLD, "rojo": RED}
        for i, p in enumerate(pedidos[:16], start=1):
            rows.append([(p.numero or "-")[:14], p.fecha.strftime("%d/%m/%y") if p.fecha else "-",
                         "%s/%s" % (_n(p.discos_vendidos), _n(p.discos_asignados)),
                         _m(p.costo_total_uyu), _m(p.recaudado_uyu), _m(p.resultado_uyu), _pct(p.rotacion_pct)])
            cmap[(i, 5)] = GREEN if (p.resultado_uyu or 0) >= 0 else RED
            cmap[(i, 6)] = sem_col.get(p.rotacion_semaforo, GREY)
        E.append(dtable(["Numero", "Fecha", "Vend/Total", "Costo", "Recaudado", "Resultado", "Rotacion"],
                        rows, [28 * mm, 18 * mm, 22 * mm, 27 * mm, 28 * mm, 27 * mm, 22 * mm],
                        aligns={2: "CENTER", 3: "RIGHT", 4: "RIGHT", 5: "RIGHT", 6: "RIGHT"}, color_map=cmap))
    else:
        E.append(Paragraph("Todavia no cargaste pedidos de compra.", BODY))

    # =====================================================================
    # 5. RADAR DE DEMANDA
    # =====================================================================
    E.append(PageBreak())
    E.append(section("5", "Radar de demanda", "Que discos generan mas interes segun las escuchas de la web (reproducciones "
                     "por dia disponible). Los disponibles son accionables; los vendidos validan tu ojo."))
    radar = _radar_data()
    E.append(kpi_row([
        {"label": "Discos con escuchas", "value": _n(radar["con_escuchas"]), "color": INK},
        {"label": "Disponibles con interes", "value": _n(len(radar["disponibles"])), "color": GREEN,
         "sub": "para empujar"},
        {"label": "Sin escuchas", "value": _n(radar["sin_escuchas"]), "color": GREY2},
    ]))
    E.append(Spacer(1, 12))

    if radar["disponibles"]:
        E.append(sub_h("Disponibles con mas interes (accionables)"))
        E.append(Spacer(1, 4))
        rows = []
        for f in radar["disponibles"][:8]:
            d = f["disc"]
            nombre = ("%s - %s" % (d.artista or "", d.titulo or ""))[:32]
            rows.append([nombre, (d.sello or "-")[:16], _n(f["plays"]), _n(f["dias"]),
                         "%.2f" % f["interes"], d.estado])
        E.append(dtable(["Disco", "Sello", "Escuchas", "Dias", "Interes/dia", "Estado"], rows,
                        [58 * mm, 30 * mm, 20 * mm, 16 * mm, 24 * mm, 30 * mm],
                        aligns={2: "CENTER", 3: "CENTER", 4: "CENTER"}))
        E.append(Spacer(1, 12))

    if radar["vendidos"]:
        E.append(sub_h("Vendidos con mas interes (te dieron la razon)"))
        E.append(Spacer(1, 4))
        rows = []
        for f in radar["vendidos"][:6]:
            d = f["disc"]
            nombre = ("%s - %s" % (d.artista or "", d.titulo or ""))[:40]
            rows.append([nombre, (d.sello or "-")[:16], _n(f["plays"]), "%.2f" % f["interes"]])
        E.append(dtable(["Disco", "Sello", "Escuchas", "Interes/dia"], rows,
                        [70 * mm, 34 * mm, 22 * mm, 26 * mm], aligns={2: "CENTER", 3: "CENTER"}))

    if not radar["disponibles"] and not radar["vendidos"]:
        E.append(Paragraph("Todavia no hay suficientes escuchas para armar el radar. Se va llenando solo a medida que "
                           "la gente reproduce discos en la web.", BODY))

    doc.build(E, onFirstPage=_first_page, onLaterPages=_later_pages)
    out.seek(0)
    return out


def _radar_data():
    """Mismo calculo que la ruta radar_demanda, autocontenido."""
    ahora = datetime.utcnow()
    filas = []
    for d in Disc.query.filter(Disc.estado != "Descartado").all():
        plays = sum((t.play_count or 0) for t in d.tracks)
        if d.fecha_ingreso:
            fin = d.fecha_venta if (d.estado == "Vendido" and d.fecha_venta) else ahora
            dias = max((fin - d.fecha_ingreso).days, 1)
        else:
            dias = 1
        filas.append({"disc": d, "plays": plays, "dias": dias, "interes": round(plays / dias, 2)})
    con = [f for f in filas if f["plays"] > 0]
    disp = sorted([f for f in con if f["disc"].estado in ("Disponible", "Pre-order", "Reservado", "Apartado para mi")],
                  key=lambda f: f["interes"], reverse=True)
    vend = sorted([f for f in con if f["disc"].estado == "Vendido"], key=lambda f: f["interes"], reverse=True)
    return {"disponibles": disp, "vendidos": vend, "con_escuchas": len(con),
            "sin_escuchas": len(filas) - len(con), "total": len(filas)}
