"""Calculo de metricas para el dashboard de resumen.

Todas las funciones reciben la sesion de db y devuelven dicts/lists listos
para renderizar.
"""
from datetime import datetime, timedelta
from sqlalchemy import func, case

from models import db, Disc, StatusHistory, Egreso, now_uy, today_uy


def _costo_negocio(d) -> float:
    """Costo del disco PARA EL NEGOCIO: 0 si es de la coleccion PERSONAL de Facundo
    (su venta es un aporte, no le costo al negocio), si no `costo_final_uyu`. Es el
    MISMO criterio que usa Contabilidad; se usa en TODAS las ganancias/margenes para
    que los numeros coincidan entre paneles (antes varias restaban el costo del
    personal y no cuadraban con el hero de Contabilidad)."""
    return 0 if (getattr(d, "origen", "arcano") == "personal") else d.costo_real_uyu


def _month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    """Devuelve (inicio, fin) del mes dado."""
    inicio = datetime(year, month, 1)
    if month == 12:
        fin = datetime(year + 1, 1, 1)
    else:
        fin = datetime(year, month + 1, 1)
    return inicio, fin


def contar_ventas(discs) -> int:
    """Cuenta TRANSACCIONES (no unidades): los discos con el mismo `venta_grupo`
    son una sola venta; los sueltos (sin grupo) cuentan de a uno."""
    grupos = set()
    sueltos = 0
    for d in discs:
        g = getattr(d, "venta_grupo", None)
        if g:
            grupos.add(g)
        else:
            sueltos += 1
    return len(grupos) + sueltos


def _rango_ventas(periodo: str, desde: str = None, hasta: str = None):
    """(inicio, fin) como date para el panel de Ventas. Suma 'hoy' y '7dias' a los
    periodos de _rango_periodo (mes_actual/mes_pasado/anio/todo/custom)."""
    from datetime import date, timedelta
    hoy = today_uy()
    if periodo == "hoy":
        return hoy, hoy + timedelta(days=1)
    if periodo == "7dias":
        return hoy - timedelta(days=6), hoy + timedelta(days=1)
    return _rango_periodo(periodo, desde, hasta)


def kpis_de_discos(discs) -> dict:
    """KPIs de una lista de discos vendidos: ventas (transacciones), unidades,
    facturado, ganancia, margen % y ticket promedio ($/venta)."""
    unidades = sum(d.copias_facturadas for d in discs)
    facturado = sum((d.precio_mostrar or d.precio_uyu or 0) * d.copias_facturadas for d in discs)

    # Ganancia con el MISMO criterio que Contabilidad: el costo de los discos de tu
    # coleccion personal es 0 (no le costaron al negocio). Antes usaba d.ganancia_uyu,
    # que resta el costo tambien a los personales -> no coincidia con Contabilidad.
    def _gan(d):
        return (d.precio_mostrar or d.precio_uyu or 0) - _costo_negocio(d)
    ganancia = sum(_gan(d) * d.copias_facturadas for d in discs)
    ventas = contar_ventas(discs)
    return {
        "ventas": ventas,
        "unidades": unidades,
        "facturado": round(facturado, 2),
        "ganancia": round(ganancia, 2),
        "margen": round(ganancia / facturado * 100, 1) if facturado else 0,
        "ticket": round(facturado / ventas) if ventas else 0,
    }


