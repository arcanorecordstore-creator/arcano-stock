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
                                TableStyle, HRFlowable)

import metrics
from models import Disc, Pedido, MovimientoCaja, Setting


def _money(n):
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        n = 0
    return "$ " + format(int(round(n)), ",d").replace(",", ".")


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


def generar_informe_pdf(periodo="mes_actual", desde=None, hasta=None):
    conta = metrics.contabilidad(periodo, desde, hasta)
    recup = metrics.recuperacion_inversion()
    frenado = metrics.stock_frenado()

    movs = MovimientoCaja.query.all()
    try:
        si = float(Setting.get("caja_saldo_inicial") or 0)
    except (TypeError, ValueError):
        si = 0.0
    ing_c = sum(m.monto_uyu or 0 for m in movs if m.tipo == "ingreso")
    egr_c = sum(m.monto_uyu or 0 for m in movs if m.tipo == "egreso")
    saldo = si + ing_c - egr_c

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
    E.append(Paragraph("Se vendieron <b>%d unidades</b> (%d titulos) en el periodo. La ganancia real descuenta el costo "
                       "de los discos de Arcano (vendidos y en stock) y los gastos operativos."
                       % (conta["unidades_vendidas"], conta["titulos_vendidos"]), small))

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
