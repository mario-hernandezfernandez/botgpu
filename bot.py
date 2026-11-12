"""Avisador de NVIDIA RTX 5080 CON STOCK a <= PRECIO_MAX (por email).

  python bot.py                  una pasada
  python bot.py --loop 60        pasadas sin fin, 60 s entre ellas (Mac)
  python bot.py --duracion 270   pasadas seguidas durante 270 s (GitHub Actions)
  python bot.py --test-email     envía un email de prueba
  python bot.py --prueba 2500    diagnóstico: qué encontraría con ese precio (no envía email)
"""
import json, os, re, smtplib, ssl, subprocess, sys, time
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import unquote, urlsplit

from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent


def cargar_env():
    f = BASE / ".env"
    if f.exists():
        for l in f.read_text().splitlines():
            l = l.strip()
            if l and not l.startswith("#") and "=" in l:
                k, v = l.split("=", 1)
                v = re.sub(r"\s+#.*$", "", v).strip().strip("\"'")  # quita comentarios al final de la línea
                os.environ.setdefault(k.strip(), v)
    # las contraseñas de aplicación de Google se muestran con espacios ("abcd efgh ..."): se quitan
    if os.getenv("SMTP_PASS"):
        os.environ["SMTP_PASS"] = os.environ["SMTP_PASS"].replace(" ", "")


cargar_env()
PRECIO_MAX = float(os.getenv("PRECIO_MAX", "1700"))
PRECIO_MIN = float(os.getenv("PRECIO_MIN", "700"))  # por debajo = accesorios, estafas o descuentos mal leídos
# Vendedores aceptados en Amazon ("*" = cualquiera)
AMAZON_VENDEDORES = [v.strip().lower() for v in os.getenv(
    "AMAZON_VENDEDORES",
    "amazon,msi,asus,gigabyte,aorus,zotac,pny,inno3d,palit,gainward,kfa2,galax,nvidia").split(",")]
MAX_VERIFICAR = 8  # fichas de producto a abrir por tienda y pasada
SEEN = BASE / "seen.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"


def tiendas():
    a, b = int(PRECIO_MIN * 100), int(PRECIO_MAX * 100)
    return {
        "PCComponentes": "https://www.pccomponentes.com/buscar/?query=rtx%205080",
        "Coolmod": "https://www.coolmod.com/tarjetas-graficas/?q=rtx+5080",
        "Life Informática": "https://lifeinformatica.com/#/dfclassic/query=rtx%205080&query_name=match_and",
        # Amazon: búsqueda en Informática ya filtrada por rango de precio
        "Amazon": f"https://www.amazon.es/s?k=nvidia+geforce+rtx+5080&i=computers&rh=p_36%3A{a}-{b}",
    }


# ---------- filtros de modelo ----------
MODELO = re.compile(r"rtx[\s-]*5080(?![\s-]*(ti|super|s\b|\d))", re.I)
NO_ES_GPU_5080 = re.compile(
    r"\b(50[5679]0|40[5-9]0|30[5-9]0|5080\s*(ti|super)|rx\s?\d{4})\b|port[aá]til|laptop|notebook|ordenador|"
    r"sobremesa|\bpc gaming\b|\btorre\b|bloque de agua|water\s?block|"
    r"reacondicionad|renewed|usad[oa]\b|segunda mano|open box|caja abierta", re.I)


def es_rtx5080(titulo):
    return bool(MODELO.search(titulo or "")) and not NO_ES_GPU_5080.search(titulo or "")


PRECIO_RE = re.compile(r"(\d{1,2}(?:[.\s]\d{3})+(?:,\d{1,2})?|\d{3,4}(?:,\d{1,2})?)\s*€")


def precios(txt):
    out = []
    for m in PRECIO_RE.finditer(txt or ""):
        try:
            out.append(float(m.group(1).replace(".", "").replace(" ", "").replace(",", ".")))
        except ValueError:
            pass
    return [p for p in out if PRECIO_MIN <= p <= 6000]