def kpis_principales() -> dict:
    """KPIs principales del negocio."""

    # ---- Stock disponible ----
    # La Mystery Box queda AFUERA: no es inventario real (no tiene costo ni stock
    # fisico), asi que no debe sumar ni al stock ni al valor/ganancia potencial.
    disponibles = (Disc.query.filter_by(estado="Disponible")
                   .filter(Disc.es_mystery_box.isnot(True)).all())
    stock_disponible_unidades = sum(d.stock or 0 for d in disponibles)
    # Inversion inmovilizada = plata que ARCANO puso en stock. Los discos de la
    # coleccion personal no le costaron al negocio (aporte), asi que cuentan 0.
    # Mismo criterio por origen que la Ganancia real (antes sumaba tambien los
    # personales y daba una "inversion" distinta al stock no vendido de la misma pagina).
    # Mismo conjunto que el "costo de stock no vendido" de la Ganancia real y la
    # "Mercaderia" de la Caja (v5.79): todo lo que se pago y no se vendio todavia
    # (Disponible + Reservado + Apartado + pre-orders/encargos ya pedidos). Antes
    # contaba solo los Disponibles y la misma pagina mostraba dos "stocks" distintos.
    _sin_vender = (
        Disc.query
        .filter(
            Disc.estado.in_(["Disponible", "Reservado", "Apartado para mi"])
            | ((Disc.estado.in_(["Pre-order", "Encargo"])) & (Disc.pedido_id.isnot(None)))
        )
        .filter(Disc.es_mystery_box.isnot(True))
        .all()
    )
    inversion_uyu = sum(
        _costo_negocio(d) * (d.stock or 1) for d in _sin_vender
    )
    valor_venta_uyu = sum(
        (d.precio_uyu or 0) * (d.stock or 1) for d in disponibles
    )
    ganancia_potencial_uyu = sum(
        ((d.precio_mostrar or d.precio_uyu or 0) - _costo_negocio(d)) * (d.stock or 1)
        for d in disponibles
    )

    # ---- Ventas historicas ----
    vendidos = Disc.query.filter_by(estado="Vendido").all()
    # Cantidad total de copias vendidas (suma de copias_vendidas de cada titulo)
    ventas_total_unidades = sum(d.copias_facturadas for d in vendidos)
    # Facturado: precio x cantidad de copias vendidas de cada disco
    facturado_uyu = sum(
        (d.precio_mostrar or d.precio_uyu or 0) * d.copias_facturadas for d in vendidos
    )
    costo_vendido_uyu = sum(
        _costo_negocio(d) * d.copias_facturadas for d in vendidos
    )
    ganancia_realizada_uyu = facturado_uyu - costo_vendido_uyu

    # ---- Margen promedio ----
    # Promedio de margen sobre los discos disponibles + vendidos (los que tienen precio).
    # Mismo criterio de costo por origen que Contabilidad (personal = 0).
    discos_con_precio = [
        d for d in disponibles + vendidos if (d.precio_uyu or 0) > 0
    ]
    def _margen(d):
        p = (d.precio_mostrar or d.precio_uyu or 0)
        return ((p - _costo_negocio(d)) / p * 100) if p else 0.0
    margenes = [_margen(d) for d in discos_con_precio]
    margen_promedio = sum(margenes) / len(margenes) if margenes else 0.0

    return {
        "stock_disponible_unidades": stock_disponible_unidades,
        "stock_disponible_titulos": len(disponibles),
        "inversion_uyu": round(inversion_uyu, 2),
        "valor_venta_uyu": round(valor_venta_uyu, 2),
        "ganancia_potencial_uyu": round(ganancia_potencial_uyu, 2),
        "ventas_total_unidades": ventas_total_unidades,
        "ventas_total_titulos": len(vendidos),
        "facturado_uyu": round(facturado_uyu, 2),
        "ganancia_realizada_uyu": round(ganancia_realizada_uyu, 2),
        "margen_promedio": round(margen_promedio, 1),
    }


def kpis_mes_actual() -> dict:
    """Ventas del mes en curso (segun fecha_venta)."""
    hoy = now_uy()
    inicio, fin = _month_bounds(hoy.year, hoy.month)

    vendidos_mes = (
        Disc.query
        .filter(Disc.estado == "Vendido")
        .filter(Disc.fecha_venta >= inicio)
        .filter(Disc.fecha_venta < fin)
        .all()
    )

    unidades = sum(d.copias_facturadas for d in vendidos_mes)
    facturado = sum((d.precio_mostrar or d.precio_uyu or 0) * d.copias_facturadas for d in vendidos_mes)
    costo = sum(_costo_negocio(d) * d.copias_facturadas for d in vendidos_mes)
    ganancia = facturado - costo

    return {
        "mes_label": "%s %d" % (["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto",
                                 "Setiembre", "Octubre", "Noviembre", "Diciembre"][hoy.month - 1], hoy.year),
        "mes_inicio": inicio.strftime("%d/%m/%Y"),
        "ventas_count": contar_ventas(vendidos_mes),
        "unidades": unidades,
        "facturado_uyu": round(facturado, 2),
        "ganancia_uyu": round(ganancia, 2),
    }


def top_sellos_inversion(limit: int = 5) -> list[dict]:
    """Top sellos por inversion inmovilizada (stock disponible)."""
    # Con el costo REAL (incluye la diferencia de cambio del pedido), igual que el
    # resto de la contabilidad.
    acum = {}
    for d in (Disc.query.filter(Disc.estado == "Disponible")
              .filter(Disc.sello.isnot(None)).filter(Disc.sello != "").all()):
        a = acum.setdefault(d.sello, {"sello": d.sello, "titulos": 0, "unidades": 0, "inversion_uyu": 0.0})
        a["titulos"] += 1
        a["unidades"] += int(d.stock or 0)
        a["inversion_uyu"] += d.costo_real_uyu * (d.stock or 0)
    filas = sorted(acum.values(), key=lambda x: x["inversion_uyu"], reverse=True)[:limit]
    for f in filas:
        f["inversion_uyu"] = round(f["inversion_uyu"], 2)
    return filas


