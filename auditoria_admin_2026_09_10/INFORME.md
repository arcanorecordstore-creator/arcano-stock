# Auditoría del admin de Arcano Record Store

**10 de septiembre de 2026 · Código local v4.96 · Primera etapa: administración**

## Evaluación general

El admin tiene buenas herramientas para la operativa de Arcano: carga de catálogo, reservas, clientes, ventas, caja, compras y análisis. Las pantallas principales funcionan y hay medidas de protección concretas. El problema más importante es la **consistencia entre esas herramientas**: una acción válida en una pantalla puede dejar datos contradictorios en otra.

**Encontré 12 problemas comprobados**, agrupando las variantes de un mismo problema. Cinco merecen prioridad alta porque pueden borrar o modificar ventas, dejar cancelaciones incompletas o perder información de clientes. También hay problemas de actualización de totales, señas, fechas y uso desde el celular.

Mi recomendación es dedicar la próxima entrega a corregir estos cruces antes de agregar funciones. No hace falta un rediseño visual ni cambiar el modelo de contabilidad por origen.

## Alcance y límites

- Revisé código, modelos, rutas, plantillas y JavaScript del admin. Recorrí sus 12 pantallas principales en navegador a 1280 px y 390 px, con datos ficticios.
- Ejecuté las 68 verificaciones de la suite existente: **68 aprobadas**. Sumé pruebas específicas de los problemas encontrados, guardadas junto a este informe.
- Probé protección de acceso en **119 rutas del admin**: todas redirigieron o rechazaron al cliente sin sesión. El manifest y el service worker son públicos y se excluyeron de esa cuenta: no contienen datos de negocio, aunque son excepciones a la regla escrita de proteger toda ruta `/admin/`.
- Validé sintaxis de 17 archivos Python, compilación de 47 plantillas Jinja, los 11 JavaScript externos y balance de llaves del CSS. Las páginas recorridas además se renderizaron con Flask y navegador.
- El código auditado incluye el cambio que ya existía en `templates/contabilidad.html`. No lo modifiqué. El documento de traspaso describe v2.72 y está desactualizado frente a v4.96.
- **No accedí al admin de producción ni utilicé su base real.** Por eso los fallos son reproducibles en esta copia del proyecto; no afirmo que ya hayan afectado operaciones reales, ni que esta copia coincida exactamente con lo desplegado.
- No hice cobros, envíos de mails, notificaciones ni llamadas pagas a IA. Scraping externo, análisis de facturas/videos, entregabilidad de correos, pagos y avisos push no quedaron verificados de punta a punta.
- No cambié la aplicación, su versión ni su base habitual. No hubo commit, push ni despliegue.

## Hallazgos comprobados

**Alta:** posible alteración de ventas o pérdida de datos. **Media:** funcionamiento incorrecto que requiere corrección. **Baja:** problema de presentación o interpretación con impacto menor.

### 1. Borrar una reserva vieja puede eliminar una venta que pertenece a otra operación

**Prioridad alta · Reservas / Ventas / Caja**

La eliminación toma todos los discos relacionados con la reserva y los devuelve a Disponible si están Reservados o Vendidos. No comprueba que la venta actual sea de esa reserva.

**Prueba:** un disco vendido por otra operación, también incluido en una reserva cancelada, pasó de Vendido a Disponible al borrar esa reserva cancelada. Como ventas y caja se reconstruyen desde el estado del disco, deja de contar esa venta.

**Corrección propuesta:** verificar la pertenencia de la operación antes de revertir un disco. Mostrar de antemano qué registros se modificarán. La misma revisión debe alcanzar “Eliminar venta”: su implementación también expande la eliminación a reservas y discos relacionados.

**Referencia:** `app.py`, `admin_reserva_detail`, acción `delete`, alrededor de 8323; `ventas_eliminar`, 3919.

### 2. Confirmar una reserva conflictiva puede cambiar el importe de una venta existente

**Prioridad alta · Reservas**

La pantalla advierte si un disco ya fue vendido por otra vía, pero el servidor permite marcar la reserva como Pagada y sobrescribe el precio cobrado con el de esa reserva.

**Prueba:** una venta de $1.000, identificada como otra operación, terminó computando $800 al confirmar una segunda reserva. Conservó el identificador de la venta anterior.

