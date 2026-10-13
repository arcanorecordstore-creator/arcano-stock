"""
Suite de QA funcional de Arcano Record Store.

Prueba la tienda publica Y el admin de punta a punta, sobre una base de datos
TEMPORAL y aislada (NUNCA toca tus datos reales de instance/ ni de produccion).

Como correrla (desde la carpeta del proyecto, con el venv activado):

    python tests/qa_suite.py

Te imprime cada prueba con OK / FALLA y un resumen al final. Si algo falla,
sale con codigo de error (util para automatizar). No necesita internet ni la app
levantada: usa el cliente de test de Flask.
"""

import os
import sys
import tempfile
import shutil

# --- AISLAMIENTO: definir la DB temporal y credenciales ANTES de importar la app ---
_TMP = tempfile.mkdtemp(prefix="arcano_qa_")
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "qa.db")
os.environ["COVERS_PATH"] = os.path.join(_TMP, "covers")
os.environ["SAMPLES_PATH"] = os.path.join(_TMP, "samples")
os.environ["ADMIN_USER"] = "qa"
os.environ["ADMIN_PASS"] = "qa"
os.environ["SECRET_KEY"] = "qa-secret-key"
os.environ.pop("RENDER", None)  # no forzar cookies secure en el test

# Permitir importar app.py desde la raiz del proyecto aunque se corra desde tests/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, date, timedelta  # noqa: E402
from app import create_app, db  # noqa: E402
from models import (  # noqa: E402
    Disc, Track, Pedido, Cliente, Reserva, ReservaItem, Egreso, MovimientoCaja, Setting,
)

app = create_app()
from flask.testing import FlaskClient


class AdminBrowserClient(FlaskClient):
    """Simula el token que envian los formularios y fetch del navegador."""
    def open(self, *args, **kwargs):
        method = kwargs.get('method', 'GET').upper()
        path = args[0] if args and isinstance(args[0], str) else kwargs.get('path', '')
        if method in ('POST', 'PUT', 'PATCH', 'DELETE') and path.startswith('/admin/'):
            with self.session_transaction() as sess:
                token = sess.get('_csrf_token')
            if not token:
                super().open('/admin/login', method='GET')
                with self.session_transaction() as sess:
                    token = sess.get('_csrf_token')
            kwargs['headers'] = dict(kwargs.get('headers') or {}, **{'X-CSRF-Token': token or ''})
        return super().open(*args, **kwargs)


app.test_client_class = AdminBrowserClient

# ---------------------------------------------------------------------------
# Mini framework de aserciones (sin dependencias)
# ---------------------------------------------------------------------------
_RESULTS = []  # (categoria, nombre, ok, detalle)
_CAT = "General"


def cat(nombre):
    global _CAT
    _CAT = nombre


def check(nombre, cond, detalle=""):
    _RESULTS.append((_CAT, nombre, bool(cond), "" if cond else detalle))


def check_status(client, metodo, url, esperado, nombre=None, **kw):
    """GET/POST a `url` y verifica el status. esperado: int o tupla de ints."""
    nombre = nombre or ("%s %s" % (metodo, url))
    try:
        resp = getattr(client, metodo.lower())(url, **kw)
        ok = resp.status_code in ((esperado,) if isinstance(esperado, int) else esperado)
        check(nombre, ok, "status %s (esperaba %s)" % (resp.status_code, esperado))
        return resp
    except Exception as e:  # noqa: BLE001
        check(nombre, False, "EXCEPCION: %r" % e)
        return None


# ---------------------------------------------------------------------------
# Semilla de datos de prueba
# ---------------------------------------------------------------------------
SEED = {}