def top_sellos_rotacion(limit: int = 5) -> list[dict]:
    """Top sellos por rotacion (cantidad vendida). Se calcula en Python para usar las
    mismas properties que el resto del sistema (copias_facturadas y precio_mostrar, que
    respetan precio de oferta y el minimo de 1 copia), en vez de columnas crudas."""
    vendidos = (
        Disc.query
        .filter(Disc.estado == "Vendido")
        .filter(Disc.sello.isnot(None))
        .filter(Disc.sello != "")
        .all()
    )
    acc = {}
    for d in vendidos:
        a = acc.setdefault(d.sello, {"titulos": 0, "unidades": 0, "facturado": 0.0})
        a["titulos"] += 1
        a["unidades"] += d.copias_facturadas
        a["facturado"] += (d.precio_mostrar or d.precio_uyu or 0) * d.copias_facturadas
    out = [
        {
            "sello": s,
            "titulos_vendidos": v["titulos"],
            "unidades_vendidas": int(v["unidades"]),
            "facturado_uyu": round(v["facturado"], 2),
        }
        for s, v in acc.items()
    ]
    return sorted(out, key=lambda x: x["titulos_vendidos"], reverse=True)[:limit]


def top_discos_rentables(limit: int = 5) -> list[dict]:
    """Top discos vendidos por margen."""
    vendidos = (
        Disc.query
        .filter_by(estado="Vendido")
        .filter(Disc.precio_uyu > 0)
        .all()
    )
    # Ordenar por margen_pct (propiedad calculada en el modelo)
    vendidos_sorted = sorted(vendidos, key=lambda d: d.margen_pct, reverse=True)
    return [
        {
            "id": d.id,
            "artista": d.artista,
            "titulo": d.titulo,
            "sello": d.sello,
            "precio_uyu": d.precio_uyu,
            "ganancia_uyu": d.ganancia_uyu,
            "margen_pct": d.margen_pct,
            "fecha_venta": d.fecha_venta,
        }
        for d in vendidos_sorted[:limit]
    ]


def ventas_ultimos_6_meses() -> list[dict]:
    """Lista de los ultimos 6 meses con cantidad vendida y facturado.

    Resultado en orden cronologico (mes mas antiguo primero, mas reciente al final).
    """
    hoy = now_uy()
    # Calcular el mes de inicio (5 meses atras)
    year = hoy.year
    month = hoy.month
    meses = []
    for _ in range(6):
        meses.append((year, month))
        # Retroceder un mes
        if month == 1:
            month = 12
            year -= 1
        else:
            month -= 1
    meses.reverse()  # ahora va del mas antiguo al mas reciente

    nombres_es = {
        1: "Ene", 2: "Feb", 3: "Mar", 4: "Abr", 5: "May", 6: "Jun",
        7: "Jul", 8: "Ago", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dic",
    }

    salida = []
    for (y, m) in meses:
        inicio, fin = _month_bounds(y, m)
        vendidos = (
            Disc.query
            .filter(Disc.estado == "Vendido")
            .filter(Disc.fecha_venta >= inicio)
            .filter(Disc.fecha_venta < fin)
            .all()
        )
        unidades = sum(d.copias_facturadas for d in vendidos)
        facturado = sum((d.precio_mostrar or d.precio_uyu or 0) * d.copias_facturadas for d in vendidos)
        ganancia = facturado - sum(_costo_negocio(d) * d.copias_facturadas for d in vendidos)

        salida.append({
            "year": y,
            "month": m,
            "label": f"{nombres_es[m]} {str(y)[-2:]}",
            "ventas": contar_ventas(vendidos),
            "unidades": unidades,
            "facturado_uyu": round(facturado, 2),
            "ganancia_uyu": round(ganancia, 2),
        })

    return salida


def get_all_metrics() -> dict:
    """Calcula todas las metricas de una vez."""
    return {
        "kpis": kpis_principales(),
        "mes_actual": kpis_mes_actual(),
        "top_sellos_inversion": top_sellos_inversion(),
        "top_sellos_rotacion": top_sellos_rotacion(),
        "top_discos_rentables": top_discos_rentables(),
        "ventas_6m": ventas_ultimos_6_meses(),
    }


# ---------------------------------------------------------------------------
# Contabilidad: egresos y ganancia real
# ---------------------------------------------------------------------------

