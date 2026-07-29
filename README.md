# Arcano Stock — v1.0

Aplicacion local para gestion de stock de Arcano Record Store.

---

## Como arrancar la app

**Forma facil:** doble click en `start.bat`.

**Forma manual** (PowerShell con venv activo):
```
python app.py
```

Abrir el navegador en: http://127.0.0.1:5001

Para apagar el servidor: Ctrl+C en la ventana de PowerShell o cerrar la ventana negra.

---

## Como hacer backup

**Forma manual:** doble click en `backup.bat`. Copia `arcano_stock.db` a la carpeta `backups/` con timestamp en el nombre.

**Recomendacion:** ejecutar el backup al menos una vez por semana. Tambien:
- Antes de probar funcionalidades nuevas o experimentales.
- Despues de cargar muchos discos juntos.
- Antes de tocar archivos del proyecto.

Los backups quedan dentro de la carpeta del proyecto. Si queres seguridad extra,
copiar la carpeta `backups/` a OneDrive, Google Drive o un pendrive periodicamente.

---

## Como cargar discos

Tres formas, en orden de comodidad:

1. **Desde URL** (Deejay.de) — pegas URL y la app extrae los datos gratis del HTML.
2. **Desde screenshot** — subis una imagen y Claude API la analiza (~$0.01 USD).
3. **Manual** — todo a mano si los otros dos modos no aplican.

En cualquiera de los tres, te lleva al mismo formulario donde podes revisar/editar
antes de guardar.

---

## Configuracion

### Tipo de cambio EUR -> UYU
Editar en "Ajustes". El valor se aplica a discos nuevos. Para recalcular discos
existentes hay un boton "Recalcular stock disponible" en la misma pantalla.

### API key de Anthropic
Editar `.env` y poner la key en `ANTHROPIC_API_KEY=sk-ant-...`. Reiniciar el
servidor.

### Costos API
El gasto acumulado se ve en Ajustes. Se puede reiniciar el contador local sin
afectar la cuenta de Anthropic.

---

## Estructura del proyecto

```
arcano_stock/
├── app.py                 # Rutas Flask
├── models.py              # Modelos SQLAlchemy (Disc, Setting, StatusHistory)
├── vision.py              # Extraccion via Claude API
├── scraper.py             # Scraping de Deejay.de
├── metrics.py             # Calculos del dashboard de resumen
├── config.py              # Carga del .env
├── seed.py                # Discos de prueba (opcional)
├── start.bat              # Arrancar la app
├── backup.bat             # Backup de la DB
├── requirements.txt
├── .env                   # API keys (no compartir)
├── arcano_stock.db        # Base SQLite (autogenerada)
├── backups/               # Backups automaticos (autogenerada)
├── static/
│   ├── css/style.css
│   ├── js/app.js
│   ├── js/upload.js
│   └── covers/            # Portadas descargadas
└── templates/             # HTML
```

---

## Que viene en Fase 2

Lista de mejoras planificadas para mas adelante:

- **Tienda publica online** — version publica accesible para clientes con el
  mismo stock pero sin botones de admin. La arquitectura ya esta preparada
  (DB y logica de negocio separadas del frontend admin).
- **Catalogo de sellos normalizado** — lista unica de sellos con autocompletado.
- **Soporte de mas tiendas** en scraping — Discogs, Hardwax, Bandcamp.
- **Multiples copias con condicion individual** — si una copia es Mint y otra
  VG+, poder distinguirlas.
- **Reportes mensuales** automaticos en PDF.
- **Import inicial desde CSV/Excel** — si en el futuro queres cargar un lote
  grande desde una planilla.

---

## Soporte

Este proyecto es 100% local. No hay servidor en la nube ni datos compartidos
con nadie excepto la API de Anthropic cuando se usa "Desde screenshot".