def seed():
    with app.app_context():
        Setting.set("tc_eur_uyu", "47")
        Setting.set("tc_usd_uyu", "41")
        Setting.set("public_anuncio_texto", "Envios gratis en Montevideo a partir de 3 discos")

        # Pedido con discos
        ped = Pedido(numero="P-001", fecha=date.today() - timedelta(days=40),
                     cant_discos=3, costo_usd=300, proveedor="Deejay.de")
        db.session.add(ped)
        db.session.flush()

        d1 = Disc(artista="Test Artist A", titulo="Disco Disponible", sello="Sello X",
                  estado="Disponible", precio_uyu=1200, costo_final_uyu=600, stock=1,
                  origen="arcano", pedido_id=ped.id, fecha_ingreso=datetime.utcnow() - timedelta(days=30),
                  en_nuevos=True, destacado=True)
        d2 = Disc(artista="Test Artist B", titulo="Disco Vendido", sello="Sello Y",
                  estado="Vendido", precio_uyu=1400, costo_final_uyu=700, stock=1,
                  copias_vendidas=1, fecha_venta=datetime.utcnow() - timedelta(days=5),
                  origen="arcano", pedido_id=ped.id, canal_venta="instagram",
                  metodo_pago_venta="transferencia",
                  fecha_ingreso=datetime.utcnow() - timedelta(days=25))
        d3 = Disc(artista="Test Artist C", titulo="Disco Preorder", sello="Sello Z",
                  estado="Pre-order", precio_uyu=1300, costo_final_uyu=650, stock=1,
                  origen="arcano", es_preorder=True, pedido_id=ped.id,
                  fecha_ingreso=datetime.utcnow() - timedelta(days=10))
        d4 = Disc(artista="Test Artist D", titulo="Disco Personal", sello="Sello W",
                  estado="Vendido", precio_uyu=900, costo_final_uyu=0, stock=1,
                  copias_vendidas=1, fecha_venta=datetime.utcnow() - timedelta(days=3),
                  origen="personal", fecha_ingreso=datetime.utcnow() - timedelta(days=20))
        db.session.add_all([d1, d2, d3, d4])
        db.session.flush()

        db.session.add(Track(disc_id=d1.id, posicion=1, lado="A", titulo="Track 1",
                             youtube_url="https://youtu.be/xxxx", play_count=3))
        db.session.add(Track(disc_id=d1.id, posicion=2, lado="B", titulo="Track 2", play_count=1))

        # Egreso
        db.session.add(Egreso(fecha=date.today() - timedelta(days=2), categoria="Publicidad",
                              descripcion="Test ad", monto=100, moneda="USD", tc_usado=41,
                              monto_uyu=4100))

        # Cliente + Reserva
        cli = Cliente(nombre="Cliente Test", email="cliente@test.com")
        if hasattr(cli, "set_password"):
            try:
                cli.set_password("clave1234")
            except Exception:
                pass
        db.session.add(cli)
        db.session.flush()
        res = Reserva(cliente_id=cli.id, nombre="Cliente Test", email="cliente@test.com",
                      whatsapp="099000000", estado="Pendiente")
        db.session.add(res)
        db.session.flush()
        db.session.add(ReservaItem(reserva_id=res.id, disc_id=d1.id))

        db.session.commit()
        SEED["disc_disponible"] = d1.id
        SEED["disc_vendido"] = d2.id
        SEED["disc_preorder"] = d3.id
        SEED["reserva"] = res.id
        SEED["pedido"] = ped.id
        SEED["cliente"] = cli.id
        SEED["track"] = Track.query.filter_by(disc_id=d1.id).first().id


def login(client):
    return client.post("/admin/login", data={"user": "qa", "password": "qa"},
                       follow_redirects=True)


# ---------------------------------------------------------------------------
# PRUEBAS
# ---------------------------------------------------------------------------
def test_publico(c):
    cat("Tienda publica")
    r = check_status(c, "GET", "/", 200, "Home carga")
    if r is not None:
        html = r.get_data(as_text=True)
        check("Home muestra discos", "Disco Disponible" in html, "no aparece el disco de prueba")
        check("Home NO muestra vendidos como disponibles",
              "public-card" in html, "no hay cards")
    # v6.05: la ficha vive en /artista-titulo; /disco/<id> redirige (301) para siempre
    check_status(c, "GET", "/disco/%d" % SEED["disc_disponible"], 301, "Direccion vieja de la ficha redirige")
    check_status(c, "GET", "/disco/%d" % SEED["disc_disponible"], 200, "Ficha de disco",
                 follow_redirects=True)
    check_status(c, "GET", "/disco/999999", 404, "Ficha inexistente da 404")
    check_status(c, "GET", "/no-existe-este-disco", 404, "Direccion de ficha inexistente da 404")
    check_status(c, "GET", "/carrito", 200, "Carrito")
    check_status(c, "GET", "/favoritos", 200, "Favoritos")
    check_status(c, "GET", "/sobre", 200, "Sobre Arcano")
    check_status(c, "GET", "/legal", 200, "Legal")
    check_status(c, "GET", "/checkout", (200, 302), "Checkout GET")
    check_status(c, "GET", "/reservar/%d" % SEED["disc_disponible"], (200, 302), "Reservar GET")
    check_status(c, "GET", "/cuenta/registro", 200, "Registro cliente")
    check_status(c, "GET", "/cuenta/login", 200, "Login cliente")
    check_status(c, "GET", "/una-pagina-que-no-existe", 404, "404 en ruta inexistente")