def _rango_periodo(periodo: str, desde: str = None, hasta: str = None):
    """Devuelve (inicio, fin) como date segun el periodo pedido.
    periodo: 'mes_actual', 'mes_pasado', 'anio', 'todo', 'custom'.
    Para 'custom' se usan desde/hasta (formato YYYY-MM-DD)."""
    from datetime import date, datetime as _dt, timedelta
    hoy = today_uy()
    if periodo == "custom" and desde and hasta:
        try:
            inicio = _dt.strptime(desde, "%Y-%m-%d").date()
            # fin es exclusivo, sumamos 1 dia para incluir el dia 'hasta'
            fin = _dt.strptime(hasta, "%Y-%m-%d").date() + timedelta(days=1)
            return inicio, fin
        except (ValueError, TypeError):
            pass  # si falla, cae al default de abajo
    if periodo == "mes_actual":
        inicio = date(hoy.year, hoy.month, 1)
        fin = date(hoy.year + 1, 1, 1) if hoy.month == 12 else date(hoy.year, hoy.month + 1, 1)
    elif periodo == "mes_pasado":
        if hoy.month == 1:
            inicio = date(hoy.year - 1, 12, 1)
            fin = date(hoy.year, 1, 1)
        else:
            inicio = date(hoy.year, hoy.month - 1, 1)
            fin = date(hoy.year, hoy.month, 1)
    elif periodo == "anio":
        inicio = date(hoy.year, 1, 1)
        fin = date(hoy.year + 1, 1, 1)
    else:  # todo
        # "Todo" tiene que ser TODO: sin tope superior real. Antes cortaba a fin del
        # anio corriente, asi que una venta con fecha futura (tipico error de tipeo:
        # 2027 en vez de 2026) desaparecia de Contabilidad pero SI sumaba en los KPIs
        # historicos, y las dos pantallas se contradecian.
        inicio = date(2000, 1, 1)
        fin = date(2100, 1, 1)
    return inicio, fin


