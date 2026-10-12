# Thing To Sell To Ebay

App personalizada de Ecwid que reemplaza M2E Multichannel para la integración con eBay.
Publica productos a eBay, sincroniza inventario/precios y crea órdenes automáticamente.

## Lo que hace

1. **Panel** — ver productos publicados en eBay con sus listing IDs
2. **Productos** — lista de productos de tu tienda con checkboxes para elegir cuáles publicar
3. **Publicar** — un clic publica los seleccionados a eBay (Inventory API)
4. **Ajustes** — IDs de políticas de eBay (pago, envío, devolución), ubicación y categoría

## Registro de la app (lo hace Gelson)

1. Ve a **my.ecwid.com** → en la barra de búsqueda escribe `#develop-apps` y entra
2. Clic en **"Create new app"** (o "Crear aplicación")
3. Nombre: **Thing To Sell To Ebay**
4. Completa:
   - **OAuth redirect URL:** `https://<tu-app>.onrender.com/oauth/callback`
   - **App iframe URL:** `https://<tu-app>.onrender.com/dashboard`
5. En **Access scopes** pide: `read_catalog`, `read_orders`, `create_orders`, `update_inventory`
6. Guarda y copia el **Client ID** y **Client Secret**

## Despliegue en Render

1. Sube esta carpeta a un repo privado de GitHub
2. En [Render](https://render.com) → New → Web Service → conecta el repo
3. Configuración:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `gunicorn app:app --bind 0.0.0.0:$PORT`
4. Variables de entorno:
   - `ECWID_CLIENT_ID` = el Client ID del paso anterior
   - `ECWID_CLIENT_SECRET` = el Client Secret
   - `APP_URL` = la URL pública de Render (ej. `https://thing-to-sell-to-ebay.onrender.com`)
   - `FLASK_SECRET` = cualquier texto largo aleatorio
5. Despliega. Cuando esté en verde, actualiza las URLs en el portal de Ecwid
   con la URL real de Render (paso 4 del registro).

## Instalación en tu tienda

1. En el portal de desarrollador de Ecwid, abre tu app **Thing To Sell To Ebay**
2. Clic en **Install** / selecciona tu tienda (120529764)
3. Autoriza los permisos
4. Te redirige al Panel de la app

## Detalles técnicos

- **eBay Inventory API:** las políticas van dentro del objeto `listingPolicies`,
  NO como campos sueltos (eBay ignora los campos sueltos sin error).
- **Item specifics requeridos** (categoría 94845): Brand, MPN, UPC.
- **Ubicación:** `dallas-tx` (ya existe en eBay).
- **Políticas por defecto** (estéreos de auto):
  - Pago: `274761426019`
  - Envío: `279114158019` (tarifa fija, gratis)
  - Devolución: `275479123019` (no aceptada)
- Los SKUs en eBay usan el formato `ECWID-{product_id}` para mapear con Ecwid.
- La sincronización automática (inventario, precios, órdenes) corre por cron
  cada minuto vía `inventory_sync.py` (separado de esta app).

## Estructura

```
ecwid-app/
  app.py              # Flask: OAuth, productos, publicar, ajustes
  templates/          # base, dashboard, products, settings, error
  requirements.txt
  render.yaml         # despliegue en Render
  data/               # config por tienda (se crea solo, no subir a git)
  README.md
```
