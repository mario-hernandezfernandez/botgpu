# Avisador de stock de RTX 5080

Avisa por email (y con una notificación del Mac) cuando una **NVIDIA RTX 5080** (ni Ti, ni Super, ni portátiles, ni reacondicionadas)
**se puede añadir al carrito** por **≤ 1700 €** (o el precio que pongas en `PRECIO_MAX`).

Tiendas: PCComponentes, Coolmod, Life Informática y Amazon.es. En Amazon solo cuenta si lo vende Amazon o una marca
oficial (MSI, ASUS, Gigabyte/Aorus, Zotac, PNY, Inno3D, Palit, Gainward, KFA2, Galax, NVIDIA). Puedes cambiarlo con `AMAZON_VENDEDORES`.

Solo te avisa una vez por producto, y otra vez si baja de precio.

## 1. Email (Gmail)
1. Activa la verificación en dos pasos en tu cuenta de Google.
2. Crea una contraseña de aplicación: https://myaccount.google.com/apppasswords
3. `cp .env.example .env` y rellena `SMTP_USER`, `SMTP_PASS` y `EMAIL_TO`.
4. Prueba: `.venv/bin/python bot.py --test-email`

## 2. En el Mac (cada ~60 s, mientras esté encendido)
```bash
./install-mac.sh                                                 # instala y arranca (y en cada inicio de sesión)
tail -f bot.log                                                  # ver lo que hace
launchctl unload ~/Library/LaunchAgents/com.rtx5080.bot.plist   # pararlo
```
Necesita Google Chrome instalado (con Chromium, PCComponentes bloquea las fichas).
El Mac no puede estar en reposo: `caffeinate -s` o Ajustes > Batería > evitar reposo automático.

## 3. GitHub Actions (24/7)
1. Sube esta carpeta a un repositorio (el `.gitignore` excluye `.env`, así que tus credenciales nunca se suben).
2. Settings > Secrets and variables > Actions > *Secrets*: `SMTP_USER`, `SMTP_PASS`, `EMAIL_TO`.
   (Opcional, en *Variables*: `PRECIO_MAX`.)
3. Actions > "RTX 5080 stock check" > Run workflow, para probarlo.

Se lanza cada 5 min y cada ejecución hace pasadas durante ~4-5 min. GitHub a veces retrasa los cron, así que el Mac es más rápido.
Con el repositorio **público** los minutos son ilimitados; en uno privado, el plan gratuito (2000 min/mes) se agota en ~1 día. Además, las IPs de GitHub
pueden recibir más bloqueos antibot que tu casa.

## Check diario (20:00)
Todos los días sobre las 20:00 (hora de España) GitHub te envía un email "Check diario RTX 5080" con:
el estado de cada tienda (si se ha podido leer), la RTX 5080 más barata con stock en cada una, las 5 más baratas
y cuántas comprobaciones automáticas se han hecho en las últimas 24 h.
**Si un día no te llega, algo falla.** Para probarlo a mano: Actions > "Check diario 20:00" > Run workflow,
o en el Mac `.venv/bin/python bot.py --informe`.

## Diagnóstico
`.venv/bin/python bot.py --prueba 2300` muestra todo lo que encontraría a ese precio y por qué lo acepta o lo descarta (no envía email).

## Limitaciones
- **NVIDIA Marketplace / Founders Edition**: desde aquí no responde (error de conexión), así que no está incluido.
  Para la FE, apúntate al aviso oficial de NVIDIA.
- **Tienda ASUS y Gigabyte/Aorus**: bloquean los bots ("Access Denied"). Sus tarjetas se cubren a través de
  Amazon (vendidas por la marca o por Amazon) y de las demás tiendas.
- Si una tienda cambia su web, revisa `bot.log` o la salida de `--prueba`.
