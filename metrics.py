"""Calculo de metricas para el dashboard de resumen.

Todas las funciones reciben la sesion de db y devuelven dicts/lists listos
para renderizar.
"""
from datetime import datetime, timedelta
from sqlalchemy import func, case

from models import db, Disc, StatusHistory, Egreso


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


def kpis_principales() -> dict:
    """KPIs principales del negocio."""

    # ---- Stock disponible ----
    disponibles = Disc.query.filter_by(estado="Disponible").all()
    stock_disponible_unidades = sum(d.stock or 0 for d in disponibles)
    inversion_uyu = sum(
        (d.costo_final_uyu or 0) * (d.stock or 1) for d in disponibles
    )
    valor_venta_uyu = sum(
        (d.precio_uyu or 0) * (d.stock or 1) for d in disponibles
    )
    ganancia_potencial_uyu = sum(
        (d.ganancia_uyu or 0) * (d.stock or 1) for d in disponibles
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
        (d.costo_final_uyu or 0) * d.copias_facturadas for d in vendidos
    )
    ganancia_realizada_uyu = facturado_uyu - costo_vendido_uyu

    # ---- Margen promedio ----
    # Promedio de margen sobre los discos disponibles + vendidos (los que tienen precio)
    discos_con_precio = [
        d for d in disponibles + vendidos if (d.precio_uyu or 0) > 0
    ]
    margenes = [d.margen_pct for d in discos_con_precio]
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
    hoy = datetime.utcnow()
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
    costo = sum((d.costo_final_uyu or 0) * d.copias_facturadas for d in vendidos_mes)
    ganancia = facturado - costo

    return {
        "mes_label": hoy.strftime("%B %Y"),
        "mes_inicio": inicio.strftime("%d/%m/%Y"),
        "ventas_count": contar_ventas(vendidos_mes),
        "unidades": unidades,
        "facturado_uyu": round(facturado, 2),
        "ganancia_uyu": round(ganancia, 2),
    }


def top_sellos_inversion(limit: int = 5) -> list[dict]:
    """Top sellos por inversion inmovilizada (stock disponible)."""
    rows = (
        db.session.query(
            Disc.sello,
            func.count(Disc.id).label("titulos"),
            func.sum(Disc.stock).label("unidades"),
            func.sum(Disc.costo_final_uyu * Disc.stock).label("inversion"),
        )
        .filter(Disc.estado == "Disponible")
        .filter(Disc.sello.isnot(None))
        .filter(Disc.sello != "")
        .group_by(Disc.sello)
        .order_by(func.sum(Disc.costo_final_uyu * Disc.stock).desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "sello": r.sello,
            "titulos": r.titulos,
            "unidades": int(r.unidades or 0),
            "inversion_uyu": round(float(r.inversion or 0), 2),
        }
        for r in rows
    ]