**Corrección propuesta:** bloquear ese cambio hasta resolver explícitamente el conflicto; no basta con un aviso. Separar “vincular una venta ya realizada” de “registrar una nueva venta”, respetando tu forma de cargar ventas de Instagram a posteriori.

**Referencia:** `app.py:8166`, `admin_reserva_detail`.

### 3. Cancelar una reserva manual pagada de un solo disco puede dejar la venta activa

**Prioridad alta · Reservas**

Al crear una reserva manual pagada, se asigna el identificador del grupo de venta solamente si hay más de un disco. La cancelación utiliza ese identificador para reconocer si puede revertir la venta.

**Prueba:** creé una reserva Pagada con un disco disponible y después la cancelé. Resultado: reserva Cancelada, disco Vendido, grupo vacío. La venta sigue contando aunque el texto de ayuda dice que el disco vuelve a Disponible.

**Corrección propuesta:** registrar la pertenencia de la venta también para reservas de un disco y usar la misma regla al crear, pagar, cancelar y eliminar.

**Referencia:** `app.py:8041` y `app.py:8166`.

### 4. Fusionar clientes puede perder el acceso web, premios y preferencias de avisos

**Prioridad alta · Clientes**

La fusión mueve compras y reservas, y completa datos de contacto. Sin embargo, borra el cliente de origen sin trasladar o conciliar su contraseña, avance del ciclo de lealtad, premio entregado ni preferencia de avisos.

**Prueba:** fusioné una cuenta web dentro de un cliente manual. El destino conservó el email, pero quedó sin contraseña, con avance de lealtad cero, premio no entregado y avisos habilitados aunque el origen los tenía deshabilitados.

**Corrección propuesta:** definir reglas de fusión para cuentas y premios, conservar la preferencia más restrictiva de avisos y mostrar un resumen antes de confirmar. Si ambas personas tienen cuenta web, ese conflicto requiere una decisión explícita.

**Referencia:** `app.py:8989`; campos de `Cliente` en `models.py:817`.

### 5. Revertir una venta desde la ficha puede conservar el precio de la venta anterior

**Prioridad alta · Ficha de disco / Ventas**

El formulario completo cambia el estado por un camino diferente al cambio rápido. No limpia todos los datos que sí limpia `register_status_change`.

**Prueba:** un disco vendido a $800 volvió a Disponible desde la ficha, con precio de lista nuevo de $1.500. Al marcarlo Vendido otra vez, siguió computando $800.

**Corrección propuesta:** centralizar las transiciones de estado para que ficha, dashboard, acciones masivas y reservas apliquen las mismas reglas. Mantener separados precio actual e importe histórico es correcto; el problema es heredar ese importe en una operación nueva.

**Referencia:** `app.py:817`, `populate_disc_from_form`; `app.py:3199`, `disc_edit`; `app.py:980`, `register_status_change`.

### 6. Los controles de señas del dashboard tienen un error de JavaScript

**Prioridad media · Dashboard**

El bloque que conecta los botones de seña intenta usar `tabla` y `guardar`, definidos dentro de otra función y fuera de su alcance.

**Evidencia:** el navegador registra `ReferenceError: tabla is not defined` al cargar el dashboard. El bloque de señas no llega a instalar sus acciones. La edición de precios y costos funciona porque se configura antes, en otro bloque.

**Corrección propuesta:** compartir correctamente esas funciones o mover el bloque a su alcance. Probar cobrar, devolver y conservar una seña desde el dashboard.

**Referencia:** `static/js/dashboard_quick_edit.js:409` y llamadas a `guardar` posteriores.

### 7. Los totales del dashboard quedan viejos después de una edición rápida

**Prioridad media · Dashboard**

La fila actualiza precio, ganancia y margen, pero los totales y resúmenes no se recalculan en pantalla.

**Prueba en navegador:** cambié un precio de $1.200 a $1.500. La fila mostró $1.500, mientras el total siguió en $7.500. Al recargar pasó a $7.800.

**Corrección propuesta:** devolver los totales de todo el conjunto filtrado o recargar después de guardar. No sumar solo las filas visibles: el cálculo de servidor actual hace bien en incluir todas las páginas.

**Referencia:** `static/js/dashboard_quick_edit.js`, guardado de precio/stock/costos; `templates/dashboard.html`, totales y resumen.

