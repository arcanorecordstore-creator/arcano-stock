"""Auditoria reproducible: datos ficticios, DB temporal y comunicaciones bloqueadas."""
import os, sys, runpy, json, ast, pathlib, subprocess
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for key in ('SMTP_HOST','SMTP_USER','SMTP_PASS','SMTP_TO','ANTHROPIC_API_KEY','MP_ACCESS_TOKEN'):
    os.environ[key] = ''
qa = runpy.run_path(str(ROOT / 'tests/qa_suite.py'))
app, db = qa['app'], qa['db']
import mailer
mailer.can_send = lambda: False
from models import Disc, Reserva, ReservaItem, Cliente, Egreso, EgresoRecurrente, Pedido, Setting, MovimientoCaja, CAT_RETIRO_PERSONAL, today_uy, now_uy
import metrics
from datetime import date, timedelta
qa['seed']()
c = app.test_client()
qa['login'](c)
results = {}
def disc(**kw):
    d = Disc(artista='Auditoria', titulo='Caso aislado', precio_uyu=1000, costo_final_uyu=400, stock=1, **kw)
    db.session.add(d); db.session.flush(); return d
def reserva(d, **kw):
    r = Reserva(nombre='Prueba', whatsapp='000', **kw)
    db.session.add(r); db.session.flush()
    db.session.add(ReservaItem(reserva_id=r.id, disc_id=d.id, precio_uyu_snap=800))
    db.session.commit(); return r.id
with app.app_context():
    d = disc(estado='Vendido', precio_venta_uyu=1000, venta_grupo='otra-venta', fecha_venta=now_uy(), copias_vendidas=1)
    did = d.id; rid = reserva(d, estado='Cancelada')
r = c.post(f'/admin/reservas/{rid}', data={'accion':'delete'})
with app.app_context():
    results['borrar_reserva_ajena'] = {'http':r.status_code,'estado_venta_despues':db.session.get(Disc,did).estado}
    d = disc(estado='Vendido', precio_venta_uyu=1000, venta_grupo='venta-ajena', fecha_venta=now_uy(), copias_vendidas=1)
    did = d.id; rid = reserva(d, estado='Pendiente')
r = c.post(f'/admin/reservas/{rid}', data={'accion':'estado','nuevo_estado':'Pagada'})
with app.app_context():
    d=db.session.get(Disc,did)
    results['pagar_reserva_conflictiva']={'http':r.status_code,'precio_antes':1000,'precio_despues':d.precio_venta_uyu,'grupo':d.venta_grupo}
for campo in ('costo_eur','envio_eur'):
    r=c.post(f'/admin/discs/{did}/quick-update',json={'campo':campo,'valor':'12.50'})
    results[campo]={'http':r.status_code,'respuesta':r.get_json()}
with app.app_context():
    ini,fin=metrics._periodo_anterior('mes_actual')
    before=metrics._resumen_rango(ini,fin)
    db.session.add(Egreso(fecha=ini,categoria=CAT_RETIRO_PERSONAL,monto=500,moneda='UYU',monto_uyu=500))
    db.session.commit()
    after=metrics._resumen_rango(ini,fin)
    results['retiro_comparacion']={'cambio_resultado_anterior':after['ganancia_real_uyu']-before['ganancia_real_uyu'],'retiros_excluidos_en_vista':metrics.contabilidad('mes_pasado')['retiros_personales_uyu']}
    p=Pedido(numero='TC-audit',fecha=today_uy(),costo_usd=100,cant_discos=1)
    db.session.add(p);db.session.commit();pid=p.id
    before=p.costo_total_uyu
    Setting.set('tc_usd_uyu','50')
    results['pedido_tc_variable']={'antes':before,'despues':p.costo_total_uyu}
    db.session.add(EgresoRecurrente(categoria='Hosting',descripcion='Futuro-audit',monto=7,moneda='USD',activo=True))
    futuro=(today_uy().replace(day=28)+timedelta(days=5)).replace(day=1)
    db.session.add(Egreso(fecha=futuro,categoria='Hosting',descripcion='Futuro-audit',monto=7,moneda='USD',monto_uyu=350))
    db.session.commit()
c.post('/admin/contabilidad/generar-mes')
with app.app_context():
    results['recurrente_futuro_bloquea_mes']=Egreso.query.filter_by(descripcion='Futuro-audit',fecha=today_uy()).count()
    d=disc(estado='Disponible');did=d.id; db.session.commit()
form={'artista':'Auditoria','titulo':'Precio negativo','estado':'Disponible','stock':'0','precio_uyu':'-100','costo_eur':'-10','envio_eur':'0'}
r=c.post(f'/admin/discs/{did}/edit',data=form)
with app.app_context():
    d=db.session.get(Disc,did)
    results['validacion_formulario']={'http':r.status_code,'precio':d.precio_uyu,'stock_enviado':0,'stock_guardado':d.stock,'costo_eur':d.costo_eur}
    d=disc(estado='Disponible');did=d.id;db.session.commit()
