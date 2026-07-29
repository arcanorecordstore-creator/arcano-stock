# Guía para poner Arcano online (deploy en Render)

Esta guía te lleva paso a paso. No necesitás experiencia previa.
Hacela con calma, un paso a la vez. Si algo no sale, pará y consultá.

Al final, tu tienda va a estar en internet con una dirección tipo
`https://arcano-record-store.onrender.com` que podés compartir con
cualquiera.

---

## ANTES DE EMPEZAR: qué vas a necesitar

- Una cuenta de correo (para registrarte en GitHub y Render)
- Tu tarjeta (Render Starter cuesta USD 7/mes; te la piden al elegir ese plan)
- Los archivos del proyecto (los que te preparé) en tu PC
- Tu base de datos actual con los 110 discos y las portadas

---

## PARTE 1 — Subir el código a GitHub

GitHub es donde va a vivir el código para que Render lo pueda leer.

### 1.1 Crear cuenta en GitHub
1. Andá a https://github.com y hacé clic en "Sign up".
2. Registrate con tu mail. Elegí un usuario (ej: "arcanorecords").
3. Confirmá tu mail.

### 1.2 Crear un repositorio
1. Ya logueado, hacé clic en el "+" arriba a la derecha -> "New repository".
2. Nombre: `arcano-stock`
3. Marcá "Private" (privado, para que nadie más vea tu código).
4. NO marques ninguna otra opción. Clic en "Create repository".

### 1.3 Subir los archivos
La forma más simple sin línea de comandos:
1. En la página del repo recién creado, hacé clic en "uploading an existing file".
2. Arrastrá TODA la carpeta del proyecto (todos los archivos que te preparé).
   IMPORTANTE: NO subas el archivo `.env` (tiene secretos) — el `.gitignore`
   ya lo excluye, pero verificá que no esté.
3. Abajo, en "Commit changes", clic en el botón verde.

Esperá a que suban todos los archivos.

---

## PARTE 2 — Desplegar en Render

### 2.1 Crear cuenta en Render
1. Andá a https://render.com y hacé clic en "Get Started".
2. Registrate con "GitHub" (te conecta las dos cuentas, más fácil).
3. Autorizá a Render para ver tus repos.

### 2.2 Crear el servicio web
1. En el panel de Render, clic en "New +" -> "Web Service".
2. Elegí tu repo `arcano-stock` de la lista.
3. Render va a detectar el archivo `render.yaml` que te preparé y va a
   configurar casi todo solo. Si te pregunta, elegí usar el Blueprint.
4. Confirmá el plan "Starter" (USD 7/mes). Acá te pide la tarjeta.

### 2.3 Configurar las variables secretas
Render ya configuró la base de datos, el disco y la SECRET_KEY solo.
Faltan 3 que tenés que poner a mano (por seguridad). En la pestaña
"Environment" del servicio, agregá estas tres:

| Variable | Qué poner |
|---|---|
| `ADMIN_USER` | El usuario con que vas a entrar al admin (ej: facundo) |
| `ADMIN_PASS` | Una contraseña FUERTE (larga, con números y símbolos) |
| `ANTHROPIC_API_KEY` | Tu API key de Anthropic (la del .env local) |

Guardá los cambios. Render va a reconstruir la app.

### 2.4 Esperar el primer deploy
Render va a "construir" tu app (tarda unos minutos la primera vez).
Cuando termine, vas a ver "Live" en verde y una URL arriba tipo
`https://arcano-record-store.onrender.com`.

Entrá a esa URL: vas a ver tu tienda... pero VACÍA (sin discos todavía).
Eso es normal — falta subir tu base de datos. Vamos a eso.

---

## PARTE 3 — Subir tus 110 discos y portadas

Tu base de datos y portadas están en tu PC. Hay que subirlas al disco
persistente de Render. Esta parte la hacemos juntos cuando llegues acá
— avisame y te doy el método exacto según cómo quedó tu Render.

(Resumen de lo que haremos: usar la consola web de Render para subir el
archivo `arcano_stock.db` y la carpeta de portadas al disco `/var/data`.)

---

## PARTE 4 — Cambiar la contraseña de admin y probar

1. Entrá a `https://tu-url.onrender.com/admin`
2. Iniciá sesión con el ADMIN_USER y ADMIN_PASS que configuraste.
3. Verificá que ves tus discos, que la tienda funciona, que las portadas
   se ven.
4. Hacé una reserva de prueba para confirmar que el flujo anda.

---

## DESPUÉS (opcional)

- **Dominio propio**: cuando quieras, comprás arcanorecordstore.com y lo
  conectás a Render (te guío).
- **Mercado Pago**: cuando valides que la gente reserva, lo integramos.

---

## SI ALGO SALE MAL

- La app no arranca -> revisá los "Logs" en Render, copiámelos y lo vemos.
- No se ven las portadas -> es el paso de subir la carpeta de covers.
- No podés entrar al admin -> revisá ADMIN_USER y ADMIN_PASS en Environment.

Cualquier cosa, pará y consultame con lo que veas en pantalla.