### 8. La comparación contable descuenta retiros personales que el resultado principal excluye

**Prioridad media · Contabilidad**

El cálculo principal separa “Retiro personal” de gastos operativos. El resumen utilizado para el período anterior suma todos los egresos, incluidos esos retiros.

**Prueba:** un retiro personal de $500 redujo en $500 el resultado anterior usado para comparar, aunque la vista de ese mes lo reconoce correctamente como retiro y lo excluye de gastos operativos.

**Corrección propuesta:** aplicar la misma exclusión a ambos períodos. Esto respeta el modelo actual del negocio; no propone cambiarlo. El gráfico de entradas y salidas puede incluir retiros por tratarse de caja, pero la comparación de resultados necesita otra base coherente.

**Referencia:** `metrics.py:351`, `contabilidad`; `metrics.py:514`, `_resumen_rango`.

### 9. Un gasto del mes siguiente puede impedir generar el recurrente del mes actual

**Prioridad media · Contabilidad / Gastos mensuales**

La búsqueda que evita duplicados usa fecha mayor o igual al inicio del mes, sin límite superior. También puede confundir gastos con el mismo importe nominal en monedas distintas, porque no compara moneda.

**Prueba:** cargué un gasto de Hosting de USD 7 para el mes siguiente y su recurrente activo. “Generar gastos del mes” no creó el gasto que faltaba en el mes actual.

**Corrección propuesta:** limitar la búsqueda al mes completo, identificar el recurrente de origen y contemplar moneda. Una restricción de unicidad ayudaría también ante dos ejecuciones simultáneas; esa concurrencia no fue probada aquí.

**Referencia:** `app.py:4231`, `generar_gastos_mes`.

### 10. La ficha acepta importes negativos por servidor y convierte stock cero en uno

**Prioridad media · Carga / Edición de discos**

El navegador incluye mínimos en los campos, pero el servidor no aplica las mismas reglas. La expresión que lee stock reemplaza cero por uno silenciosamente.

**Prueba:** envié precio −100, costo EUR −10 y stock 0 al formulario de edición. Guardó ambos importes negativos y stock 1.

**Alcance:** el formulario normal del navegador frena varios de esos valores. El fallo aparece al enviar la solicitud directamente o si otro cliente evita esa validación; no significa que cualquier tipeo negativo pase por la interfaz habitual.

**Corrección propuesta:** validación compartida de importes finitos y rangos, respuestas claras y una decisión consistente sobre si stock cero es válido. El cambio rápido actualmente sí acepta cero.

**Referencia:** `app.py:817`, `populate_disc_from_form`.

### 11. Hay desbordes horizontales en tres pantallas de celular

**Prioridad media · Uso móvil**

Con ancho de 390 px y el mismo conjunto de prueba:

| Pantalla | Ancho del contenido medido | Resultado |
|---|---:|---|
| Contabilidad | 441 px | Desborde; algunas tablas superan el espacio disponible |
| Ajustes | 398 px | Desborde leve |
| Agregar disco manual | 492 px | La fila de acciones se sale; “Cancelar” queda fuera del ancho inicial |

Dashboard, Reservas, Ventas, Clientes, Caja, Pedidos, Radar, carga combinada y carga de factura no mostraron desborde de página en ese recorrido. Las tablas con desplazamiento interno no se consideran por sí mismas un error.

**Corrección propuesta:** permitir que los botones salten de línea y contener las tablas en su propio desplazamiento. Repetir con nombres largos y pantallas de detalle. El resultado de Contabilidad corresponde al archivo local que ya tenía cambios pendientes.

**Referencia:** `templates/disc_form.html`, `.page-actions`; `templates/contabilidad.html`, tablas; `templates/settings.html` y CSS asociado.

### 12. Las reservas manuales muestran una hora tres horas anterior

**Prioridad baja · Reservas**

Las reservas manuales guardan `now_uy()`, ya en hora local. El filtro de presentación `hora_uy` presupone UTC y vuelve a restar tres horas.

**Prueba:** una hora local guardada como 19:35 se mostró como 16:35. La mezcla puede incluso desplazar la fecha mostrada cerca de medianoche.

**Corrección propuesta:** adoptar una sola convención de almacenamiento y presentación, contemplando los registros históricos antes de migrar. No aplicar una resta general sobre todas las fechas.

