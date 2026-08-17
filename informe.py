# -*- coding: utf-8 -*-
"""Genera el informe PDF de contabilidad de un periodo (para /admin/contabilidad).

Reutiliza las metricas de metrics.py. Devuelve un io.BytesIO listo para servir.
"""
import io
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, HRFlowable, PageBreak)
from reportlab.graphics.shapes import Drawing
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.legends import Legend

import metrics
from models import Disc, Pedido, MovimientoCaja, Setting


def _money(n):
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        n = 0
    return "$ " + format(int(round(n)), ",d").replace(",", ".")


def _ts_tabla():
    """Estilo reutilizable para las tablas de tops/pedidos del informe."""
    from reportlab.lib import colors as _c
    return TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.5), ("BACKGROUND", (0, 0), (-1, 0), _c.HexColor("#0A0A0A")),
        ("TEXTCOLOR", (0, 0), (-1, 0), _c.white), ("FONT", (0, 1), (-1, -1), "Helvetica", 8.5),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 4.2), ("BOTTOMPADDING", (0, 0), (-1, -1), 4.2),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [_c.white, _c.HexColor("#faf9f6")]),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, _c.HexColor("#e3e2dc")),
    ])


def _chequeos():
    """Chequeos rapidos de inconsistencias sobre la data actual."""
    discs = Disc.query.all()
    def per(d): return (d.origen or "arcano") == "personal"
    vend = [d for d in discs if d.estado == "Vendido"]
    pid = set(p.id for p in Pedido.query.all())
    checks = [
        ("Ventas sin fecha (no cuentan en periodos)", len([d for d in vend if not d.fecha_venta])),
        ("Vendidos de Arcano sin costo cargado", len([d for d in vend if not per(d) and not (d.costo_final_uyu or 0)])),
        ("Discos activos con precio en 0", len([d for d in discs if d.estado in ("Disponible", "Vendido", "Reservado") and not (d.precio_uyu or 0)])),
        ("Discos apuntando a un pedido borrado", len([d for d in discs if d.pedido_id and d.pedido_id not in pid])),
        ("Ofertas sin descuento real", len([d for d in discs if d.precio_oferta and d.precio_uyu and d.precio_oferta >= d.precio_uyu])),
        ("Disponibles con stock 0", len([d for d in discs if d.estado == "Disponible" and (d.stock or 0) <= 0])),
    ]
    return checks


def _grafico_barras(labels, series, colores, ancho_mm=174, alto=130):
    """Devuelve un Drawing con un grafico de barras agrupadas.
    series: lista de listas (una por serie). colores: lista de HexColor."""
    w = ancho_mm * mm
    d = Drawing(w, alto)
    bc = VerticalBarChart()
    bc.x = 34
    bc.y = 16
    bc.width = w - 48
    bc.height = alto - 28
    bc.data = series
    bc.categoryAxis.categoryNames = labels
    bc.categoryAxis.labels.fontSize = 7
    bc.valueAxis.labels.fontSize = 7
    todos = [v for s in series for v in s]
    bc.valueAxis.valueMin = min(0, min(todos)) if todos else 0
    bc.groupSpacing = 8
    bc.barSpacing = 0.5
    for i, col in enumerate(colores):
        if i < len(bc.bars):
            bc.bars[i].fillColor = col
    d.add(bc)
    return d