def a_float(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = re.sub(r"[^\d,.]", "", str(x))
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def normalizar(href):
    h = unquote(href)
    m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", h)
    if "amazon." in h and m:
        return f"https://www.amazon.es/dp/{m.group(1)}"
    p = urlsplit(href)
    return f"{p.scheme}://{p.netloc}{p.path}"


def bloqueado(page):
    t = (page.title() or "").lower()
    return any(s in t for s in ("un momento", "just a moment", "access denied", "attention required", "robot")) \
        or page.locator('form[action*="validateCaptcha"]').count() > 0


# ---------- listado ----------
JS_LISTADO = r"""() => [...document.querySelectorAll('a[href]')].map(a => {
  const img = a.querySelector('img');
  const own = [a.innerText||'', a.title||'', a.getAttribute('aria-label')||'', img ? img.alt : ''].join(' ');
  let n = a, t = a.innerText || '';
  for (let i = 0; i < 6 && n.parentElement && !/\d\s*€/.test(t); i++) { n = n.parentElement; t = n.innerText || ''; }
  return {href: a.href, own: own.slice(0, 300), text: t.slice(0, 800)};
})"""


def candidatos(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(4000)
    if bloqueado(page):
        raise RuntimeError("BLOQUEADO (antibot/captcha)")
    page.mouse.wheel(0, 5000)
    page.wait_for_timeout(1500)
    res = {}
    for it in page.evaluate(JS_LISTADO):
        if not MODELO.search(it["own"]) or not it["href"].startswith("http"):
            continue
        ps = precios(it["text"])
        if ps and min(ps) <= PRECIO_MAX * 1.15:  # margen: el listado a veces muestra el precio sin descuento
            u = normalizar(it["href"])
            res.setdefault(u, {"url": u, "precio_listado": min(ps), "titulo": " ".join(it["own"].split())[:150],
                               "texto": it["text"]})
    return list(res.values())


# ---------- ficha de producto: modelo, precio, stock ----------
JS_LDJSON = r"""() => { const out = [];
  const walk = o => { if (!o || typeof o !== 'object') return; if (Array.isArray(o)) { o.forEach(walk); return; }
    if ([].concat(o['@type'] || []).includes('Product')) out.push({name: o.name, offers: o.offers});
    if (o['@graph']) walk(o['@graph']); };
  document.querySelectorAll('script[type="application/ld+json"]').forEach(s => { try { walk(JSON.parse(s.textContent)) } catch (e) {} });
  return out; }"""

JS_BOTON = r"""() => { const re = /añadir al carrito|añadir a la cesta|añadir a cesta|comprar ahora|^comprar$|add to cart/i;
  return [...document.querySelectorAll('button, input[type=submit], a')].some(b => {
    const t = (b.innerText || b.value || '').trim(); if (!re.test(t) || t.length > 40) return false;
    const r = b.getBoundingClientRect(), cs = getComputedStyle(b);
    return r.width > 0 && r.height > 0 && !b.disabled && b.getAttribute('aria-disabled') !== 'true'
      && cs.visibility !== 'hidden' && cs.pointerEvents !== 'none' && !/disabled|agotado/i.test(b.className); }); }"""

JS_ZONA = r"""() => { let n = document.querySelector('h1'); if (!n) return '';
  while (n.parentElement && (n.innerText || '').length < 1500) n = n.parentElement;
  return (n.innerText || '').slice(0, 6000); }"""

SIN_STOCK = re.compile(r"agotad|sin stock|sin unidades|fuera de stock|no disponible|av[ií]same|notif[ií]came|"
                       r"pr[oó]ximamente|pre-?venta|reserva|bajo pedido", re.I)


CON_STOCK_LISTADO = re.compile(r"entrega (hoy|mañana|en \d+[^|\n]{0,20})|recíbelo [^|\n]{0,25}|en stock|"
                               r"env[ií]o (en|inmediato)[^|\n]{0,20}|disponible", re.I)


def verificar_generico(page):
    prods = page.evaluate(JS_LDJSON)
    titulo = (prods[0].get("name") if prods else None) or page.locator("h1").first.inner_text(timeout=3000)
    disp, precio = [], None
    for p in prods:
        offs = p.get("offers") or []
        offs = offs if isinstance(offs, list) else [offs]
        for o in offs:
            for sub in ([o] + (o.get("offers") if isinstance(o.get("offers"), list) else [])):
                if sub.get("availability"):
                    disp.append(str(sub["availability"]))
                pr = a_float(sub.get("price") or sub.get("lowPrice"))
                if pr and (precio is None or pr < precio):
                    precio = pr
    boton = page.evaluate(JS_BOTON)
    negativo = bool(SIN_STOCK.search(page.evaluate(JS_ZONA)))
    disp_txt = " ".join(disp)
    # Regla principal: si se puede añadir al carrito, hay stock.
    if boton:
        stock, motivo = True, "confirmado: botón 'añadir al carrito' activo"
    elif re.search(r"InStock|LimitedAvailability|OnlineOnly", disp_txt) and not negativo:
        stock, motivo = True, "probable: la tienda lo marca en stock (no se detectó el botón)"
    else:
        stock, motivo = False, "sin botón de añadir al carrito"
    return {"titulo": titulo, "precio": precio, "stock": stock, "motivo": motivo, "vendedor": ""}


JS_AMAZON = r"""() => { const q = s => { const e = document.querySelector(s); return e ? e.innerText.trim() : '' };
  const pe = document.querySelector('#corePrice_feature_div .a-offscreen, #corePriceDisplay_desktop_feature_div .a-price .a-offscreen, #apex_desktop .a-price .a-offscreen');
  return { titulo: q('#productTitle'), disp: q('#availability'), precio: pe ? pe.textContent : '',
    vendedor: q('#sellerProfileTriggerId') || q('[offer-display-feature-name="desktop-merchant-info"] .offer-display-feature-text') || q('#merchant-info'),
    buybox: q('#tabular-buybox') || q('#buybox'),
    carrito: !!document.querySelector('#add-to-cart-button, #buy-now-button') }; }"""


def verificar_amazon(page):
    d = page.evaluate(JS_AMAZON)
    vendedor = d["vendedor"]
    if not vendedor:
        m = re.search(r"Vendido por\s*\n?\s*([^\n]+)", d["buybox"])
        vendedor = m.group(1).strip() if m else ""
    disp = d["disp"]
    # Regla principal: botón "Añadir a la cesta"/"Comprar ya" presente = stock (salvo reservas de preventa)
    stock = d["carrito"] and not re.search(r"reserva|disponible el", disp, re.I)
    ok_vend = "*" in AMAZON_VENDEDORES or any(re.search(rf"\b{re.escape(v)}\b", vendedor.lower()) for v in AMAZON_VENDEDORES)
    motivo = (f"confirmado: se puede añadir al carrito ({disp or 'botón activo'})" if stock
              else f"sin stock ({disp or 'sin botón de añadir al carrito'})")
    if stock and not ok_vend:
        stock, motivo = False, f"vendedor no aceptado: {vendedor or 'desconocido'}"
    return {"titulo": d["titulo"], "precio": a_float(d["precio"]), "stock": stock, "motivo": motivo, "vendedor": vendedor}


def verificar(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)
    for _ in range(8):  # los retos antibot tipo "Un momento…" se resuelven solos en unos segundos
        if not bloqueado(page):
            break
        page.wait_for_timeout(1500)
    if bloqueado(page):
        raise RuntimeError("BLOQUEADO (antibot/captcha)")
    return verificar_amazon(page) if "amazon." in url else verificar_generico(page)


# ---------- avisos ----------
def enviar_email(asunto, cuerpo):
    if not os.getenv("SMTP_USER") or not os.getenv("SMTP_PASS"):
        print("!! Falta SMTP_USER/SMTP_PASS: no se puede enviar el email")
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"] = asunto, os.environ["SMTP_USER"]
    msg["To"] = os.getenv("EMAIL_TO") or os.environ["SMTP_USER"]
    msg.set_content(cuerpo)
    with smtplib.SMTP_SSL(os.getenv("SMTP_HOST", "smtp.gmail.com"), int(os.getenv("SMTP_PORT", "465")),
                          context=ssl.create_default_context()) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)


def notificar_mac(texto):
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", f'display notification "{texto}" with title "RTX 5080" sound name "Glass"'],
                       check=False)