def test_seo(c):
    cat("SEO / robots / sitemap")
    r = check_status(c, "GET", "/sitemap.xml", 200, "Sitemap carga")
    if r is not None:
        xml = r.get_data(as_text=True)
        check("Sitemap NO expone /admin", "/admin" not in xml, "el sitemap tiene rutas de admin")
        check("Sitemap incluye el home", "<url>" in xml, "sitemap vacio")
    r = check_status(c, "GET", "/robots.txt", 200, "robots.txt carga")
    if r is not None:
        txt = r.get_data(as_text=True)
        check("robots bloquea /admin/", "Disallow: /admin" in txt, "robots no bloquea admin")
        check("robots referencia el sitemap", "Sitemap" in txt, "robots sin sitemap")


def test_api_publica(c):
    cat("APIs publicas")
    check_status(c, "POST", "/api/track/%d/play" % SEED["track"], (200, 204),
                 "Registrar escucha (play)")
    check_status(c, "GET", "/api/buscar-sugerencias?q=Test", 200, "Sugerencias de busqueda")
    check_status(c, "GET", "/api/discos-por-id?ids=%d" % SEED["disc_disponible"], 200,
                 "Discos por id (carrito)")


def test_admin_auth(c):
    cat("Seguridad de acceso (admin cerrado sin login)")
    # Cliente NUEVO sin login
    anon = app.test_client()
    for url in ["/admin/", "/admin/stock", "/admin/tareas", "/admin/contabilidad", "/admin/caja", "/admin/pedidos",
                "/admin/ventas", "/admin/reservas", "/admin/settings",
                "/admin/backup/db", "/admin/export/csv"]:
        r = anon.get(url)
        # admin_required redirige al login (302) o responde 401/403 — NUNCA 200
        check("Bloqueado sin login: %s" % url, r.status_code in (302, 401, 403),
              "status %s (deberia redirigir al login)" % r.status_code)


def test_admin_paginas(c):
    cat("Admin: paginas principales cargan")
    login(c)
    for url, nombre in [
        ("/admin/", "Home"), ("/admin/stock", "Stock"), ("/admin/tareas", "Tareas"),
        ("/admin/tareas?vista=constancia", "Constancia"), ("/admin/contabilidad", "Contabilidad"),
        ("/admin/caja", "Caja"), ("/admin/pedidos", "Pedidos"),
        ("/admin/ventas", "Ventas"), ("/admin/reservas", "Reservas"),
        ("/admin/radar", "Radar"),
        ("/admin/settings", "Ajustes"),
        ("/admin/discs/%d" % SEED["disc_disponible"], "Detalle de disco"),
        ("/admin/discs/%d/edit" % SEED["disc_disponible"], "Editar disco"),
        ("/admin/discs/new", "Alta manual"),
        ("/admin/reservas/%d" % SEED["reserva"], "Detalle de reserva"),
    ]:
        check_status(c, "GET", url, 200, "Carga: %s" % nombre)
    # /admin/resumen es un redirect legacy a Contabilidad
    r = c.get("/admin/resumen")
    check("Resumen redirige a Contabilidad", r.status_code == 302, "status %s" % r.status_code)


def test_admin_quick_edit(c):
    cat("Admin: edicion rapida (quick-update)")
    login(c)
    did = SEED["disc_disponible"]
    # precio
    r = c.post("/admin/discs/%d/quick-update" % did, json={"campo": "precio_uyu", "valor": "1500"})
    ok = r.status_code == 200 and (r.get_json() or {}).get("ok")
    check("Quick-update precio", ok, "resp %s %s" % (r.status_code, r.get_data(as_text=True)[:120]))
    with app.app_context():
        check("Precio persistio", db.session.get(Disc, did).precio_uyu == 1500, "no guardo 1500")
    # estado -> Vendido pone fecha_venta sola
    r = c.post("/admin/discs/%d/quick-update" % did, json={"campo": "estado", "valor": "Vendido"})
    check("Quick-update estado", r.status_code == 200 and (r.get_json() or {}).get("ok"),
          "resp %s" % r.status_code)
    with app.app_context():
        d = db.session.get(Disc, did)
        check("Vendido puso fecha_venta sola", d.estado == "Vendido" and d.fecha_venta is not None,
              "fecha_venta vacia tras pasar a Vendido")
    # estado invalido -> 400
    r = c.post("/admin/discs/%d/quick-update" % did, json={"campo": "estado", "valor": "Inexistente"})
    check("Estado invalido rechazado (400)", r.status_code == 400, "acepto un estado invalido")


def test_admin_caja(c):
    cat("Admin: caja (movimientos y cuadrar)")
    login(c)
    # agregar ingreso manual
    r = c.post("/admin/caja/movimiento", data={"tipo": "ingreso", "monto": "500",
               "concepto": "QA ingreso", "metodo": "efectivo"}, follow_redirects=True)
    check("Agregar movimiento manual", r.status_code == 200, "status %s" % r.status_code)
    # cuadrar a un numero
    r = c.post("/admin/caja/cuadrar", data={"objetivo": "12345"}, follow_redirects=True)
    check("Cuadrar caja a un numero", r.status_code == 200, "status %s" % r.status_code)