def top_sellos_rotacion(limit: int = 5) -> list[dict]:
    """Top sellos por rotacion (cantidad vendida)."""
    rows = (
        db.session.query(
            Disc.sello,
            func.count(Disc.id).label("titulos"),
            func.sum(Disc.copias_vendidas).label("unidades"),
            func.sum(Disc.precio_uyu * Disc.copias_vendidas).label("facturado"),
        )
        .filter(Disc.estado == "Vendido")
        .filter(Disc.sello.isnot(None))
        .filter(Disc.sello != "")
        .group_by(Disc.sello)
        .order_by(func.count(Disc.id).desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "sello": r.sello,
            "titulos_vendidos": r.titulos,
            "unidades_vendidas": int(r.unidades or 0),
            "facturado_uyu": round(float(r.facturado or 0), 2),
        }
        for r in rows
    ]


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
    hoy = datetime.utcnow()
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
        ganancia = facturado - sum((d.costo_final_uyu or 0) * d.copias_facturadas for d in vendidos)

        salida.append({
            "year": y,
            "month": m,
            "label": f"{nombres_es[m]} {str(y)[-2:]}",
            "ventas": len(vendidos),
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
    hoy = date.today()
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
        inicio = date(2000, 1, 1)
        fin = date(hoy.year + 1, 1, 1)
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
    costo_discos_uyu = sum((d.costo_final_uyu or 0) * d.copias_facturadas for d in vendidos_arcano)
    unidades_vendidas = sum(d.copias_facturadas for d in vendidos)
    unidades_personal = sum(d.copias_facturadas for d in vendidos_personal)

    # --- COSTO DE STOCK NO VENDIDO (solo arcano) ---
    # Plata ya gastada en discos que todavia no se vendieron: los Disponibles, MAS los
    # Pre-order que tienen un pedido asignado (ya se encargaron/pagaron a Deejay). Los
    # Pre-order SIN pedido asignado NO cuentan: todavia no hay plata comprometida.
    disponibles_arcano = (
        Disc.query
        .filter(
            (Disc.estado == "Disponible")
            | ((Disc.estado == "Pre-order") & (Disc.pedido_id.isnot(None)))
        )
        .filter((Disc.origen != "personal") | (Disc.origen.is_(None)))
        .all()
    )
    costo_stock_no_vendido_uyu = sum((d.costo_final_uyu or 0) * (d.stock or 1) for d in disponibles_arcano)

    # --- EGRESOS: gastos operativos del periodo ---
    egresos = (
        Egreso.query
        .filter(Egreso.fecha >= inicio)
        .filter(Egreso.fecha < fin)
        .all()
    )
    egresos_uyu = sum(e.monto_uyu or 0 for e in egresos)

    # Egresos agrupados por categoria
    por_categoria = {}
    for e in egresos:
        por_categoria[e.categoria] = por_categoria.get(e.categoria, 0) + (e.monto_uyu or 0)
    egresos_por_categoria = sorted(por_categoria.items(), key=lambda x: x[1], reverse=True)

    # --- GANANCIA ---
    # Ganancia bruta: solo sobre los discos de Arcano (los personales no restan costo
    # pero su ingreso ya esta contado; el margen "de Arcano" los excluye del costo).
    ganancia_bruta = ingresos_uyu - costo_discos_uyu       # margen de los discos vendidos
    # Ganancia real: descontar tambien el stock no vendido de arcano y los gastos.
    ganancia_real = ganancia_bruta - costo_stock_no_vendido_uyu - egresos_uyu

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
        flujo_actual = ganancia_bruta - egresos_uyu
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
        "fin": (fin).strftime("%d/%m/%Y"),
        "ingresos_uyu": round(ingresos_uyu, 2),
        "costo_discos_uyu": round(costo_discos_uyu, 2),
        "unidades_vendidas": unidades_vendidas,
        "titulos_vendidos": len(vendidos),
        "egresos_uyu": round(egresos_uyu, 2),
        "egresos_por_categoria": [(c, round(m, 2)) for c, m in egresos_por_categoria],
        "egresos_count": len(egresos),
        "ganancia_bruta_uyu": round(ganancia_bruta, 2),
        "ganancia_real_uyu": round(ganancia_real, 2),
        "comparacion": comparacion,
        "ingresos_arcano_uyu": round(ingresos_arcano_uyu, 2),
        "ingresos_personal_uyu": round(ingresos_personal_uyu, 2),
        "unidades_personal": unidades_personal,
        "titulos_personal": len(vendidos_personal),
        "costo_stock_no_vendido_uyu": round(costo_stock_no_vendido_uyu, 2),
        "stock_no_vendido_titulos": len(disponibles_arcano),
    }


def _periodo_anterior(periodo: str):
    """Devuelve (inicio, fin) del periodo INMEDIATAMENTE anterior al dado."""
    from datetime import date
    hoy = date.today()
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
        (d.costo_final_uyu or 0) * d.copias_facturadas
        for d in vendidos if (d.origen or "arcano") != "personal"
    )
    egresos = (
        Egreso.query.filter(Egreso.fecha >= inicio).filter(Egreso.fecha < fin).all()
    )
    egresos_uyu = sum(e.monto_uyu or 0 for e in egresos)
    return {
        "ingresos_uyu": round(ingresos, 2),
        "ganancia_real_uyu": round(ingresos - costo - egresos_uyu, 2),
        "egresos_uyu": round(egresos_uyu, 2),
    }


def dinero_por_cobrar() -> dict:
    """Discos reservados: ventas casi cerradas que todavia no se cobraron."""
    reservados = Disc.query.filter(Disc.estado == "Reservado").all()
    total = sum((d.precio_mostrar or d.precio_uyu or 0) for d in reservados)
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
    hoy = date.today()
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
    hoy = date.today()
    discos = (
        Disc.query
        .filter(Disc.estado == "Disponible")
        .filter((Disc.origen != "personal") | (Disc.origen.is_(None)))
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
            "costo": round((d.costo_final_uyu or 0) * (d.stock or 1), 2),
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
    """Cuanto se invirtio en pedidos vs cuanto se recupero vendiendo esos discos."""
    from models import Pedido
    peds = Pedido.query.all()
    invertido = sum(p.costo_total_uyu for p in peds)
    recuperado = sum(p.recaudado_uyu for p in peds)
    discos_total = sum(p.discos_asignados for p in peds)
    discos_vendidos = sum(p.discos_vendidos for p in peds)
    pct = round(recuperado / invertido * 100, 1) if invertido > 0 else 0.0
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
    }


def arcano_vs_personal_6m() -> list:
    """Por mes (ultimos 6): ventas de stock de Arcano vs ventas de tu coleccion
    personal. Muestra cuanto se sostiene el negocio por si solo."""
    from datetime import date
    hoy = date.today()
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