def ciclo(browser, prueba=False):
    viejo = json.loads(SEEN.read_text()) if SEEN.exists() else {}
    nuevo, alertas, fallidas = {}, [], set()
    for nombre, url in tiendas().items():
        ctx = browser.new_context(locale="es-ES", user_agent=UA, viewport={"width": 1366, "height": 900})
        ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page = ctx.new_page()
        try:
            cands = candidatos(page, url)
            ficha_bloqueada = False
            print(f"[{nombre}] {len(cands)} candidatas <= {PRECIO_MAX:.0f}€ en el listado")
            for c in sorted(cands, key=lambda x: x["precio_listado"])[:MAX_VERIFICAR]:
                v = None
                if not ficha_bloqueada:
                    try:
                        v = verificar(page, c["url"])
                    except Exception as e:
                        print(f"   ? ficha no accesible ({str(e)[:60]}); se usa el listado")
                        ficha_bloqueada = "BLOQUEADO" in str(e)
                if v is None:
                    # sin ficha: solo vale si el listado muestra un plazo de entrega real y no pone agotado
                    m = CON_STOCK_LISTADO.search(c["texto"])
                    ok = bool(m) and not SIN_STOCK.search(c["texto"])
                    v = {"titulo": c["titulo"], "precio": None, "stock": ok, "vendedor": "",
                         "motivo": f"según el listado: '{m.group(0)}' (ficha no verificable)" if ok
                         else "listado sin plazo de entrega -> se asume sin stock"}
                if not MODELO.search(v["titulo"] or ""):
                    v["titulo"] = c["titulo"]
                precio = v["precio"] or c["precio_listado"]
                if not v["precio"] and c["precio_listado"] > PRECIO_MAX:
                    continue
                modelo_ok = es_rtx5080(v["titulo"])
                valida = modelo_ok and v["stock"] and PRECIO_MIN <= precio <= PRECIO_MAX
                print(f"   {'OK' if valida else '--'} {precio:.2f}€ | {'5080' if modelo_ok else 'NO-5080'} | "
                      f"{v['motivo']}{' | ' + v['vendedor'] if v['vendedor'] else ''} | {v['titulo'][:70]}")
                if valida:
                    h = {"tienda": nombre, "precio": precio, "url": c["url"], "titulo": v["titulo"],
                         "motivo": v["motivo"], "vendedor": v["vendedor"]}
                    nuevo[c["url"]] = h
                    if c["url"] not in viejo or precio < viejo[c["url"]]["precio"]:
                        alertas.append(h)
        except Exception as e:
            fallidas.add(nombre)
            print(f"[{nombre}] ERROR: {str(e).splitlines()[0][:120]}")
        finally:
            ctx.close()
    if prueba:
        return
    # conservar lo ya avisado de tiendas que han fallado esta vez (para no repetir avisos)
    nuevo.update({k: v for k, v in viejo.items() if v["tienda"] in fallidas and k not in nuevo})
    SEEN.write_text(json.dumps(nuevo, ensure_ascii=False, indent=1))
    if alertas:
        alertas.sort(key=lambda h: h["precio"])
        cuerpo = "\n\n".join(
            f"{h['tienda']}: {h['precio']:.2f} €  —  stock {h['motivo']}"
            f"{chr(10) + 'Vendedor: ' + h['vendedor'] if h['vendedor'] else ''}\n{h['titulo']}\n{h['url']}"
            for h in alertas)
        asunto = f"RTX 5080 CON STOCK a {alertas[0]['precio']:.0f}€ en {alertas[0]['tienda']}"
        print(">>> ALERTA\n" + cuerpo)
        notificar_mac(asunto)
        enviar_email(asunto, cuerpo)