def contabilidad(periodo: str = "mes_actual", desde: str = None, hasta: str = None) -> dict:
    """Resumen contable de un periodo: ingresos, egresos y ganancia real."""
    from datetime import datetime as _dt
    inicio, fin = _rango_periodo(periodo, desde, hasta)
    inicio_dt = _dt(inicio.year, inicio.month, inicio.day)
    fin_dt = _dt(fin.year, fin.month, fin.day)

    # --- INGRESOS: discos vendidos en el periodo ---
    vendidos = (
        Disc.query
        .filter(Disc.estado == "Vendido")
        .filter(Disc.fecha_venta >= inicio_dt)
        .filter(Disc.fecha_venta < fin_dt)
        .all()
    )
    # Separar ventas por origen: arcano (inversion del negocio) vs personal (aporte)
    vendidos_arcano = [d for d in vendidos if (d.origen or "arcano") != "personal"]
    vendidos_personal = [d for d in vendidos if (d.origen or "arcano") == "personal"]

    ingresos_uyu = sum((d.precio_mostrar or d.precio_uyu or 0) * d.copias_facturadas for d in vendidos)
    ingresos_personal_uyu = sum((d.precio_mostrar or d.precio_uyu or 0) * d.copias_facturadas for d in vendidos_personal)
    ingresos_arcano_uyu = ingresos_uyu - ingresos_personal_uyu

    # Costo de discos vendidos: SOLO los de arcano (los personales no costaron al negocio)
    costo_discos_uyu = sum(d.costo_real_uyu * d.copias_facturadas for d in vendidos_arcano)
    unidades_vendidas = sum(d.copias_facturadas for d in vendidos)
    unidades_personal = sum(d.copias_facturadas for d in vendidos_personal)

    # --- COSTO DE STOCK NO VENDIDO (solo arcano) ---
    # Plata ya gastada en discos que todavia NO se vendieron: TODO lo que no esta Vendido
    # (Disponible + Reservado + Apartado para mi), MAS los Pre-order con pedido asignado
    # (ya se encargaron/pagaron a Deejay). Un Reservado SIGUE siendo stock sin vender: la
    # plata sigue metida ahi hasta que la venta se concreta (por eso reservar no debe subir
    # la ganancia). Los Encargo (disco que un cliente pidio) siguen la misma regla que el
    # Pre-order. Quedan afuera: los Pre-order/Encargo SIN pedido (todavia no hay plata puesta) y
    # los Descartados (perdida, se cuenta aparte).
    disponibles_arcano = (
        Disc.query
        .filter(
            Disc.estado.in_(["Disponible", "Reservado", "Apartado para mi"])
            | ((Disc.estado.in_(["Pre-order", "Encargo"])) & (Disc.pedido_id.isnot(None)))
        )
        .filter((Disc.origen != "personal") | (Disc.origen.is_(None)))
        .filter(Disc.es_mystery_box.isnot(True))   # la box no es stock real
        .all()
    )
    costo_stock_no_vendido_uyu = sum(d.costo_real_uyu * (d.stock or 1) for d in disponibles_arcano)

    # --- DESCARTADOS: perdida del periodo (decision de Facundo, 12/10/2026) ---
    # Un disco de Arcano que pasaste a Descartado (roto, perdido) es plata perdida: su
    # costo resta en el mes en que lo descartaste. Antes simplemente dejaba de contar
    # como stock y la ganancia SUBIA al descartarlo.
    descartados, perdida_descartados_uyu = _descartados_en_rango(inicio, fin)

    # --- EGRESOS: gastos operativos del periodo ---
    from models import CAT_RETIRO_PERSONAL
    egresos_all = (
        Egreso.query
        .filter(Egreso.fecha >= inicio)
        .filter(Egreso.fecha < fin)
        .all()
    )
    # Los retiros personales NO son un gasto operativo: son plata que Facundo saca para
    # si mismo (afecta el capital, no la ganancia del negocio). Se excluyen del gasto y
    # se muestran/suman aparte.
    egresos = [e for e in egresos_all if e.categoria != CAT_RETIRO_PERSONAL]
    retiros = [e for e in egresos_all if e.categoria == CAT_RETIRO_PERSONAL]
    egresos_uyu = sum(e.monto_uyu or 0 for e in egresos)
    retiros_personales_uyu = sum(e.monto_uyu or 0 for e in retiros)

    # Egresos agrupados por categoria (sin los retiros personales)
    por_categoria = {}
    for e in egresos:
        por_categoria[e.categoria] = por_categoria.get(e.categoria, 0) + (e.monto_uyu or 0)
    egresos_por_categoria = sorted(por_categoria.items(), key=lambda x: x[1], reverse=True)

    # --- GANANCIA ---
    # Ganancia bruta: solo sobre los discos de Arcano (los personales no restan costo
    # pero su ingreso ya esta contado; el margen "de Arcano" los excluye del costo).
    ganancia_bruta = ingresos_uyu - costo_discos_uyu       # margen de los discos vendidos
    # Ganancia real: descontar los gastos y (solo cuando tiene sentido) el stock no
    # vendido de arcano. El stock es una FOTO "a hoy": restarlo en periodos PASADOS
    # (mes pasado, este ano, custom) mezcla ese stock actual con ingresos viejos y da
    # numeros muy negativos y confusos. Por eso el stock solo se resta en "Este mes" y
    # "Todo" (donde el corte incluye el hoy); en el resto la ganancia real es el flujo
    # operativo del periodo. El costo del stock igual se devuelve y se muestra aparte.
    # El stock no vendido es una FOTO "a hoy": solo se resta si el periodo LLEGA hasta hoy
    # (lo incluye). Eso cubre "Este mes", "Este ano" y "Todo" (y custom que termine hoy o
    # despues), porque su rango termina despues de hoy. Un periodo que cerro en el pasado
    # (mes pasado, custom viejo) NO lo resta. Antes usaba una lista fija que dejaba "Este
    # ano" afuera por error, dando distinto que "Todo" aunque para Arcano son lo mismo.
    incluir_stock = fin > today_uy()
    ganancia_real = (ganancia_bruta - egresos_uyu - perdida_descartados_uyu
                     - (costo_stock_no_vendido_uyu if incluir_stock else 0))

    # --- COMPARACION con periodo anterior ---
    comparacion = None
    ant_inicio, ant_fin = _periodo_anterior(periodo)
    if ant_inicio and ant_fin:
        ant = _resumen_rango(ant_inicio, ant_fin)
        # Comparamos el RESULTADO OPERATIVO del periodo (ingresos - costo de los
        # vendidos de arcano - egresos), SIN el stock no vendido. El stock es una
        # foto "a hoy" (no pertenece a un periodo pasado), asi que dejarlo afuera
        # de ambos lados hace la comparacion coherente. _resumen_rango usa esa
        # misma formula, asi que aca comparamos manzanas con manzanas.
        flujo_actual = ganancia_bruta - egresos_uyu - perdida_descartados_uyu
        prev = ant["ganancia_real_uyu"]
        if prev != 0:
            var_pct = round((flujo_actual - prev) / abs(prev) * 100, 1)
        else:
            var_pct = None
        comparacion = {
            "ingresos_ant": ant["ingresos_uyu"],
            "egresos_ant": ant["egresos_uyu"],
            "ganancia_real_ant": prev,
            "var_ganancia_pct": var_pct,
            "mejoro": flujo_actual >= prev,
        }

    return {
        "periodo": periodo,
        "inicio": inicio.strftime("%d/%m/%Y"),
        # "Todo" usa un tope tecnico lejano (ano 2100) para no perder ventas con fecha
        # futura; para MOSTRAR el rango se usa hoy, que es lo que el usuario espera leer.
        "fin": (today_uy() if fin.year >= 2100 else fin).strftime("%d/%m/%Y"),
        "ingresos_uyu": round(ingresos_uyu, 2),
        "costo_discos_uyu": round(costo_discos_uyu, 2),
        "unidades_vendidas": unidades_vendidas,
        "titulos_vendidos": len(vendidos),
        "egresos_uyu": round(egresos_uyu, 2),
        "egresos_por_categoria": [(c, round(m, 2)) for c, m in egresos_por_categoria],
        "egresos_count": len(egresos),
        "retiros_personales_uyu": round(retiros_personales_uyu, 2),
        "retiros_count": len(retiros),
        "ganancia_bruta_uyu": round(ganancia_bruta, 2),
        "ganancia_real_uyu": round(ganancia_real, 2),
        "comparacion": comparacion,
        "ingresos_arcano_uyu": round(ingresos_arcano_uyu, 2),
        "ingresos_personal_uyu": round(ingresos_personal_uyu, 2),
        "unidades_personal": unidades_personal,
        "titulos_personal": len(vendidos_personal),
        "costo_stock_no_vendido_uyu": round(costo_stock_no_vendido_uyu, 2),
        "stock_no_vendido_titulos": len(disponibles_arcano),
        "stock_en_ganancia": incluir_stock,
        "perdida_descartados_uyu": round(perdida_descartados_uyu, 2),
        "descartados_count": len(descartados),
    }


