# ARCANO RECORD STORE — Documento de traspaso completo
**Generado: 5 de agosto de 2026 · Estado al cierre de la sesión "dominio + QA + v2.72"**

Este documento contiene TODO el contexto necesario para continuar trabajando en la web de Arcano exactamente donde quedamos. Subilo a los archivos del Proyecto de Claude o adjuntalo al primer mensaje del chat nuevo.

---

## 1. ROL DE CLAUDE Y ESTILO DE TRABAJO

- Claude opera como **COO / estratega de marketing / líder técnico** de Arcano según el contexto. Facundo es el dueño y decide; Claude ejecuta, propone y da **feedback honesto y directo**, nunca complaciente. Separa métricas de vanidad de lo que mueve ventas.
- Comunicación **siempre en español rioplatense (voseo)**.
- Facundo **NO es developer** (Windows 11, Intel i5 13va gen). Todo lo técnico se entrega en bloques ejecutables con rutas exactas de Windows y explicaciones claras.
- Rechaza contenido con tono de copywriter: frases espejadas, slogans, "coach de Instagram". Solo afirmaciones verificables que él pueda demostrar con sus propios discos.
- Antes de resolver un problema detectado, Claude pregunta si Facundo lo considera problema desde su operativa.
- Cuando Facundo elige opciones con botones, Claude confirma el modelo mental antes de programar features contables o de negocio.

## 2. EL NEGOCIO

- **Arcano Record Store**: tienda curada de vinilos de electrónica underground (house, techno, minimal), Montevideo, Uruguay. Facundo es DJ (alias **Lapente**) y coleccionista; solo vende lo que él pondría o guardaría.
- Ventas históricamente por **Instagram DM (@arcanorecordstore)**; la web ya permite reservas.
- Sourcing: **Deejay.de** (principal) + digging presencial en Europa (París: Yoyaku, Syncrophone, Techno Import; Madrid: Tipo, Rotor, Marilians, El Almacén; Barcelona: Discos Paradiso, Subwax).
- Diferenciales web: player flotante que sigue sonando al navegar, "Escuchar todo", curaduría visible (Tips + reseñas), chart de escuchas. Competidores locales (2222, Cinética) **no tienen web que rankee**; la vara real son Deejay.de/Yoyaku/Juno.

## 3. INFRAESTRUCTURA Y ACCESOS (estado actual)