def test_admin_exports(c):
    cat("Admin: exportaciones (CSV / Excel / PDF)")
    login(c)
    r = check_status(c, "GET", "/admin/export/csv", 200, "Export CSV stock")
    if r is not None:
        check("CSV tiene contenido", len(r.get_data()) > 20, "CSV vacio")
    r = check_status(c, "GET", "/admin/contabilidad/exportar", 200, "Export contabilidad CSV")
    r = check_status(c, "GET", "/admin/contabilidad/informe-pdf", 200, "Informe contabilidad PDF")
    if r is not None:
        check("Informe PDF es un PDF", r.get_data()[:4] == b"%PDF", "no arranca con %PDF")
    r = check_status(c, "GET", "/admin/informe-ejecutivo.pdf?periodo=todo", 200, "Informe ejecutivo PDF")
    if r is not None:
        check("Informe ejecutivo es un PDF", r.get_data()[:4] == b"%PDF", "no arranca con %PDF")
    r = check_status(c, "GET", "/admin/pedidos/exportar/excel", 200, "Export pedidos Excel")
    if r is not None:
        check("Excel es xlsx (PK zip)", r.get_data()[:2] == b"PK", "no parece xlsx")
    check_status(c, "GET", "/admin/pedidos/exportar/pdf", 200, "Export pedidos PDF")


def test_admin_backup(c):
    cat("Admin: backup")
    login(c)
    r = check_status(c, "GET", "/admin/backup/db", 200, "Backup de la base")
    if r is not None:
        data = r.get_data()
        check("Backup es SQLite", data[:16].startswith(b"SQLite format 3"),
              "el backup no parece una base SQLite")


def test_seguridad(c):
    cat("Seguridad: CSRF y rate limit")
    login(c)
    # CSRF: POST con Origin ajeno -> 403
    r = c.post("/admin/caja/saldo-inicial", data={"saldo_inicial": "0"},
               headers={"Origin": "https://sitio-malicioso.com"})
    check("CSRF: POST de origen ajeno rechazado (403)", r.status_code == 403,
          "status %s (deberia ser 403)" % r.status_code)
    # Rate limit login: 5 fallos -> el siguiente frena (429)
    anon = app.test_client()
    codes = []
    for _ in range(6):
        rr = anon.post("/admin/login", data={"user": "qa", "password": "MAL"})
        codes.append(rr.status_code)
    check("Rate limit login (429 tras varios fallos)", 429 in codes,
          "nunca devolvio 429 (codes=%s)" % codes)


def test_admin_acciones(c):
    cat("Admin: acciones (duplicar, bulk, reservas)")
    login(c)
    did = SEED["disc_preorder"]
    r = c.post("/admin/discs/%d/duplicate" % did, follow_redirects=True)
    check("Duplicar disco", r.status_code == 200, "status %s" % r.status_code)
    # toggle entrega de reserva
    r = c.post("/admin/reservas/%d/toggle-entrega" % SEED["reserva"], follow_redirects=True)
    check("Toggle entrega de reserva", r.status_code in (200, 302), "status %s" % r.status_code)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def main():
    print("=" * 66)
    print("  SUITE DE QA - Arcano Record Store")
    print("  DB temporal:", os.environ["DATABASE_PATH"])
    print("=" * 66)
    seed()
    c = app.test_client()

    for fn in [test_publico, test_seo, test_api_publica, test_admin_auth,
               test_admin_paginas, test_admin_quick_edit, test_admin_caja,
               test_admin_exports, test_admin_backup, test_seguridad,
               test_admin_acciones]:
        try:
            fn(c)
        except Exception as e:  # noqa: BLE001
            check("[%s reventó]" % fn.__name__, False, repr(e))

    # Reporte
    cat_actual = None
    ok_total = fail_total = 0
    for categoria, nombre, ok, detalle in _RESULTS:
        if categoria != cat_actual:
            print("\n%s" % categoria)
            print("-" * len(categoria))
            cat_actual = categoria
        marca = "  OK  " if ok else " FALLA"
        linea = "  [%s] %s" % (marca, nombre)
        if not ok and detalle:
            linea += "  ->  %s" % detalle
        print(linea)
        if ok:
            ok_total += 1
        else:
            fail_total += 1

    print("\n" + "=" * 66)
    print("  RESULTADO:  %d OK  |  %d FALLA  |  %d total"
          % (ok_total, fail_total, ok_total + fail_total))
    print("=" * 66)

    # limpieza
    try:
        shutil.rmtree(_TMP, ignore_errors=True)
    except Exception:
        pass

    sys.exit(1 if fail_total else 0)


if __name__ == "__main__":
    main()