def _descartados_en_rango(inicio, fin):
    """Discos de Arcano (no personales, no la Mystery Box) que estan Descartados y cuya
    fecha de descarte (el ultimo paso a "Descartado" en su historial) cae en el rango.
    Si no hay fecha registrada, solo cuentan en "Todo" (rango que arranca en el 2000 o
    antes). Devuelve (lista, perdida_uyu)."""
    from models import StatusHistory
    from datetime import datetime as _dt
    discs = (
        Disc.query.filter(Disc.estado == "Descartado")
        .filter((Disc.origen != "personal") | (Disc.origen.is_(None)))
        .filter(Disc.es_mystery_box.isnot(True))
        .all()
    )
    if not discs:
        return [], 0.0
    ids = [d.id for d in discs]
    fechas = {}
    for h in (StatusHistory.query.filter(StatusHistory.disc_id.in_(ids))
              .filter(StatusHistory.estado == "Descartado").all()):
        if h.fecha and (h.disc_id not in fechas or h.fecha > fechas[h.disc_id]):
            fechas[h.disc_id] = h.fecha
    ini_dt = _dt(inicio.year, inicio.month, inicio.day)
    fin_dt = _dt(fin.year, fin.month, fin.day)
    es_todo = inicio.year <= 2000
    en_rango = []
    for d in discs:
        f = fechas.get(d.id)
        if (f is not None and ini_dt <= f < fin_dt) or (f is None and es_todo):
            en_rango.append(d)
    perdida = sum(d.costo_real_uyu * (d.stock or 1) for d in en_rango)
    return en_rango, perdida


def _periodo_anterior(periodo: str):
    """Devuelve (inicio, fin) del periodo INMEDIATAMENTE anterior al dado."""
    from datetime import date
    hoy = today_uy()
    if periodo == "mes_actual":
        # anterior = mes pasado
        if hoy.month == 1:
            return date(hoy.year - 1, 12, 1), date(hoy.year, 1, 1)
        return date(hoy.year, hoy.month - 1, 1), date(hoy.year, hoy.month, 1)
    elif periodo == "mes_pasado":
        # anterior = ante-mes
        m = hoy.month - 2
        y = hoy.year
        if m <= 0:
            m += 12
            y -= 1
        fin_m = m + 1
        fin_y = y
        if fin_m > 12:
            fin_m = 1
            fin_y += 1
        return date(y, m, 1), date(fin_y, fin_m, 1)
    elif periodo == "anio":
        return date(hoy.year - 1, 1, 1), date(hoy.year, 1, 1)
    return None, None