- **Dominio: https://arcanorecordstore.com** — comprado HOY 5/8/2026 en **Porkbun** (USD 11.08/año, renueva igual, WHOIS privacy gratis activada, 2FA recomendado activar). Usuario Porkbun visible en panel: ffll1699.
- **DNS en Porkbun (Cloudflare DNS)**: A `@` → 216.24.57.1 · A `www` → 216.24.57.1 · MX fwd1/fwd2.porkbun.com · TXT spf · 2 TXT _acme-challenge. Los ALIAS/CNAME de parking de Porkbun fueron borrados.
- **Render**: servicio `arcano-stock`, workspace plan figura como "Hobby". Custom domains verificados, SSL emitido, **www redirige al dominio raíz**. Subdominio `arcano-stock.onrender.com` sigue activo (toggle Enabled).
- Disco persistente `/var/data`: DB en `/var/data/arcano_stock.db`, covers en `/var/data/covers`.
- **Env vars en Render**: DATABASE_PATH, COVERS_PATH, SECRET_KEY, ADMIN_USER, ADMIN_PASS, ANTHROPIC_API_KEY, 6 de SMTP (Gmail con app password), y RENDER (automática, activa SESSION_COOKIE_SECURE).
- **GitHub**: repo privado `arcano-stock`, usuario `arcanorecordstore-creator`, branch `main`. Flujo de deploy: Facundo descomprime el ZIP que entrega Claude → sube archivos a GitHub web → Commit → Render autodespliega ("Deployed").
- **Google Analytics 4**: propiedad "Arcano Web", **ID G-32YTNG1DQ9**, flujo con URL https://arcanorecordstore.com. El snippet completo (2 scripts) está pegado en Ajustes → "Código de Analytics" (setting `analytics_snippet`), se inyecta solo en la tienda pública, NO en admin. Verificado en Tiempo Real. **No borrar el snippet: sostiene la verificación de Search Console.**
- **Google Search Console**: verificado automáticamente vía GA. **Sitemap enviado: estado Correcto, 67 páginas descubiertas** (5/8/2026).
- **Local (Facundo)**: `C:\Users\facun\herramientas\arcano_stock\` — puerto 5001, venv, Python 3, SQLite en `instance\arcano_stock.db`, covers en `static\covers\`.
- **Admin**: solo por URL directa `/admin/` (sin links públicos, robots lo bloquea). Login con rate limiting.

## 4. DATOS REALES DE PRODUCCIÓN (backup verificado 5/8/2026)

101 discos (61 Disponible, 32 Vendido, 4 Reservado, 2 Pre-order, 2 Apartado para mi) · 379 tracks · 4 pedidos · 2 clientes · 26+ settings · Egresos: hoy cargó **Dominio 11.75 USD (470 UYU)**, **Hosting 7 USD (280 UYU)**, y Publicidad 116.56 USD (4.662) del 20/6. Integridad de backup: OK (11 tablas). ~212 covers en producción.

## 5. STACK Y ARQUITECTURA

**Flask + SQLAlchemy + SQLite + Gunicorn + flask-compress**. Frontend: Jinja2, CSS propio (~187KB, tema claro/oscuro con variables), JS vanilla (player.js, public_anim.js, app.js, dashboard_quick_edit.js). Fuente Space Grotesk. requirements: Flask, flask-compress, SQLAlchemy, openpyxl, reportlab, Pillow, etc.

### Modelos (models.py)
- **Disc**: artista, titulo, sello, anio, formato, genero, pais, condicion, tags, precio_uyu, precio_oferta, costo_eur, envio_eur, tc_usado, costo_final_uyu, stock, estado, copias_vendidas, fecha_venta, fecha_reserva, fecha_ingreso, cover_local/cover_url, destacado, en_nuevos, cartel_ultima, mostrar_agotado, es_preorder, **es_tip**, **resena_corta**(160), origen('arcano'/'personal'), pedido_id(FK), descripcion_publica... Properties: ganancia_uyu, margen_pct, precio_mostrar, esta_agotado, copias_facturadas, mostrar_cartel_nuevo/ultima, en_carrusel_preorder, tracks_con_sample, tiene_sample. ESTADOS_DISCO = [Disponible, Pre-order, Reservado, Vendido, Apartado para mi, Descartado].
- **Track**: disc_id(FK CASCADE), posicion, lado, titulo, artista, duracion, sample_local, youtube_url (sirve YouTube O SoundCloud), youtube_start, **play_count**. Properties: has_sample, sample_kind ('local'|'youtube'|'soundcloud'|'none').
- **PlayEvent**: track_id, disc_id, fecha (para chart "del mes" real, 30 días).
- **DiscImage**: galería por disco (posicion, filename).
- **StatusHistory**: historial de estados por disco.
- **Cliente / Reserva / ReservaItem**: cuentas de clientes y reservas multi-disco con emails de confirmación (SMTP Gmail).
- **Egreso**: fecha(Date), categoria, descripcion, monto, moneda(UYU/USD/EUR), tc_usado, **monto_uyu (editable a mano desde v2.71)**. CATEGORIAS default: Hosting, Publicidad, Envios, Viajes, Otros + extras vía Setting.
- **EgresoRecurrente**: gastos mensuales fijos + botón "Generar gastos del mes" (idempotente).
- **Pedido**: numero, fecha, cant_discos, costo_eur, costo_usd, **costo_uyu_manual** (si está, manda sobre el cálculo), proveedor(Deejay.de/Viaje/Otro), notas. Properties: costo_total_uyu, costo_total_uyu_calculado, usa_costo_manual, discos_asignados/vendidos, recaudado_uyu, recuperado, falta_recuperar, resultado_uyu, resultado_potencial_uyu, margen_pct, margen_potencial_pct, dias_recuperacion, rotacion_pct, rotacion_semaforo(verde≥70/amarillo30-70/rojo<30), costo_por_disco_uyu.
- **Setting**: key-value con **caché por request vía flask.g** (set() invalida). Helpers: get_tc (EUR, default 47), get_tc_usd (default 40), get_categorias_egreso.
- **Migraciones**: `_migrate_schema()` idempotente (ALTER TABLE por columna faltante); tablas nuevas via create_all. Ya migradas: pedido_id, origen, costo_uyu_manual, es_tip, resena_corta, play_count.

### Settings importantes (claves)
tc_eur_uyu, tc_usd_uyu, public_tagline, public_anuncio_texto (barra superior), public_hero_*, public_about*, public_envios, public_instagram, public_whatsapp (VACÍO — decisión pendiente de Facundo), public_grid_*, public_discos_por_pagina, public_carrusel_velocidad, public_card_tamano, analytics_snippet, **tip_label** (default "Tip", editable), **carrusel_preorder/tips/escuchados/destacados/nuevos** ("1"/"0", toggles v2.72), categorias_egreso_extra.

### Rutas clave
- Público: `/` (home con carruseles+filtros+paginación), `/disco/<id>`, `/carrito`, checkout de reserva, `/sobre`, favoritos, cuenta cliente, `/sitemap.xml`, `/robots.txt` (Allow: / · Disallow: /admin/, /cuenta/, /carrito, /checkout · Sitemap), **POST `/api/track/<id>/play`** (contador de escuchas, público).
- Admin (todas con @admin_required salvo login/logout): `/admin/` dashboard (paginado 25/50/100/todos, quick-edit AJAX `/admin/discs/<id>/quick-update` con campo/valor JSON, bulk, duplicar `/admin/discs/<id>/duplicate`), disc_form (crear/editar completo), desde URL (scraper deejay.de), desde screenshot/factura (usa ANTHROPIC_API_KEY), reservas, `/admin/contabilidad` (períodos + custom desde/hasta, export CSV), egresos + recurrentes + **POST `/admin/contabilidad/egreso/<id>/monto-uyu`**, `/admin/pedidos` (+ **POST `/admin/pedidos/<id>/costo-manual`**, exports Excel 3 hojas y PDF ejecutivo), `/admin/settings` (+ POST `/admin/settings/public`), **`/admin/backup/db`** y **`/admin/backup/covers`**, login con rate limiting (5 fallos→60s creciente por IP, sleep 1s por fallo).
- Seguridad global: gzip (flask-compress), cookies HTTPONLY+SAMESITE=Lax (+SECURE si RENDER), headers (X-Frame-Options SAMEORIGIN, nosniff, Referrer-Policy), CSRF por verificación de Origin/Referer en POST/PUT/DELETE a /admin (origen ajeno → 403).
- Covers: helper `convertir_cover_a_webp()` aplicado en los 4 puntos de entrada (upload, scraper, galería, factura). Solo covers NUEVAS; las existentes intactas.

### Frontend público — features
- Player flotante (player.js): MP3 local vía `<audio>` (#fp-audio), YouTube/SoundCloud vía iframe (#fp-youtube-slot). Playlist interna con prev/next. **registrarPlay()** fire&forget al reproducir. **"Escuchar todo"** (#listen-all-btn): junta todos los `.card-play-btn[data-tracks]` de la página en una super-playlist; MP3 avanza con `ended`, YouTube/SC con timeout de 60s; botón toggle Detener; cover por track.
- Home: carruseles Pre-order / Tip (label editable) / Lo más escuchado del mes (top 6 por PlayEvents 30 días, se oculta sin datos) / Destacados / Nuevos — **cada uno con toggle en Ajustes**. Solo aparecen en página 1 sin filtros.
- Cards: badge ★Tip dorado, reseña corta en cursiva, badges Nuevo/Última copia, botones play/fav/cart, precio (con oferta tachada si hay), lazy loading, skeleton shimmer + fade-in (img-pending/img-loaded), hover elevación+zoom.
- Micro-interacciones (public_anim.js): fly-to-cart (clon de cover vuela al carrito), bounce del contador, prefetch on hover de fichas, reveal on scroll (IntersectionObserver).
- View Transitions API (@view-transition, fades 0.18/0.22s), respeta prefers-reduced-motion.
- Ficha: galería, tracklist con samples por track (botones con data-track-id), botón Reservar, botón WhatsApp condicional al setting, Compartir, similares por género, overlay VENDIDO/Agotado sobre cover, badges/tags, SEO completo (meta, OG, Twitter, Schema.org, canonical).
- Mobile (v2.72): header compacto en una línea (logo chico + hamburguesa + carrito SVG con badge; nav superior oculta porque existe bottom-nav), íconos SVG en bottom-nav (nota/corazón/carrito reemplazaron emojis), player flotante reordenado (cover 44px, textos con ellipsis, audio a fila completa, volumen oculto), bottom-nav 4 items.

### Admin — features
Dashboard 18 columnas con orden por columnas (asc/desc/reset), filtros (texto, estado, sello, origen, fechas por tipo), totales del pie SIEMPRE sobre todo lo filtrado, paginación (25/50/100/todos; mobile default sugerido 25), quick-edit inline (precio, stock, estado con fecha_venta automática y copias_vendidas=1, pedido_id, toggles destacado/en_nuevos/cartel_ultima/mostrar_agotado/es_preorder/es_tip), bulk-bar (solo visible con selección), duplicar disco (copia campos+tracks+imágenes, arranca Disponible), export CSV stock. Contabilidad: hero ganancia real + comparación período anterior, por cobrar (reservas), desglose con **origen** (línea azul "↳ de tu coleccion personal"; costo de vendidos SOLO arcano; **costo de stock no vendido arcano se resta**; ganancia_real = ingresos − costo_vendidos_arcano − stock_no_vendido_arcano − egresos), gráfico 6 meses, panel pedidos, 8 KPIs, top sellos/discos, gastos con monto_uyu editable, recurrentes. Pedidos: tarjetas con costo editable (manual/calculado), rotación con semáforo, proyección, costo x disco, días de recuperación, lista de discos desplegable, comparativa (totales + mejor/peor por margen), gráfico de barras por margen, exports ejecutivos. Ajustes: TC EUR/USD, recalcular stock, textos públicos, WhatsApp, analytics, tip_label, toggles de carruseles, grid/paginación pública, banner, barra de anuncio, backup DB+covers.

## 6. MÉTODO DE TRABAJO TÉCNICO DE CLAUDE (crítico para continuidad)

1. **El entorno de Claude se REINICIA entre chats.** En el chat nuevo NO va a existir `/home/claude/work/arcano_stock/`. Como el repo de GitHub es privado, **Facundo debe subir el ZIP del proyecto completo** (o mínimo: app.py, models.py, metrics.py, templates/, static/) en el primer mensaje técnico del chat nuevo. Para análisis de datos reales, subir también un backup fresco de la DB (botón en Ajustes).
2. Claude reconstruye en `/home/claude/work/arcano_stock/`, y ANTES de editar verifica versión con `grep footer-version templates/base.html`.
3. Validación SIEMPRE antes de entregar: `ast.parse` para .py, Jinja2 get_template para .html, `node --check` para .js, balance de llaves para CSS.
4. Testing: Flask test_client para lógica + Playwright headless (`--no-sandbox --disable-setuid-sandbox --disable-dev-shm-usage`) para UI/capturas. Deps: `pip install --break-system-packages` (flask, flask-compress, openpyxl, reportlab, pillow, playwright) + `python3 -m playwright install chromium`. El sandbox NO tiene acceso a dominios externos (YouTube/GA fallan ahí: es normal, no es bug).
5. Entrega: ZIP solo con archivos modificados, estructura `arcano_stock/...`, a `/mnt/user-data/outputs/`, con present_files. **Facundo sube cada ZIP a GitHub apenas lo recibe** — no advertir sobre acumulación.
6. **Actualizar `footer-version` en templates/base.html en CADA entrega** (formato: `v2.XX · Descripción corta`).
7. Si un lote es muy grande para un turno: avisar estado honesto y seguir con "Continuar" (dinámica probada).
8. Si un test da FALLA, investigar antes de reportar: la mayoría fueron artefactos del test (URLs mal llamadas, clases mal escritas, red del sandbox), no bugs.

## 7. HISTORIAL DE VERSIONES (resumen v2.55 → v2.72, todas en producción salvo confirmar la última)

- v2.55 totales de ganancia + orden por columnas · v2.56-57 **Contabilidad** (períodos, custom, comparación, por cobrar, gráfico 6m, KPIs) · v2.58 **Pedidos** · v2.59 overlay VENDIDO en ficha + carruseles filtran vendidos · v2.60 columna Pedido en dashboard · v2.61 resultado + lista discos en tarjeta pedido · v2.62 export Excel/PDF pedidos · v2.63 filtro fechas custom + features pedidos (margen, recuperación, semáforo, proyección, comparativa, gráfico) · v2.64 bulk-bar solo con selección + **Duplicar disco** · v2.65 **costo_uyu_manual en pedidos** · v2.66 informes ejecutivos (Excel 3 hojas, PDF con resumen) · v2.67 **contabilidad por origen** (aporte personal destacado, stock no vendido arcano se resta) · v2.68 **auditoría aplicada**: gzip, cookies seguras, headers, CSRF origin-check, rate limit login, backup DB+covers, analytics_snippet, caché settings, selectinload, paginación dashboard, botón WhatsApp en ficha · v2.69 **curaduría+fluidez**: escuchar todo, play_count+PlayEvent+chart, Tip con label editable, reseña corta, WebP, view transitions, skeleton, fly-to-cart, prefetch · v2.70 opción 25/pág + dashboard mobile pulido · v2.71 monto_uyu editable en gastos · **v2.72 mobile público pulido (header compacto, carrito SVG, player) + toggles de carruseles — CONFIRMAR SI YA ESTÁ DESPLEGADA.**

## 8. DECISIONES DE NEGOCIO TOMADAS (no reabrir salvo que Facundo lo pida)

- **Mercado Pago: POSTERGADO** a propósito. Sin costo fijo; comisión checkout UY ~5,99%+IVA inmediato (~7,3% real) o 4,99%+IVA a 21 días (~6,1% real). Se integra cuando el volumen de reservas web justifique automatizar el cobro; se anunciará como mejora ("ahora pagás online").
- **"Avisame cuando vuelva": DESCARTADA** por Facundo.
- **Rediseño visual: DESCARTADO.** Se mostraron 6 mockups (3 skins A/B/C + 3 estructuras D crates/E lista/F vitrina); no le gustó ninguno. La web mantiene su diseño actual. Si se retoma: que Facundo traiga 2-3 webs de referencia que le gusten y trabajar desde ahí. Lección aprendida: para diseño, los mockups deben variar ESTRUCTURA, no solo colores — y siempre entregarse con present_files (view no lo ve el usuario).
- **Contabilidad por origen** (v2.67): personales vendidos suman ingreso destacado como aporte, NO restan costo; stock no vendido arcano SÍ resta de ganancia real.
- **WhatsApp en ficha**: implementado, se activa solo, cargando el número en Ajustes. Decisión de Facundo pendiente ("luego").
- **Duplicar vs copias_vendidas**: conviven; para copias múltiples se recomienda Duplicar.
- **La contabilidad cuenta ventas por fecha_venta** (necesaria para períodos). Quick-update/bulk la ponen sola; solo la edición manual del form puede dejarla vacía.

## 9. QA — ESTADO Y LIMITACIÓN CONOCIDA

- Suite completa corrida sobre el código de producción (v2.71): **37/37 OK reales** (páginas, SEO, sitemap sin admin, filtros, robots, auth en todas las rutas admin incl. backup, carrito+contador, favoritos, player, escuchar-todo, ficha, mobile sin overflow, bottom-nav, login, quick-edit, vendido con fecha, duplicar, contabilidad, pedidos, 3 exports, backup SQLite real, ajustes). Las 6 "fallas" iniciales fueron artefactos del test.
- **Limitación**: Claude NO puede hacer QA funcional sobre el dominio vivo (web_fetch no ejecuta JS y robots bloquea). Opciones ofrecidas a Facundo (ELECCIÓN PENDIENTE): (1) checklist manual de 15 min para que lo haga él desde el celu — recomendada pre-anuncio; (2) script Playwright que Claude escribe y Facundo corre en su Windows contra el dominio real; (3) UptimeRobot gratis para monitoreo. 

## 10. LANZAMIENTO — ESTADO Y PRÓXIMOS PASOS

**Hecho hoy (5/8/2026):** dominio comprado y conectado, SSL, www→raíz, GA4 apuntando al dominio, Search Console verificado, sitemap Correcto (67 páginas), gastos de dominio y hosting cargados en Contabilidad.

**Pendientes de Facundo (orden sugerido):**
1. Confirmar deploy de v2.72 y revisar mobile en el celu real.
2. Elegir toggles de carruseles (sugerido para lanzar: Pre-order + Tip + Nuevos; apagar Escuchados y Destacados hasta tener datos/curaduría).
3. Marcar 3-5 discos como Tip + escribirles reseña corta.
4. Recorrido manual de compra (checklist QA opción 1) antes de anunciar.
5. Cambiar link de bio de IG a arcanorecordstore.com.
6. Barra de anuncio: "Bienvenidos a la web oficial de Arcano" unos días (opcional).
7. **Anuncio orgánico = el lanzamiento** (no esperar la pauta): historias + posteo; reel mostrando "Escuchar todo" ("escuchá todo el catálogo como diggeando en la tienda").
8. Días después: **pauta Meta** — objetivo Mensajes o Tráfico (NUNCA Interacción), audio original/licenciado, segmentación Montevideo+Maldonado 20-50 intereses DJ/electrónica, creative: reel de Facundo DJing.
9. Decidir WhatsApp sí/no.
10. Activar 2FA en Porkbun si no lo hizo.
11. Cargar hosting como EgresoRecurrente mensual (hoy lo cargó como egreso suelto).

**Pendientes técnicos abiertos:**
- QA sobre dominio vivo: esperar elección (checklist vs script local).
- Sugerido no urgente: panel de escuchas en admin (plays por disco + cruce "escuchado vs vendido") — única feature nueva que Claude empuja; la data ya se acumula sola.
- Drop Hunter v3.0.1 (herramienta aparte de reels): pendiente decisión Opción B vs B+C para los 404 de deejay.de en catálogo viejo.
- Futuro: Mercado Pago cuando haya volumen; dominio en GA4 ya actualizado.

## 11. MARKETING Y CONTENIDO (principios vigentes)

- **Unboxing = activo #1 de ventas** (histórico: 8 ventas en una tarde orgánica). Voz auténtica de Facundo solo sale respondiendo preguntas casuales, no con guiones.
- Loop semanal: anticipo → unboxing + ad Mensajes → catálogo → sello destacado → disco de la semana → últimas unidades → pedido a medida. Cadencia: 2 discos/día × 5 días, carrusel educativo cada ~10 días (sirven a marca, no a ventas directas), reel los viernes. Rotación de formatos de 8 semanas definida (archivo del proyecto: claude_rutina-semanal-arcano.md).
- Scarcity real ("queda 1", stories de vendidos) = social proof gratis.
- Identidad visual: negro #0A0A0A para catálogo/ventas (bandas arriba/abajo); gris-beige #C8C4BC con números fantasma para educativo. Space Grotesk (700/500/400), 1080×1350. Sin dos puntos en copys.
- Paris content (Yoyaku/Syncrophone) pendiente de publicar; formato "mostrar compras" NO (usado en House Mates).

## 12. CÓMO ARRANCAR EL CHAT NUEVO

Mensaje sugerido para Facundo: *"Seguimos con Arcano. Acá está el documento de traspaso [adjunto o ya en el proyecto] + el ZIP del proyecto actual [adjuntar] + backup de la DB si hace falta [adjuntar]. Retomamos en: [tema]."*
Claude debe: leer este documento, verificar la versión del ZIP contra el historial (§7), y continuar con el rol y método descritos. Si el chat está en el mismo Proyecto, también puede usar la búsqueda de conversaciones pasadas para detalles finos de cualquier sesión anterior.

*Fin del traspaso. — Generado por Claude (COO de Arcano) el 5/8/2026.*