def arg(nombre, defecto=None):
    return sys.argv[sys.argv.index(nombre) + 1] if nombre in sys.argv else defecto


if __name__ == "__main__":
    if "--test-email" in sys.argv:
        enviar_email("Test bot RTX 5080", "Si lees esto, los avisos por email funcionan.")
        print("Email de prueba enviado")
        sys.exit()
    prueba = arg("--prueba")
    if prueba:
        PRECIO_MAX = float(prueba)
    loop, duracion = int(arg("--loop", 0)), int(arg("--duracion", 0))
    inicio = time.time()
    with sync_playwright() as p:
        # Google Chrome real (pasa el antibot de PCComponentes); si no está instalado, Chromium de Playwright
        args = ["--disable-blink-features=AutomationControlled"]
        try:
            browser = p.chromium.launch(headless=True, channel="chrome", args=args)
        except Exception:
            print("Aviso: Google Chrome no encontrado, uso Chromium (PCComponentes puede bloquear las fichas)")
            browser = p.chromium.launch(headless=True, args=args)
        while True:
            print(time.strftime("== %Y-%m-%d %H:%M:%S =="), flush=True)
            ciclo(browser, prueba=bool(prueba))
            sys.stdout.flush()
            if prueba or (not loop and not duracion) or (duracion and time.time() - inicio > duracion - 60):
                break
            time.sleep(loop or 20)
