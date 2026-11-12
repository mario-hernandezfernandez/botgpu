"""Avisador de NVIDIA RTX 5080 CON STOCK a <= PRECIO_MAX (por email).

  python bot.py                  una pasada
  python bot.py --loop 120       pasadas sin fin, 2 min entre ellas (Mac)
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
MAX_VERIFICAR = int(os.getenv("MAX_VERIFICAR", "5"))  # nº de RTX 5080 con stock a reunir por tienda (las más baratas)
MAX_FICHAS = int(os.getenv("MAX_FICHAS", "15"))       # tope de fichas abiertas por tienda y pasada
SEEN = BASE / "seen.json"


def tiendas(tope=None):
    a, b = int(PRECIO_MIN * 100), int((tope or PRECIO_MAX) * 100)
    return {
        "PCComponentes": "https://www.pccomponentes.com/buscar/?query=rtx%205080",
        # páginas de categoría solo RTX 5080, de menor a mayor precio
        "Coolmod": "https://www.coolmod.com/tarjetas-graficas/serie-rtx-5080/?ordenacion=precio-menor",
        "NeoByte": "https://www.neobyte.es/tarjetas-graficas-nvidia-149?q=Modelo-GeForce+RTX+5080&order=product.price.asc",
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


# "2.199,95 €", "1.999 95 €" (Coolmod), "1999 €", "2 199,95 €"
PRECIO_RE = re.compile(r"(?<![\d.,])(\d{1,2}(?:[.\s\u00a0]\d{3})+|\d{3,4})(?:[,\s\u00a0](\d{2}))?\s*€")


def precios(txt):
    out = []
    for m in PRECIO_RE.finditer(txt or ""):
        entero = re.sub(r"\D", "", m.group(1))
        out.append(float(f"{entero}.{m.group(2) or '00'}"))
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
    if any(s in t for s in ("un momento", "just a moment", "access denied", "attention required", "robot",
                            "verificando", "checking your browser", "security check", "captcha", "pardon our interruption")):
        return True
    return page.locator('form[action*="validateCaptcha"], #challenge-form, #cf-challenge-running, '
                        'iframe[src*="challenges.cloudflare.com"]:not([style*="display: none"]), #px-captcha, '
                        'iframe[src*="captcha-delivery.com"]').count() > 0


SIN_STOCK = re.compile(r"agotad|sin stock|sin unidades|fuera de stock|no disponible|av[ií]same|notif[ií]came|"
                       r"pr[oó]ximamente|pre-?venta|reserva|bajo pedido", re.I)


# ---------- listado ----------
JS_LISTADO = r"""() => [...document.querySelectorAll('a[href]')].map(a => {
  const img = a.querySelector('img');
  const own = [a.innerText||'', a.title||'', a.getAttribute('aria-label')||'', img ? img.alt : ''].join(' ');
  // sube hasta la "tarjeta" del producto: primer ancestro con precio que no contenga otros productos
  let n = a, t = a.innerText || '';
  for (let i = 0; i < 8 && n.parentElement; i++) {
    const p = n.parentElement, pt = p.innerText || '';
    if ((pt.match(/RTX\s*5080/gi) || []).length > 3 || pt.length > 1500) break;
    n = p; t = pt;
    if (/\d\s*€/.test(t) && /article|product|card|item|miniature/i.test((n.tagName + ' ' + n.className).toString())) break;
  }
  return {href: a.href, own: own.slice(0, 300), text: t.slice(0, 800)};
})"""


def esperar_antibot(page, max_s=30):
    """Los retos tipo Cloudflare ("Un momento…") se resuelven solos en un navegador real: se espera."""
    t = time.time()
    while bloqueado(page) and time.time() - t < max_s:
        page.wait_for_timeout(1000)
    if bloqueado(page):
        raise RuntimeError("BLOQUEADO (antibot/captcha)")


def aceptar_cookies(page):
    for sel in ["#onetrust-accept-btn-handler", "#didomi-notice-agree-button", "#sp-cc-accept",
                "button:has-text('Aceptar todas')", "button:has-text('Aceptar todo')", "button:has-text('Aceptar')"]:
        try:
            b = page.locator(sel).first
            if b.is_visible(timeout=300):
                b.click(timeout=1500)
                page.wait_for_timeout(500)
                return
        except Exception:
            pass


def candidatos(page, url, tope=None):
    tope = tope or PRECIO_MAX
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)
    esperar_antibot(page)
    aceptar_cookies(page)
    page.wait_for_timeout(1500)
    for _ in range(10):  # baja hasta el final para que carguen todos los productos (carga perezosa)
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(500)
    res = {}
    for it in page.evaluate(JS_LISTADO):
        if not es_rtx5080(it["own"]) or not it["href"].startswith("http") or len(urlsplit(it["href"]).path) < 3:
            continue
        if not re.search(r"16\s?GB|GDDR7|tarjeta|gr[aá]fica|graphics", it["own"], re.I):
            continue  # enlace de filtro/menú, no un producto
        ps = precios(it["text"])
        if not ps or len(set(ps)) > 3:  # sin precio, o es un filtro/menú que abarca varios productos
            continue
        u = normalizar(it["href"])
        if u in res:
            continue
        res[u] = {"url": u, "precio_listado": min(ps), "titulo": " ".join(it["own"].split())[:150],
                  "texto": it["text"], "agotado_listado": bool(SIN_STOCK.search(it["text"]))}
    return [c for c in res.values() if c["precio_listado"] <= tope * 1.15]  # margen: el listado a veces no aplica descuentos


# ---------- ficha de producto: modelo, precio, stock ----------
JS_LDJSON = r"""() => { const out = [];
  const walk = o => { if (!o || typeof o !== 'object') return; if (Array.isArray(o)) { o.forEach(walk); return; }
    if ([].concat(o['@type'] || []).includes('Product')) out.push({name: o.name, offers: o.offers});
    if (o['@graph']) walk(o['@graph']); };
  document.querySelectorAll('script[type="application/ld+json"]').forEach(s => { try { walk(JSON.parse(s.textContent)) } catch (e) {} });
  return out; }"""

JS_BOTON = r"""() => { const re = /añadir al carrito|añadir a la cesta|añadir a cesta|comprar ahora|^comprar$|add to cart/i;
  const h1 = document.querySelector('h1');
  const ajeno = 'header, nav, footer, [class*="product-list"], [class*="miniature"], [class*="carousel"], ' +
                '[class*="swiper"], [class*="slider"], [class*="related"], [class*="accessor"], [class*="upsell"], [class*="cross"]';
  const cands = [...document.querySelectorAll('button, input[type=submit], a')].filter(b => {
    const t = (b.innerText || b.value || '').trim(); if (!re.test(t) || t.length > 40) return false;
    const r = b.getBoundingClientRect(); if (r.width === 0 || r.height === 0) return false;
    if (b.closest(ajeno)) return false;
    return !h1 || (h1.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING); });
  const b = cands[0]; if (!b) return false;  // el botón principal es el primero tras el título
  const cs = getComputedStyle(b);
  return !b.disabled && b.getAttribute('aria-disabled') !== 'true' && cs.visibility !== 'hidden'
    && cs.pointerEvents !== 'none' && !/disabled|agotado|out-of-stock/i.test(String(b.className)); }"""

JS_ZONA = r"""() => { let n = document.querySelector('h1'); if (!n) return '';
  while (n.parentElement && (n.innerText || '').length < 1500) n = n.parentElement;
  return (n.innerText || '').slice(0, 6000); }"""

CON_STOCK_LISTADO = re.compile(r"entrega (hoy|mañana|en \d+[^|\n]{0,20})|recíbelo [^|\n]{0,25}|en stock|"
                               r"env[ií]o (en|inmediato)[^|\n]{0,20}|disponible", re.I)


def verificar_generico(page):
    prods = page.evaluate(JS_LDJSON)
    try:
        titulo = page.locator("h1").first.inner_text(timeout=3000).strip()
    except Exception:
        titulo = ""
    titulo = titulo or (prods[0].get("name") if prods else "") or ""
    if not MODELO.search(titulo):
        return {"titulo": titulo, "precio": 0.0, "stock": False, "motivo": "la ficha no es del producto", "vendedor": ""}
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
    elif re.search(r"InStock|LimitedAvailability|OnlineOnly", disp_txt) and not re.search(r"OutOfStock|SoldOut", disp_txt) \
            and not negativo:
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
    page.wait_for_timeout(2000)
    esperar_antibot(page)
    aceptar_cookies(page)
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


def ciclo(browser, prueba=False, tope=None):
    """tope: precio hasta el que se buscan y verifican tarjetas (por defecto PRECIO_MAX).
    Solo se avisa de las que están a <= PRECIO_MAX. Devuelve un resumen por tienda."""
    tope = tope or PRECIO_MAX
    viejo = json.loads(SEEN.read_text()) if SEEN.exists() else {}
    nuevo, alertas, fallidas, resumen = {}, [], set(), {}
    for nombre, url in tiendas(tope).items():
        page = browser.new_page()
        r = resumen[nombre] = {"estado": "OK", "candidatas": 0, "con_stock": []}
        try:
            try:
                cands = candidatos(page, url, tope)
            except RuntimeError as e:
                if "BLOQUEADO" not in str(e) or not browser.resolver_antibot(url):
                    raise
                page = browser.new_page()
                cands = candidatos(page, url, tope)
            r["candidatas"] = len(cands)
            ficha_bloqueada = False
            disponibles = [c for c in cands if not c["agotado_listado"]]
            abiertas, descartadas = 0, 0
            for c in sorted(disponibles, key=lambda x: x["precio_listado"]):
                # se recorre de más barata a más cara hasta tener MAX_VERIFICAR con stock (o abrir MAX_FICHAS fichas)
                if len(r["con_stock"]) >= MAX_VERIFICAR or abiertas >= MAX_FICHAS:
                    break
                abiertas += 1
                v = None
                if not ficha_bloqueada:
                    try:
                        v = verificar(page, c["url"])
                    except Exception as e:
                        if "BLOQUEADO" in str(e) and browser.resolver_antibot(c["url"]):
                            page = browser.new_page()
                            try:
                                v = verificar(page, c["url"])
                            except Exception as e2:
                                e = e2
                        if v is None:
                            ficha_bloqueada = "BLOQUEADO" in str(e)
                if v is None:
                    # sin ficha: solo vale si el listado muestra un plazo de entrega real y no pone agotado
                    m = CON_STOCK_LISTADO.search(c["texto"])
                    ok = bool(m) and not SIN_STOCK.search(c["texto"])
                    v = {"titulo": c["titulo"], "precio": None, "stock": ok, "vendedor": "",
                         "motivo": f"según el listado: '{m.group(0)}' (ficha no verificable)" if ok
                         else "listado sin plazo de entrega -> se asume sin stock"}
                if v.get("precio") is not None and not MODELO.search(v["titulo"] or ""):
                    # la "ficha" no es este producto (redirige a una categoría, producto retirado...)
                    descartadas += 1
                    continue
                if not MODELO.search(v["titulo"] or ""):
                    v["titulo"] = c["titulo"]
                precio = v["precio"] or c["precio_listado"]
                if not v["precio"] and c["precio_listado"] > tope:
                    continue
                modelo_ok = es_rtx5080(v["titulo"])
                valida = modelo_ok and v["stock"] and PRECIO_MIN <= precio <= tope
                if not valida:
                    descartadas += 1
                    continue
                if valida:
                    h = {"tienda": nombre, "precio": precio, "url": c["url"], "titulo": v["titulo"],
                         "motivo": v["motivo"], "vendedor": v["vendedor"]}
                    r["con_stock"].append(h)
                    if precio <= PRECIO_MAX:
                        nuevo[c["url"]] = h
                        if c["url"] not in viejo or precio < viejo[c["url"]]["precio"]:
                            alertas.append(h)
            r["con_stock"].sort(key=lambda h: h["precio"])
            agotadas = len(cands) - len(disponibles) + descartadas
            print(f"[{nombre}] {len(r['con_stock'])} RTX 5080 con stock <= {tope:.0f}€"
                  + (f" (ignoradas {agotadas} agotadas/no comprables)" if agotadas else ""))
            for h in r["con_stock"]:
                print(f"   {h['precio']:8.2f}€ | {h['titulo'][:75]}{' | ' + h['vendedor'] if h['vendedor'] else ''}")
        except Exception as e:
            fallidas.add(nombre)
            r["estado"] = "ERROR: " + str(e).splitlines()[0][:120]
            print(f"[{nombre}] {r['estado']}")
        finally:
            try:
                page.close()
            except Exception:
                pass
    if prueba:
        return resumen
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
    return resumen


# ---------- informe diario ----------
def toca_informe():
    """GitHub programa el informe a dos horas UTC (verano/invierno); solo se envía la que cae ~20:00 en España."""
    cron = os.getenv("CRON", "").split()
    if len(cron) < 2:
        return True  # lanzado a mano
    from datetime import datetime
    from zoneinfo import ZoneInfo
    desfase = int(datetime.now(ZoneInfo("Europe/Madrid")).utcoffset().total_seconds() // 3600)
    return int(cron[1]) + desfase == 19  # cron a las XX:50 UTC -> 19:50 en España, llega sobre las 20:00


def comprobaciones_24h():
    """Cuenta las ejecuciones del workflow de vigilancia en las últimas 24 h (solo en GitHub Actions)."""
    token, repo = os.getenv("GITHUB_TOKEN"), os.getenv("GITHUB_REPOSITORY")
    if not token or not repo:
        return None
    import urllib.request
    from datetime import datetime, timedelta, timezone
    desde = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"https://api.github.com/repos/{repo}/actions/workflows/check.yml/runs?per_page=100&created=%3E%3D{desde}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
                                               "Accept": "application/vnd.github+json"})
    try:
        runs = json.load(urllib.request.urlopen(req, timeout=20)).get("workflow_runs", [])
    except Exception as e:
        return f"no se pudo consultar ({e})"
    ok = sum(r["conclusion"] == "success" for r in runs)
    fallos = sum(r["conclusion"] not in ("success", None, "cancelled", "skipped") for r in runs)
    ultima = runs[0]["created_at"].replace("T", " ").replace("Z", " UTC") if runs else "ninguna"
    return f"{len(runs)} ejecuciones ({ok} correctas, {fallos} con fallo). Última: {ultima}"


def informe(browser):
    tope = float(os.getenv("INFORME_TOPE", "3500"))
    resumen = ciclo(browser, prueba=True, tope=tope)
    todas = sorted((h for r in resumen.values() for h in r["con_stock"]), key=lambda h: h["precio"])
    errores = [n for n, r in resumen.items() if r["estado"] != "OK"]
    bajo = [h for h in todas if h["precio"] <= PRECIO_MAX]

    lineas = [f"El bot está funcionando. Límite de aviso: {PRECIO_MAX:.0f} €.", ""]
    c24 = comprobaciones_24h()
    if c24:
        lineas += [f"Comprobaciones automáticas en las últimas 24 h: {c24}", ""]
    if bajo:
        lineas += [f"⚠️ HAY {len(bajo)} RTX 5080 CON STOCK A ≤ {PRECIO_MAX:.0f} € AHORA MISMO (ya te habrá llegado el aviso):", ""]
    lineas += [f"Las {MAX_VERIFICAR} RTX 5080 más baratas CON STOCK de cada tienda (de menor a mayor):"]
    for n, r in resumen.items():
        lineas.append("")
        if r["estado"] != "OK":
            lineas.append(f"✗ {n}: {r['estado']}")
            continue
        top = sorted(r["con_stock"], key=lambda h: h["precio"])[:MAX_VERIFICAR]
        if not top:
            lineas.append(f"✓ {n}: leída bien, ninguna RTX 5080 con stock entre sus {MAX_VERIFICAR} más baratas")
            continue
        lineas.append(f"✓ {n}:")
        lineas += [f"   {h['precio']:8.2f} € · {h['titulo'][:75]}\n              {h['url']}" for h in top]
    if errores:
        lineas += ["", f"Ojo: {', '.join(errores)} ha(n) fallado en esta pasada. Si se repite varios días, "
                       "puede que la tienda haya cambiado su web o bloquee al bot."]

    mas_barata = f"la más barata {todas[0]['precio']:.0f} €" if todas else "sin RTX 5080 con stock"
    estado = "TODO OK" if not errores else f"{len(errores)} tienda(s) con error"
    asunto = f"Check diario RTX 5080: {estado} · {mas_barata}"
    cuerpo = "\n".join(lineas)
    print(asunto + "\n" + cuerpo)
    enviar_email(asunto, cuerpo)


# Cada tipo de ejecución usa su propio perfil: Chrome no permite dos procesos con el mismo perfil a la vez
PERFIL = BASE / (".perfil-chrome" if ("--loop" in sys.argv or "--duracion" in sys.argv) else ".perfil-chrome-manual")
PUEDE_VENTANA = sys.platform == "darwin" or bool(os.getenv("DISPLAY"))


class Navegador:
    """Google Chrome real con un perfil propio, que guarda las cookies que dan los antibot (Cloudflare...).
    Funciona sin ventana (headless). Si una tienda bloquea, abre un momento Chrome con ventana (fuera de la
    pantalla), que pasa los retos más duros, guarda la cookie y vuelve al modo sin ventana."""

    def __init__(self, p):
        self.p, self.ctx, self.ua = p, None, None
        self.abrir(ventana=os.getenv("VENTANA", "0") == "1")

    def _lanzar(self, ventana, ua=None):
        args = ["--disable-blink-features=AutomationControlled"]
        if ventana:
            args += ["--window-position=-2400,-2400", "--window-size=1366,900"]
        op = dict(headless=not ventana, args=args, locale="es-ES", viewport={"width": 1366, "height": 900},
                  ignore_default_args=["--enable-automation"])
        if ua:
            op["user_agent"] = ua
        try:
            return self.p.chromium.launch_persistent_context(str(PERFIL), channel="chrome", **op)
        except Exception as e:
            if "ProcessSingleton" in str(e) or "SingletonLock" in str(e):
                raise RuntimeError("el perfil de Chrome está en uso por otro proceso del bot") from None
            print(f"Aviso: no se pudo abrir Google Chrome ({str(e).splitlines()[0][:80]}); uso Chromium")
            return self.p.chromium.launch_persistent_context(str(PERFIL), **op)

    def abrir(self, ventana):
        self.ventana = ventana
        ctx = self._lanzar(ventana)
        if not ventana:
            # el user-agent de Chrome headless dice "HeadlessChrome": se usa el de un Chrome normal, misma versión
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            self.ua = pg.evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
            ctx.close()
            ctx = self._lanzar(False, self.ua)
        ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        for pg in ctx.pages[1:]:
            pg.close()
        self.ctx = ctx

    def new_page(self):
        return self.ctx.new_page()

    def resolver_antibot(self, url):
        """Abre la URL con ventana para superar el reto antibot. Devuelve True si lo consigue."""
        if self.ventana or not PUEDE_VENTANA:
            return False
        print(f"   ... reto antibot: lo resuelvo con una ventana de Chrome ({urlsplit(url).netloc})")
        self.ctx.close()
        ok = False
        try:
            self.abrir(ventana=True)
            pg = self.new_page()
            pg.goto(url, wait_until="domcontentloaded", timeout=45000)
            esperar_antibot(pg, 45)
            ok = True
        except Exception as e:
            print(f"   ... no se pudo superar: {str(e)[:80]}")
        try:
            self.ctx.close()
        except Exception:
            pass
        self.abrir(ventana=False)
        return ok

    def close(self):
        self.ctx.close()


def arg(nombre, defecto=None):
    return sys.argv[sys.argv.index(nombre) + 1] if nombre in sys.argv else defecto


if __name__ == "__main__":
    if "--test-email" in sys.argv:
        enviar_email("Test bot RTX 5080", "Si lees esto, los avisos por email funcionan.")
        print("Email de prueba enviado")
        sys.exit()
    if "--informe" in sys.argv and not toca_informe():
        print("Esta hora programada no corresponde a las 20:00 en España (cambio de horario): no se envía.")
        sys.exit()
    prueba = arg("--prueba")
    if prueba:
        PRECIO_MAX = float(prueba)
    loop, duracion = int(arg("--loop", 0)), int(arg("--duracion", 0))
    inicio = time.time()
    with sync_playwright() as p:
        browser = Navegador(p)
        if "--informe" in sys.argv:
            informe(browser)
            sys.exit()
        while True:
            print(time.strftime("== %Y-%m-%d %H:%M:%S =="), flush=True)
            ciclo(browser, prueba=bool(prueba))
            sys.stdout.flush()
            if prueba or (not loop and not duracion) or (duracion and time.time() - inicio > duracion - 60):
                break
            time.sleep(loop or 20)