def generar_informe_pdf(periodo="mes_actual", desde=None, hasta=None,
                        caja_disponible=None, mercaderia_uyu=None):
    conta = metrics.contabilidad(periodo, desde, hasta)
    recup = metrics.recuperacion_inversion()
    frenado = metrics.stock_frenado()
    mets = metrics.get_all_metrics()
    kpis = mets["kpis"]
    por_cobrar = metrics.dinero_por_cobrar()
    seis_m = metrics.ingresos_vs_egresos_6m()
    arc_per = metrics.arcano_vs_personal_6m()
    pedidos = Pedido.query.order_by(Pedido.fecha.desc()).all()

    # "Plata en caja": usar el MISMO numero que la pantalla de Caja (_resumen_caja
    # -> disponible), que el route pasa aca. Antes se recalculaba a mano sumando los
    # adelantos de MP y sin descontar lo retenido en MP -> sobreestimaba el saldo.
    if caja_disponible is not None:
        saldo = caja_disponible
    else:
        movs = MovimientoCaja.query.all()
        try:
            si = float(Setting.get("caja_saldo_inicial") or 0)
        except (TypeError, ValueError):
            si = 0.0
        ing_c = sum(m.monto_uyu or 0 for m in movs if m.tipo == "ingreso")
        egr_c = sum(m.monto_uyu or 0 for m in movs if m.tipo == "egreso")
        saldo = si + ing_c - egr_c

    if mercaderia_uyu is not None:
        merc = mercaderia_uyu
    else:
        stock = (
            Disc.query
            .filter((Disc.estado == "Disponible") | ((Disc.estado == "Pre-order") & (Disc.pedido_id.isnot(None))))
            .filter((Disc.origen != "personal") | (Disc.origen.is_(None)))
            .all()
        )
        merc = sum((d.costo_final_uyu or 0) * (d.stock or 1) for d in stock)

    chequeos = _chequeos()
    hay_problemas = any(n > 0 and "sin descuento" not in nombre for nombre, n in chequeos)

    # nombres de periodo legibles
    per_label = {
        "mes_actual": "Este mes", "mes_pasado": "Mes pasado",
        "anio": "Este ano", "todo": "Todo el historial", "custom": "Periodo elegido",
    }.get(periodo, periodo)

    BLACK = colors.HexColor("#0A0A0A"); GREEN = colors.HexColor("#0f7a53")
    RED = colors.HexColor("#c0392b"); GREY = colors.HexColor("#6b6b6b")
    LIGHT = colors.HexColor("#f2f1ec")
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=19, textColor=BLACK, spaceAfter=2, leading=22)
    sub = ParagraphStyle("sub", parent=styles["Normal"], fontSize=9.5, textColor=GREY)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=13, textColor=BLACK, spaceBefore=14, spaceAfter=6, leading=15)
    body = ParagraphStyle("body", parent=styles["Normal"], fontSize=10, leading=15, textColor=colors.HexColor("#1a1a1a"))
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8.5, leading=12, textColor=GREY)

    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=18*mm, rightMargin=18*mm,
                            topMargin=16*mm, bottomMargin=15*mm, title="Informe Arcano")
    E = []
    E.append(Paragraph("ARCANO RECORD STORE", h1))
    E.append(Paragraph("Informe de contabilidad &middot; %s (%s a %s) &middot; generado el %s"
                       % (per_label, conta["inicio"], conta["fin"], datetime.now().strftime("%d/%m/%Y")), sub))
    E.append(HRFlowable(width="100%", thickness=2, color=BLACK, spaceBefore=6, spaceAfter=10))

    def mrow(items):
        data = [[Paragraph("<font size=8 color='#6b6b6b'>%s</font><br/><font size=15><b>%s</b></font>" % (l, v), body) for l, v in items]]
        t = Table(data, colWidths=[(174*mm)/len(items)]*len(items))
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), LIGHT), ("INNERGRID", (0, 0), (-1, -1), 4, colors.white),
            ("TOPPADDING", (0, 0), (-1, -1), 10), ("BOTTOMPADDING", (0, 0), (-1, -1), 10), ("LEFTPADDING", (0, 0), (-1, -1), 10)]))
        return t

    # Resultado del periodo
    E.append(Paragraph("Resultado del periodo", h2))
    E.append(mrow([("Ingresos (ventas)", _money(conta["ingresos_uyu"])),
                   ("Egresos (gastos)", _money(conta["egresos_uyu"])),
                   ("Ganancia real", _money(conta["ganancia_real_uyu"]))]))
    E.append(Spacer(1, 5))
    comp = conta.get("comparacion")
    if comp and comp.get("var_ganancia_pct") is not None:
        signo = "subio" if comp["mejoro"] else "bajo"
        E.append(Paragraph("La ganancia real %s <b>%s%%</b> vs el periodo anterior (antes %s)."
                           % (signo, abs(comp["var_ganancia_pct"]), _money(comp["ganancia_real_ant"])), body))
    _stock_txt = ("La ganancia real descuenta el costo de los discos de Arcano (vendidos y en stock) y los gastos operativos."
                  if conta.get("stock_en_ganancia")
                  else "La ganancia real es el resultado operativo del periodo (ventas menos costo de lo vendido de Arcano menos gastos); "
                       "el stock no vendido NO se resta en periodos pasados (es una foto de hoy).")
    E.append(Paragraph("Se vendieron <b>%d unidades</b> (%d titulos) en el periodo. %s"
                       % (conta["unidades_vendidas"], conta["titulos_vendidos"], _stock_txt), small))

    # Desglose por origen (Arcano vs coleccion personal)
    E.append(mrow([("Ingresos de Arcano", _money(conta["ingresos_arcano_uyu"])),
                   ("Aporte de tu coleccion", _money(conta["ingresos_personal_uyu"])),
                   ("Stock no vendido (Arcano)", _money(conta["costo_stock_no_vendido_uyu"]))]))
    if conta["ingresos_personal_uyu"]:
        E.append(Spacer(1, 4))
        E.append(Paragraph("De los ingresos, <b>%s</b> vienen de tu coleccion personal (no le costaron nada al negocio)."
                           % _money(conta["ingresos_personal_uyu"]), small))

    # Estado actual del negocio
    E.append(Paragraph("Estado actual del negocio (hoy)", h2))
    E.append(mrow([("Plata en caja", _money(saldo)),
                   ("En mercaderia (stock)", _money(merc)),
                   ("Inversion recuperada", "%d%%" % round(recup["pct_real"]))]))
    E.append(Spacer(1, 5))
    if recup["pedidos_count"] > 0 and not recup["recuperado_total"]:
        E.append(Paragraph("De los pedidos invertiste <b>%s</b> y recuperaste <b>%s</b>. Te faltan vender "
                           "<b>%d discos</b> para recuperar todo (%s)." % (_money(recup["invertido"]),
                           _money(recup["recuperado"]), recup["discos_sin_vender"], _money(recup["falta"])), body))
    elif recup["recuperado_total"]:
        E.append(Paragraph("Ya recuperaste toda tu inversion en pedidos: lo que vendas de ahora en mas es ganancia limpia.", body))

    # Evolucion de los ultimos 6 meses (mirada de caja) + grafico
    E.append(Paragraph("Plata que entro vs salio (ultimos 6 meses)", h2))
    E.append(Paragraph("Mirada de caja: los egresos incluyen los pedidos (compras de stock). El neto es lo que entro menos lo que salio.", small))
    if seis_m:
        labels = [m["label"] for m in seis_m]
        serie_ing = [round(m["ingresos_uyu"]) for m in seis_m]
        serie_egr = [round(m["egresos_uyu"]) for m in seis_m]
        E.append(_grafico_barras(labels, [serie_ing, serie_egr], [GREEN, RED]))
        E.append(Paragraph("<font color='#0f7a53'>Verde: ingresos</font> &nbsp; <font color='#c0392b'>Rojo: egresos (con pedidos)</font>", small))
        data6 = [["Mes", "Ingresos", "Egresos", "Gastos", "Pedidos", "Neto"]]
        for m in seis_m:
            data6.append([m["label"], _money(m["ingresos_uyu"]), _money(m["egresos_uyu"]),
                          _money(m["egresos_opex_uyu"]), _money(m["pedidos_uyu"]), _money(m["neto_uyu"])])
        t = Table(data6, colWidths=[24*mm, 30*mm, 30*mm, 30*mm, 30*mm, 30*mm])
        t.setStyle(TableStyle([("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8), ("BACKGROUND", (0, 0), (-1, 0), BLACK),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("FONT", (0, 1), (-1, -1), "Helvetica", 8),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#faf9f6")]),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#e3e2dc"))]))
        E.append(t)

    # Arcano vs coleccion personal
    if arc_per and any(m["arcano"] or m["personal"] for m in arc_per):
        E.append(Paragraph("Arcano vs tu coleccion personal (6 meses)", h2))
        labels = [m["label"] for m in arc_per]
        E.append(_grafico_barras(labels, [[round(m["arcano"]) for m in arc_per],
                                          [round(m["personal"]) for m in arc_per]], [BLACK, colors.HexColor("#3b82f6")]))
        E.append(Paragraph("<font color='#0a0a0a'>Negro: ventas de Arcano</font> &nbsp; <font color='#3b82f6'>Azul: tu coleccion</font>", small))

    # Egresos por categoria
    if conta.get("egresos_por_categoria"):
        E.append(Paragraph("Egresos por categoria (periodo)", h2))
        data = [["Categoria", "Total"]]
        for cat, monto in conta["egresos_por_categoria"]:
            data.append([cat or "(sin categoria)", _money(monto)])
        data.append(["TOTAL", _money(conta["egresos_uyu"])])
        t = Table(data, colWidths=[130*mm, 44*mm])
        n = len(conta["egresos_por_categoria"])
        t.setStyle(TableStyle([("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9), ("BACKGROUND", (0, 0), (-1, 0), BLACK),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
            ("ALIGN", (1, 0), (1, -1), "RIGHT"), ("TOPPADDING", (0, 0), (-1, -1), 4.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
            ("FONT", (0, n + 1), (-1, n + 1), "Helvetica-Bold", 9), ("LINEABOVE", (0, n + 1), (-1, n + 1), 0.8, BLACK),
            ("ROWBACKGROUNDS", (0, 1), (-1, n), [colors.white, colors.HexColor("#faf9f6")])]))
        E.append(t)

    # Top sellos y discos
    E.append(PageBreak())
    E.append(Paragraph("Top sellos y discos", h2))
    tsi = mets["top_sellos_inversion"]
    if tsi:
        E.append(Paragraph("<b>Sellos con mas plata inmovilizada (stock)</b>", body))
        d0 = [["Sello", "Titulos", "Unidades", "Inversion"]]
        for s in tsi:
            d0.append([(s["sello"] or "-")[:34], str(s["titulos"]), str(s["unidades"]), _money(s["inversion_uyu"])])
        t = Table(d0, colWidths=[90*mm, 24*mm, 26*mm, 34*mm])
        t.setStyle(_ts_tabla())
        E.append(t)
        E.append(Spacer(1, 6))
    tsr = mets["top_sellos_rotacion"]
    if tsr:
        E.append(Paragraph("<b>Sellos que mas rotan (vendidos)</b>", body))
        d0 = [["Sello", "Titulos vend.", "Unidades", "Facturado"]]
        for s in tsr:
            d0.append([(s["sello"] or "-")[:34], str(s["titulos_vendidos"]), str(s["unidades_vendidas"]), _money(s["facturado_uyu"])])
        t = Table(d0, colWidths=[90*mm, 24*mm, 26*mm, 34*mm])
        t.setStyle(_ts_tabla())
        E.append(t)
        E.append(Spacer(1, 6))
    tdr = mets["top_discos_rentables"]
    if tdr:
        E.append(Paragraph("<b>Discos vendidos mas rentables</b>", body))
        d0 = [["Disco", "Sello", "Precio", "Ganancia", "Margen"]]
        for d in tdr:
            nombre = ("%s - %s" % (d["artista"] or "", d["titulo"] or ""))[:42]
            d0.append([nombre, (d["sello"] or "-")[:18], _money(d["precio_uyu"]), _money(d["ganancia_uyu"]), "%.0f%%" % (d["margen_pct"] or 0)])
        t = Table(d0, colWidths=[74*mm, 34*mm, 22*mm, 24*mm, 20*mm])
        t.setStyle(_ts_tabla())
        E.append(t)

    # Pedidos
    if pedidos:
        E.append(Paragraph("Pedidos (compras de stock)", h2))
        d0 = [["Numero", "Fecha", "Costo", "Recaudado", "Resultado", "Rotacion"]]
        for p in pedidos[:14]:
            def _g(a):
                try:
                    v = getattr(p, a)
                    return v if v is not None else 0
                except Exception:
                    return 0
            d0.append([(p.numero or "-")[:16], p.fecha.strftime("%d/%m/%y") if p.fecha else "-",
                       _money(_g("costo_total_uyu")), _money(_g("recaudado_uyu")), _money(_g("resultado_uyu")),
                       "%.0f%%" % _g("rotacion_pct")])
        t = Table(d0, colWidths=[36*mm, 22*mm, 30*mm, 30*mm, 30*mm, 26*mm])
        t.setStyle(_ts_tabla())
        E.append(t)

    # Dinero por cobrar
    E.append(Paragraph("Dinero por cobrar (reservados)", h2))
    if por_cobrar["titulos"]:
        E.append(Paragraph("Tenes <b>%d disco(s)</b> reservados sin cobrar todavia, por un total de <b>%s</b>."
                           % (por_cobrar["titulos"], _money(por_cobrar["total_uyu"])), body))
    else:
        E.append(Paragraph("No hay discos reservados pendientes de cobro.", body))

    # Stock que no rota
    E.append(Paragraph("Stock que no rota", h2))
    if frenado["frenados_count"] > 0:
        E.append(Paragraph("Tenes <b>%d disco(s)</b> parados hace mas de %d dias, con <b>%s</b> quietos. "
                           "Conviene empujarlos o rematarlos." % (frenado["frenados_count"], frenado["umbral"],
                           _money(frenado["plata_frenada"])), body))
    else:
        E.append(Paragraph("Ningun disco pasa los %d dias sin venderse. El stock esta rotando bien." % frenado["umbral"], body))
    filas = frenado["filas"][:8]
    if filas:
        data = [["Dias", "Disco", "Sello", "Costo"]]
        for f in filas:
            d = f["disc"]
            nombre = ("%s - %s" % (d.artista or "", d.titulo or ""))[:46]
            data.append([str(f["dias"]) if f["dias"] is not None else "-", nombre, (d.sello or "-")[:18], _money(f["costo"])])
        t = Table(data, colWidths=[16*mm, 96*mm, 34*mm, 28*mm])
        ts = [("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.5), ("BACKGROUND", (0, 0), (-1, 0), BLACK), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
              ("FONT", (0, 1), (-1, -1), "Helvetica", 8.5), ("ALIGN", (0, 0), (0, -1), "CENTER"), ("ALIGN", (3, 0), (3, -1), "RIGHT"),
              ("TOPPADDING", (0, 0), (-1, -1), 4.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
              ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#e3e2dc")), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#faf9f6")])]
        for i, f in enumerate(filas, start=1):
            if f["dias"] is not None and f["dias"] >= frenado["umbral"]:
                ts.append(("TEXTCOLOR", (0, i), (0, i), RED))
        t.setStyle(TableStyle(ts))
        E.append(t)

    # Chequeo de datos
    E.append(Paragraph("Chequeo de datos", h2))
    if hay_problemas:
        E.append(Paragraph("Se encontraron cosas para revisar:", body))
    else:
        E.append(Paragraph("<font color='#0f7a53'><b>Los datos estan limpios.</b></font> No aparecieron inconsistencias:", body))
    data = [["Chequeo", "Resultado"]]
    for nombre, n in chequeos:
        data.append([nombre, ("OK - ninguno" if n == 0 else "%d para revisar" % n)])
    t = Table(data, colWidths=[126*mm, 48*mm])
    ts = [("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9), ("BACKGROUND", (0, 0), (-1, 0), BLACK), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
          ("FONT", (0, 1), (-1, -1), "Helvetica", 9), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#faf9f6")]),
          ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5), ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#e3e2dc"))]
    for i, (nombre, n) in enumerate(chequeos, start=1):
        ts.append(("TEXTCOLOR", (1, i), (1, i), GREEN if n == 0 else RED))
    t.setStyle(TableStyle(ts))
    E.append(t)

    E.append(Spacer(1, 10))
    E.append(HRFlowable(width="100%", thickness=0.5, color=GREY))
    E.append(Paragraph("Arcano Record Store &middot; Montevideo &middot; numeros en pesos uruguayos (UYU).", small))

    doc.build(E)
    out.seek(0)
    return out