**Referencia:** `app.py:577`, filtro `_hora_uy`; creación manual en `app.py:8041`; `models.py:934`.

## Evaluación por apartado y oportunidades

| Apartado | Qué está bien | Qué conviene mejorar |
|---|---|---|
| Dashboard | Filtros variados, paginación, edición rápida, control de portada/home, orden manual. Los totales iniciales se calculan antes de paginar. | Corregir señas y totales. Dar mensajes explícitos al fallar un guardado; el destello rojo o la reversión de una celda se puede pasar por alto. |
| Ficha / alta manual | Datos, multimedia y stock agrupados; registro de cambios de estado; fecha automática al vender. | Unificar cambios de estado y validaciones. Avisar por cambios sin guardar. Distinguir mejor precio de lista y precio efectivamente cobrado. |
| URL / foto / video / factura | Formularios accesibles y pasos de revisión; límites de carga; aviso de falta de configuración de IA. | Probar archivos reales y errores externos en una etapa específica. Dar reintento por elemento y progreso para cargas largas. No certifico aquí precisión de IA ni scraping. |
| Tracks / galería / samples | Editores separados, cargas dedicadas, conversión de nuevas portadas a WebP y limpieza de archivos. | Probar reemplazo, reproducción y eliminación con archivos de muestra. Rechazar archivos corruptos por su contenido, no solamente por tipo declarado. |
| Reservas | Separa pago de entrega; conserva precios al reservar; admite carga manual y muestra conflictos. | Es el área más urgente: pertenencia de la venta, cancelaciones, borrado seguro y consistencia transaccional. La reserva y sus discos se guardan en commits distintos; un fallo intermedio puede dejar un cambio incompleto. |
| Ventas | Canales, métodos, rangos, agrupación, exportación CSV con filtros y carga manual facilitan seguimiento. | Revisar alcance de “Eliminar venta”; diferenciar anular de borrar. Verificar con datos reales la reconciliación entre operaciones agrupadas, copias e importes. |
| Clientes / lealtad | Centraliza compras, contacto, reservas y premios. | Corregir la fusión antes de usarla con cuentas reales. Agregar vista previa de diferencias y búsqueda/detección de duplicados. |
| Contabilidad | Diferencia origen personal/Arcano y retiros; exporta; muestra gastos fijos, períodos y recuperación. | Corregir comparación y recurrentes. Aclarar que la variación compara resultado operativo sin stock, mientras la cifra grande puede restarlo: hoy quedan visualmente asociados y se pueden interpretar como la misma fórmula. |
| Caja | Sincroniza ventas, pedidos, gastos y señas; permite ajustes e identifica movimientos manuales. Dos sincronizaciones seguidas conservaron cantidad e importes. | Mostrar claramente cuándo un importe fue corregido a mano y dejó de seguir al origen. El método editado en el formulario completo no sigue exactamente el mismo camino que la edición rápida: conviene unificarlo y probarlo. |
| Pedidos | Costo manual, comisión, asignación, recuperación y comparativa son útiles. | Un pedido sin costo manual usa el tipo de cambio global actual: probé USD 100 que pasaron de $4.100 a $5.000 al cambiar el TC. Eso también puede mover egresos históricos de caja. **Es un comportamiento comprobado cuya solución requiere confirmar si querés costo histórico o revaluación**, no lo cambio como una decisión contable automática. |
| Radar | Une escuchas y búsquedas sin resultado; sirve para orientar compras. | Mostrar ventana temporal de cada métrica y no presentar reproducciones como personas o intención de compra. “Calientes y disponibles” incluye Reservado y Apartado para mí en el código: conviene separar lo vendible de lo comprometido. |
| Ajustes | Reúne configuración pública, moneda, almacenamiento, backups y mantenimiento. | Dividir en secciones plegables con guardado claro; advertir cambios pendientes y mostrar el alcance de recalcular. Actualizar textos de ayuda que todavía remiten a editar `.env` al usuario final. |
| Exportaciones | CSV con BOM, Excel y PDF se generan; la suite verifica firmas básicas de archivos. Ventas tiene su exportación filtrada. | Revisar la presentación y reconciliación de importes con datos reales. La apertura del archivo no certifica por sí sola todos sus cálculos o diseño. |

## Seguridad, respaldo y mantenimiento