c.post('/admin/reservas/nueva',data={'nombre':'Reserva un disco','disc_ids':str(did),'estado':'Pagada'})
with app.app_context():
    rid=Reserva.query.filter_by(nombre='Reserva un disco').first().id
c.post(f'/admin/reservas/{rid}',data={'accion':'estado','nuevo_estado':'Cancelada'})
with app.app_context():
    results['cancelar_manual_un_disco']={'reserva':db.session.get(Reserva,rid).estado,'disco':db.session.get(Disc,did).estado,'grupo':db.session.get(Disc,did).venta_grupo}
    origen=Cliente(nombre='Cuenta origen', email='origen@example.invalid');origen.set_password('solo-prueba');origen.lealtad_base=15000;origen.premio1_cobrado=True;origen.acepta_avisos=False
    destino=Cliente(nombre='Destino manual')
    db.session.add_all([origen,destino]);db.session.commit();oid,tid=origen.id,destino.id
c.post(f'/admin/clientes/{oid}/fusionar',data={'destino_id':str(tid)})
with app.app_context():
    t=db.session.get(Cliente,tid)
    results['fusion_cliente']={'cuenta_web_conservada':bool(t.password_hash),'lealtad_base':t.lealtad_base,'premio_cobrado':t.premio1_cobrado,'acepta_avisos':t.acepta_avisos}
with app.app_context():
    d=disc(estado='Vendido',precio_venta_uyu=800,fecha_venta=now_uy()-timedelta(days=60),copias_vendidas=1)
    did=d.id;db.session.commit()
edit={'artista':'Auditoria','titulo':'Reventa formulario','estado':'Disponible','stock':'1','precio_uyu':'1500','costo_eur':'10','envio_eur':'0','fecha_venta':''}
c.post(f'/admin/discs/{did}/edit',data=edit)
c.post(f'/admin/discs/{did}/quick-update',json={'campo':'estado','valor':'Vendido'})
with app.app_context():
    d=db.session.get(Disc,did)
    results['reventa_formulario']={'lista_nueva':d.precio_uyu,'precio_venta_heredado':d.precio_venta_uyu,'ingreso_computado':d.precio_mostrar}
    hora=now_uy()
    results['hora_reserva_manual']={'hora_guardada_local':hora.strftime('%H:%M'),'hora_mostrada':app.jinja_env.filters['hora_uy'](hora,'%H:%M')}
c.get('/admin/caja')
with app.app_context():
    n1=MovimientoCaja.query.count();s1=sum(m.monto_uyu for m in MovimientoCaja.query.all())
c.get('/admin/caja')
with app.app_context():
    results['caja_idempotente']={'antes':n1,'despues':MovimientoCaja.query.count(),'mismos_montos':s1==sum(m.monto_uyu for m in MovimientoCaja.query.all())}
pages={}
for rule in app.url_map.iter_rules():
    if rule.rule.startswith('/admin/') and 'GET' in rule.methods and not rule.arguments and rule.endpoint not in ('admin_logout','settings_debug','disc_debug'):
        if any(x in rule.rule for x in ('backup/covers','mp/test','debug','diag-banner')):continue
        r=c.get(rule.rule);pages[rule.rule]=r.status_code
results['paginas_GET']=pages
anon=app.test_client(); auth_routes={}
for rule in app.url_map.iter_rules():
    if not rule.rule.startswith('/admin/') or rule.endpoint in ('admin_login','admin_logout','admin_manifest','admin_service_worker'):continue
    url=rule.rule
    import re
    url=re.sub(r'<int:[^>]+>','1',url);url=re.sub(r'<path:[^>]+>','audit.txt',url)
    method='GET' if 'GET' in rule.methods else 'POST'
    r=anon.open(url,method=method)
    auth_routes[rule.rule]=r.status_code
results['auth']={'rutas':len(auth_routes),'no_redireccion_login':{k:v for k,v in auth_routes.items() if v not in (302,401,403)}}
syntax=[]
for f in list(ROOT.glob('*.py'))+list((ROOT/'tests').glob('*.py')):
    ast.parse(f.read_text(encoding='utf-8-sig'));syntax.append(f.name)
results['python_sintaxis']=len(syntax)
with app.app_context():
    templates=app.jinja_env.list_templates()
    for t in templates:app.jinja_env.get_template(t)
results['jinja_compiladas']=len(templates)
results['js_sintaxis']={str(f.relative_to(ROOT)):subprocess.run(['node','--check',str(f)],capture_output=True,text=True).returncode for f in (ROOT/'static/js').glob('*.js')}
css=(ROOT/'static/css/style.css').read_text(encoding='utf-8')
results['css_balance']=css.count('{')-css.count('}')
out=pathlib.Path(__file__).parent/'resultados.json';out.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(results,ensure_ascii=False,indent=2))
if '--serve' in sys.argv:
    print('Servidor de auditoria aislado 127.0.0.1:5017',flush=True)
    app.run(host='127.0.0.1',port=5017,use_reloader=False)