def _resumen_rango(inicio, fin):
    """Resultado OPERATIVO de un rango de fechas: ingresos de las ventas menos el
    costo de los discos de ARCANO vendidos (los personales no le costaron nada al
    negocio) menos los gastos del periodo. NO incluye el stock no vendido (que es
    una foto 'a hoy', no de un periodo pasado). Misma formula que el flujo del
    periodo actual, para que la comparacion periodo-contra-periodo sea coherente."""
    from datetime import datetime as _dt
    inicio_dt = _dt(inicio.year, inicio.month, inicio.day)
    fin_dt = _dt(fin.year, fin.month, fin.day)
    vendidos = (
        Disc.query.filter(Disc.estado == "Vendido")
        .filter(Disc.fecha_venta >= inicio_dt).filter(Disc.fecha_venta < fin_dt).all()
    )
    ingresos = sum((d.precio_mostrar or d.precio_uyu or 0) * d.copias_facturadas for d in vendidos)
    # Costo SOLO de los de arcano (excluye personales), igual que costo_discos_uyu.
    costo = sum(
        d.costo_real_uyu * d.copias_facturadas
        for d in vendidos if (d.origen or "arcano") != "personal"
    )
    # Los retiros personales NO son gasto del negocio (igual que en contabilidad()):
    # antes bajaban la ganancia en el grafico de 6 meses y en la comparacion.
    from models import CAT_RETIRO_PERSONAL
    egresos = (
        Egreso.query.filter(Egreso.fecha >= inicio).filter(Egreso.fecha < fin)
        .filter(Egreso.categoria != CAT_RETIRO_PERSONAL).all()
    )
    egresos_uyu = sum(e.monto_uyu or 0 for e in egresos)
    _desc, perdida_desc = _descartados_en_rango(inicio, fin)
    return {
        "ingresos_uyu": round(ingresos, 2),
        "ganancia_real_uyu": round(ingresos - costo - egresos_uyu - perdida_desc, 2),
        "egresos_uyu": round(egresos_uyu, 2),
    }


def dinero_por_cobrar() -> dict:
    """Discos reservados: lo que todavia FALTA cobrar (el precio menos la sena que el
    cliente ya pago por adelantado). Si no hubo sena, es el precio completo."""
    reservados = Disc.query.filter(Disc.estado == "Reservado").all()
    total = sum(d.precio_falta_uyu for d in reservados)
    return {
        "titulos": len(reservados),
        "total_uyu": round(total, 2),
        "discos": reservados,
    }