**Fortalezas verificadas en el código y pruebas:** control de sesión en las rutas operativas, límite de intentos de login, bloqueo de solicitudes con origen externo, cookies protegidas, encabezados de seguridad, validación de configuración obligatoria en Render y service worker que no cachea las páginas financieras del admin. No encontré una ruta operativa del admin abierta en el barrido sin sesión.

**Riesgos a atender, sin afirmar una explotación o pérdida ya ocurrida:**

1. **Backup con la base en uso.** La descarga envía directamente el archivo SQLite. Conviene generar una copia consistente con la función de backup de SQLite y verificar restauración. No se probó una descarga durante escrituras concurrentes.
2. **Respaldo incompleto de archivos.** Hay descarga de base y portadas, pero no un paquete integral de samples, comprobantes y demás archivos persistentes. La base conserva referencias; no reemplaza esos archivos. Proponer un respaldo completo y una prueba documentada de recuperación.
3. **Protección de formularios mejorable.** La verificación de origen compara hostname y admite solicitudes sin Origin/Referer; las cookies SameSite aportan otra defensa. Agregar tokens y validación estricta sería endurecimiento, no evidencia de un ataque exitoso en esta auditoría.
4. **Cambios destructivos sin recuperación dentro del producto.** “Eliminar” borra definitivamente. Sería preferible anular/archivar operaciones y conservar quién cambió qué, cuándo y por qué. Hoy el historial de estados no es un historial completo de importes, clientes y reservas.
5. **Dependencias sin versiones fijadas.** `requirements.txt` permite que distintas instalaciones resuelvan versiones diferentes. Fijar una combinación validada mejora la reproducibilidad; no hice un análisis de vulnerabilidades de paquetes.
6. **Entorno y documentación atrasados.** El ejecutable del entorno virtual apunta a una instalación Python que no está disponible en esta máquina. Pude ejecutar las pruebas con el runtime disponible y las bibliotecas del proyecto. Conviene reparar el entorno habitual y actualizar el traspaso.
7. **Rendimiento a medida que crezca el catálogo.** Dashboard carga todo el conjunto antes de paginar; Caja recorre datos históricos para sincronizar; Clientes y Radar hacen recorridos completos y consultas de relaciones. No medí saturación en producción. Optimizar solo con mediciones y volúmenes representativos.

## Orden recomendado de trabajo

1. **Integridad de operaciones:** hallazgos 1–5. Pruebas cruzadas entre reservar, vender, cancelar, borrar y volver a vender; fusión de clientes con cuenta y premios. Confirmar con Facundo las decisiones de fusión y vinculación de ventas existentes.
2. **Confiabilidad cotidiana:** señas del dashboard, totales que se actualicen, comparación contable y recurrentes. Revisar también costo histórico de pedidos con el modelo acordado.
3. **Uso y validación:** límites de importes, stock, horario uniforme, desbordes móviles y mensajes de guardado.
4. **Respaldo y evolución:** recuperación probada, archivo de operaciones, dependencias reproducibles, documentación y pruebas de regresión.

No recomiendo agregar otra pantalla o reordenar toda la estética antes de resolver la integridad de operaciones. Las herramientas necesarias ya existen; el mayor beneficio ahora es poder confiar en que registran lo mismo en todas las secciones.

## Evidencia y continuación

- `resultados.json`: resultados exactos de las pruebas aisladas.
- `pruebas.py`: reproducción de los casos, sin datos de producción. Son sondas de auditoría: registran lo que ocurre; todavía no son una suite de regresión que exija el comportamiento corregido.
- `evidencia_navegador.json`: tamaños de pantalla, error de JavaScript y comparación de totales antes/después de recargar.
- Suite existente: `tests/qa_suite.py`, 68 verificaciones aprobadas. Varios chequeos miran estado HTTP o firma de archivo; no prueban por sí mismos integridad del negocio. Por ejemplo, “Home NO muestra vendidos” solo comprueba que existan cards. Eso explica por qué esta suite pasa a pesar de los fallos encontrados.

Para completar la etapa de producción, falta contrastar la versión desplegada, probar los flujos externos en condiciones controladas y analizar un backup reciente si querés determinar si hay reservas/ventas reales inconsistentes. La tienda pública queda para la siguiente etapa solicitada; las pruebas públicas incidentales de la suite no equivalen a una auditoría completa de esa parte.