def ingresos_vs_egresos_6m() -> list:
    """Plata que entro vs salio por mes (ultimos 6 meses), para el grafico.

    Egresos INCLUYE los pedidos (compras de stock) ademas de los gastos
    operativos: es una mirada de CAJA (cuanta plata se movio). El neto es
    ingresos - egresos. Distinto de la 'ganancia real' del hero, que es
    contable (el costo del pedido se cuenta al vender cada disco)."""
    from datetime import date
    from models import Pedido
    hoy = today_uy()
    nombres_es = ["", "Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
    y, m = hoy.year, hoy.month
    lista = []
    for _ in range(6):
        lista.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    lista.reverse()
    salida = []
    for (yy, mm) in lista:
        inicio = date(yy, mm, 1)
        fin = date(yy + 1, 1, 1) if mm == 12 else date(yy, mm + 1, 1)
        r = _resumen_rango(inicio, fin)
        # Pedidos (compras de stock) con fecha en el mes = plata que salio
        peds = Pedido.query.filter(Pedido.fecha >= inicio, Pedido.fecha < fin).all()
        costo_pedidos = sum(p.costo_total_uyu for p in peds)
        egresos_total = (r["egresos_uyu"] or 0) + costo_pedidos
        neto = (r["ingresos_uyu"] or 0) - egresos_total
        salida.append({
            "label": f"{nombres_es[mm]} {str(yy)[-2:]}",
            "ingresos_uyu": r["ingresos_uyu"],
            "egresos_uyu": round(egresos_total, 2),
            "egresos_opex_uyu": r["egresos_uyu"],
            "pedidos_uyu": round(costo_pedidos, 2),
            "neto_uyu": round(neto, 2),
            "ganancia_real_uyu": r["ganancia_real_uyu"],
        })
    return salida


def stock_frenado(dias_umbral: int = 45) -> dict:
    """Discos de Arcano (no personales) que siguen Disponibles, ordenados por
    cuanto tiempo llevan en stock (los mas viejos primero). Sirve para ver que
    plata esta quieta y que conviene rematar o empujar."""
    from datetime import date
    hoy = today_uy()
    discos = (
        Disc.query
        .filter(Disc.estado == "Disponible")
        .filter((Disc.origen != "personal") | (Disc.origen.is_(None)))
        .filter(Disc.es_mystery_box.isnot(True))   # la box no es stock real
        .all()
    )
    filas = []
    for d in discos:
        fi = d.fecha_ingreso
        if fi is not None:
            fecha = fi.date() if hasattr(fi, "date") else fi
            dias = (hoy - fecha).days
        else:
            dias = None
        filas.append({
            "disc": d,
            "dias": dias,
            "costo": round(d.costo_real_uyu * (d.stock or 1), 2),
            "precio": round(d.precio_mostrar or d.precio_uyu or 0, 2),
        })
    # Los mas viejos primero; los sin fecha al final
    filas.sort(key=lambda x: (x["dias"] is None, -(x["dias"] or 0)))
    frenados = [f for f in filas if (f["dias"] or 0) >= dias_umbral]
    return {
        "filas": filas,
        "frenados_count": len(frenados),
        "plata_frenada": round(sum(f["costo"] for f in frenados), 2),
        "umbral": dias_umbral,
        "total_disponibles": len(filas),
    }


def recuperacion_inversion() -> dict:
    """Cuanto se invirtio en pedidos vs cuanto se recupero vendiendo esos discos.

    Devuelve DOS miradas:
      1) Mercaderia sola: costo de los pedidos vs lo recaudado por esos discos.
      2) Con gastos operativos: costo de pedidos + todos los gastos (publicidad,
         hosting, etc.) vs TODAS las ventas de discos. Es el punto de equilibrio
         real del negocio (cuando esta linea llega al 100%, empezas a ganar).
    """
    from models import Pedido
    peds = Pedido.query.all()
    invertido = sum(p.costo_total_uyu for p in peds)
    recuperado = sum(p.recaudado_uyu for p in peds)
    discos_total = sum(p.discos_asignados for p in peds)
    discos_vendidos = sum(p.discos_vendidos for p in peds)
    pct = round(recuperado / invertido * 100, 1) if invertido > 0 else 0.0

    # Linea 2: punto de equilibrio REAL del negocio. Usa EXACTAMENTE los mismos
    # numeros que la "Ganancia real" (contabilidad de 'todo') para que las dos nunca
    # se contradigan: lo puesto = costo de los discos de Arcano (vendidos + stock no
    # vendido) + TODOS los gastos operativos; lo recuperado = todas las ventas.
    # Asi: recuperado - puesto == ganancia real historica.
    conta = contabilidad("todo")
    ventas_tot = conta["ingresos_uyu"]
    costo_arcano = conta["costo_discos_uyu"] + conta["costo_stock_no_vendido_uyu"]
    gastos_tot = conta["egresos_uyu"]
    puesto = costo_arcano + gastos_tot
    resultado2 = round(ventas_tot - puesto, 2)          # = ganancia_real de 'todo'
    pct2 = round(ventas_tot / puesto * 100, 1) if puesto > 0 else 0.0

    return {
        "invertido": round(invertido, 2),
        "recuperado": round(recuperado, 2),
        "falta": round(max(0, invertido - recuperado), 2),
        "pct": min(pct, 100),
        "pct_real": pct,
        "discos_total": discos_total,
        "discos_vendidos": discos_vendidos,
        "discos_sin_vender": discos_total - discos_vendidos,
        "recuperado_total": recuperado >= invertido and invertido > 0,
        "pedidos_count": len(peds),
        # Linea 2: con gastos operativos (mismo calculo que la Ganancia real)
        "gastos_operativos": round(gastos_tot, 2),
        "costo_arcano": round(costo_arcano, 2),
        "invertido_con": round(puesto, 2),
        "recuperado_con": round(ventas_tot, 2),
        "falta_con": round(max(0, -resultado2), 2),
        "sobra_con": round(max(0, resultado2), 2),
        "pct_con": min(pct2, 100),
        "pct_con_real": pct2,
        "recuperado_total_con": resultado2 >= 0 and puesto > 0,
    }


def arcano_vs_personal_6m() -> list:
    """Por mes (ultimos 6): ventas de stock de Arcano vs ventas de tu coleccion
    personal. Muestra cuanto se sostiene el negocio por si solo."""
    from datetime import date
    hoy = today_uy()
    nombres_es = ["", "Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
    y, m = hoy.year, hoy.month
    lista = []
    for _ in range(6):
        lista.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    lista.reverse()
    salida = []
    for (yy, mm) in lista:
        inicio, fin = _month_bounds(yy, mm)
        vend = (
            Disc.query.filter(Disc.estado == "Vendido")
            .filter(Disc.fecha_venta >= inicio).filter(Disc.fecha_venta < fin).all()
        )
        arc = sum((d.precio_mostrar or d.precio_uyu or 0) * d.copias_facturadas
                  for d in vend if (d.origen or "arcano") != "personal")
        per = sum((d.precio_mostrar or d.precio_uyu or 0) * d.copias_facturadas
                  for d in vend if (d.origen or "arcano") == "personal")
        salida.append({
            "label": f"{nombres_es[mm]} {str(yy)[-2:]}",
            "arcano": round(arc, 2),
            "personal": round(per, 2),
        })
    return salida
