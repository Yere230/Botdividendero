"""Bot de dividendos (Bolsa de Santiago) que avisa por Telegram. Versión 2.

Fuentes: calendario público de dividendos de Rankia Chile (fecha límite,
fecha de pago, monto) y Yahoo Finance como base y respaldo. La lista de
acciones está en acciones.txt.
"""
import datetime as dt
import html
import json
import os
import re
import time

import requests
import yfinance as yf

ARCHIVO_ESTADO = "estado.json"
ARCHIVO_ACCIONES = "acciones.txt"
DIAS_ATRAS = 10      # solo se avisan dividendos con fecha límite de los últimos N días o futura
DIAS_RESUMEN = 14    # ventana del resumen semanal
URL_CALENDARIO = "https://www.rankia.cl/blog/analisis-ipsa/4082930-calendario-dividendos-bolsa-santiago"

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
MODO_PRUEBA = os.environ.get("MODO_PRUEBA") == "1"
FORZAR_RESUMEN = os.environ.get("FORZAR_RESUMEN") == "1"


# ---------------------------------------------------------------- Telegram
def enviar(texto):
    """Envía un mensaje a Telegram. Devuelve True si salió bien."""
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": texto,
                  "disable_web_page_preview": True},
            timeout=30,
        )
        if r.status_code != 200:
            print("Error de Telegram:", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        print("No se pudo enviar el mensaje:", type(e).__name__)
        return False


def enviar_largo(texto):
    """Parte los mensajes largos en varios para respetar el límite de Telegram."""
    ok, bloque = True, ""
    for linea in texto.split("\n"):
        if len(bloque) + len(linea) + 1 > 3800:
            ok = enviar(bloque) and ok
            bloque = ""
        bloque += linea + "\n"
    if bloque.strip():
        ok = enviar(bloque) and ok
    return ok


# ---------------------------------------------------------------- Utilidades
def a_fecha(valor):
    """Convierte distintos formatos de fecha (Yahoo) a datetime.date."""
    if valor is None:
        return None
    if isinstance(valor, dt.datetime):
        return valor.date()
    if isinstance(valor, dt.date):
        return valor
    if isinstance(valor, (int, float)):
        return dt.datetime.fromtimestamp(valor, dt.timezone.utc).date()
    return None


def texto_a_fecha(v):
    """Convierte textos como 2026-10-07, 07-10-2026 o 07/10/2026 a fecha."""
    if v is None:
        return None
    s = str(v).strip()[:10]
    for formato in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(s, formato).date()
        except ValueError:
            pass
    return None


def a_numero(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        try:
            return float(str(v).replace(".", "").replace(",", "."))
        except ValueError:
            return None


def miles_cl(s):
    """Formato chileno: punto para miles, coma para decimales."""
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_fecha(f):
    return f.strftime("%d-%m-%Y")


def fmt_monto(m, moneda="$", decimales=None):
    if decimales is None:
        s = f"{m:,.4f}".rstrip("0").rstrip(".")
    else:
        s = f"{m:,.{decimales}f}"
    return f"{moneda}{miles_cl(s)}"


def fmt_cantidad(c):
    return miles_cl(f"{c:,.0f}")


def normalizar_moneda(texto):
    t = str(texto or "").upper()
    if "US" in t or "DOL" in t:
        return "US$"
    return "$"


def buscar_clave(d, *partes):
    """Busca en un diccionario la primera clave que contenga todos los textos dados."""
    for k in d:
        kl = str(k).lower()
        if all(p in kl for p in partes):
            return k
    return None


# ---------------------------------------------------------------- Configuración
def leer_acciones():
    """Lee acciones.txt (un nemotécnico por línea; el .SN se agrega solo)."""
    lista = []
    try:
        with open(ARCHIVO_ACCIONES, "r", encoding="utf-8") as f:
            for linea in f:
                t = linea.split("#")[0].strip().upper()
                if not t:
                    continue
                if not t.endswith(".SN"):
                    t += ".SN"
                if t not in lista:
                    lista.append(t)
    except FileNotFoundError:
        print("No existe acciones.txt")
    return lista


def leer_mis_acciones():
    """Lee el secreto MIS_ACCIONES con el formato CHILE=10000,BCI=50."""
    texto = os.environ.get("MIS_ACCIONES", "").strip()
    resultado = {}
    for par in texto.replace(";", ",").replace("\n", ",").split(","):
        if "=" not in par:
            continue
        nombre, _, cantidad = par.partition("=")
        nombre = nombre.strip().upper().replace(".SN", "")
        try:
            resultado[nombre] = float(cantidad.strip())
        except ValueError:
            print("Cantidad no válida para", nombre)
    return resultado


def cargar_estado():
    try:
        with open(ARCHIVO_ESTADO, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"enviados": {}}


def guardar_estado(estado):
    estado["ultima_revision"] = dt.date.today().isoformat()
    with open(ARCHIVO_ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- Fuente 1: calendario de dividendos (Rankia)
def limpiar_celda(texto):
    texto = re.sub(r"<[^>]+>", "", texto)
    return html.unescape(texto).replace("\xa0", " ").strip()


def leer_calendario():
    """Lee la tabla pública del calendario de dividendos de Rankia Chile
    (basada en los anuncios de la Bolsa de Santiago).

    Es una página de terceros: puede fallar o cambiar. Devuelve
    ({NEMO: [eventos]}, texto_de_diagnóstico).
    """
    try:
        r = requests.get(URL_CALENDARIO, timeout=30, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
            "Accept-Language": "es-CL,es;q=0.9",
        })
    except Exception as e:
        return {}, f"error de conexión ({type(e).__name__})"
    if r.status_code != 200:
        return {}, f"la página respondió con código {r.status_code}"

    eventos, primera = {}, None
    for fila in re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.S | re.I):
        celdas = [limpiar_celda(c)
                  for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", fila, re.S | re.I)]
        if len(celdas) < 4:
            continue
        m = re.search(r"\(([A-Za-z0-9\-]+)\)\s*$", celdas[0])
        limite = texto_a_fecha(celdas[1])
        if not m or limite is None:
            continue
        if primera is None:
            primera = " | ".join(celdas)[:200]
        eventos.setdefault(m.group(1).upper(), []).append({
            "limite": limite,
            "pago": texto_a_fecha(celdas[3]),
            "monto": a_numero(celdas[2]),
        })

    if not eventos:
        return {}, "no encontré la tabla de dividendos (puede haber cambiado la página)"
    total = sum(len(v) for v in eventos.values())
    return eventos, f"{total} dividendos leídos del calendario. Ejemplo: {primera}"


# ---------------------------------------------------------------- Fuente 2: Yahoo Finance
def consultar_yahoo(ticker):
    t = yf.Ticker(ticker)
    monto = fecha_monto = ex = pago = None

    try:
        div = t.dividends
        if div is not None and len(div) > 0:
            fecha_monto = a_fecha(div.index[-1])
            monto = float(div.iloc[-1])
    except Exception as e:
        print(ticker, "dividendos:", type(e).__name__)

    try:
        cal = t.calendar
        if isinstance(cal, dict):
            ex = a_fecha(cal.get("Ex-Dividend Date"))
            pago = a_fecha(cal.get("Dividend Date"))
    except Exception as e:
        print(ticker, "calendario:", type(e).__name__)

    try:
        info = t.info
        if ex is None:
            ex = a_fecha(info.get("exDividendDate"))
        if pago is None:
            pago = a_fecha(info.get("dividendDate"))
    except Exception as e:
        print(ticker, "info:", type(e).__name__)

    fechas = [f for f in (ex, fecha_monto) if f is not None]
    ex_final = max(fechas) if fechas else None
    if pago is not None and ex_final is not None and pago < ex_final:
        pago = None

    return {
        "ex": ex_final,
        "pago": pago,
        "monto": monto,
        "monto_exacto": (fecha_monto is not None and fecha_monto == ex_final),
    }


# ---------------------------------------------------------------- Unir fuentes
def monto_coherente(monto, referencia):
    """Evita mezclar pesos con dólares: compara con el último dividendo de Yahoo (en pesos)."""
    if monto is None:
        return False
    if referencia is None or referencia <= 0:
        return monto >= 1
    return 0.2 <= monto / referencia <= 5


def construir_eventos(acciones, calendario):
    eventos, sin_datos = [], []
    for tk in acciones:
        nemo = tk.replace(".SN", "")
        y = consultar_yahoo(tk)
        time.sleep(1)  # pausa para no saturar a Yahoo
        propios = calendario.get(nemo, [])
        hay_datos = False

        for e in propios:
            hay_datos = True
            nota = None
            if monto_coherente(e["monto"], y["monto"]):
                monto, exacto = e["monto"], True
            else:
                monto, exacto = y["monto"], False
                if e["monto"] is not None:
                    nota = (f"El calendario informa {fmt_monto(e['monto'], '')} por acción "
                            "(moneda por confirmar)")
            eventos.append({"ticker": nemo, "limite": e["limite"], "pago": e["pago"],
                            "monto": monto, "exacto": exacto, "moneda": "$",
                            "fuente": "Calendario", "nota": nota})

        if y["ex"] is not None:
            ya_cubierto = any(abs((e["limite"] - y["ex"]).days) <= 7 for e in propios)
            if not ya_cubierto:
                hay_datos = True
                eventos.append({"ticker": nemo, "limite": y["ex"], "pago": y["pago"],
                                "monto": y["monto"], "exacto": y["monto_exacto"],
                                "moneda": "$", "fuente": "Yahoo", "nota": None})

        if not hay_datos:
            sin_datos.append(tk)
    return eventos, sin_datos


# ---------------------------------------------------------------- Mensajes
def armar_mensaje(ev, es_nuevo, mis, hoy):
    dias = (ev["limite"] - hoy).days
    if dias > 0:
        cuando = f"en {dias} día(s)"
    elif dias == 0:
        cuando = "HOY"
    else:
        cuando = f"hace {-dias} día(s)"

    titulo = "💰 Dividendo" if es_nuevo else "🔔 Actualización de fecha de pago"
    if ev["fuente"] == "Calendario":
        etiqueta = "Fecha límite (calendario de dividendos)"
    else:
        etiqueta = "Fecha ex-dividendo (Yahoo, referencial)"

    lineas = [
        f"{titulo}: {ev['ticker']}",
        f"📅 {etiqueta}: {fmt_fecha(ev['limite'])} ({cuando})",
    ]
    if ev["pago"]:
        lineas.append(f"💵 Fecha de pago: {fmt_fecha(ev['pago'])}")
    else:
        lineas.append("💵 Fecha de pago: aún no informada (confirmar en CMF)")

    if ev["monto"] is not None:
        txt = fmt_monto(ev["monto"], ev["moneda"])
        if ev["exacto"]:
            lineas.append(f"📈 Monto por acción: {txt}")
        else:
            lineas.append(f"📈 Monto de referencia (último dividendo registrado): {txt}")
        cantidad = mis.get(ev["ticker"])
        if cantidad:
            total = ev["monto"] * cantidad
            rotulo = "Recibirías aprox." if ev["exacto"] else "Estimación referencial"
            lineas.append(
                f"🧮 {rotulo}: {fmt_monto(total, ev['moneda'], 0)} "
                f"por tus {fmt_cantidad(cantidad)} acciones (bruto, antes de impuestos)")

    if ev.get("nota"):
        lineas.append(f"ℹ️ {ev['nota']}")
    lineas.append("")
    lineas.append("Confirma siempre en cmfchile.cl o con tu corredora antes de decidir.")
    return "\n".join(lineas)


def armar_resumen(eventos, mis, hoy):
    fin = hoy + dt.timedelta(days=DIAS_RESUMEN)
    filas = []
    for ev in eventos:
        relevantes = []
        if hoy <= ev["limite"] <= fin:
            relevantes.append(ev["limite"])
        if ev["pago"] and hoy <= ev["pago"] <= fin:
            relevantes.append(ev["pago"])
        if relevantes:
            filas.append((min(relevantes), ev))
    filas.sort(key=lambda x: (x[0], x[1]["ticker"]))

    lineas = [f"🗓️ Resumen semanal de dividendos (próximos {DIAS_RESUMEN} días)", ""]
    if not filas:
        lineas.append("No hay dividendos anunciados en este período para tus acciones.")
        return "\n".join(lineas)

    total_clp, hay_total = 0.0, False
    for _, ev in filas:
        linea = f"• {ev['ticker']}: límite {fmt_fecha(ev['limite'])}"
        linea += f" · pago {fmt_fecha(ev['pago'])}" if ev["pago"] else " · pago por confirmar"
        if ev["monto"] is not None:
            linea += f" · {fmt_monto(ev['monto'], ev['moneda'])}"
            if not ev["exacto"]:
                linea += " (ref.)"
            cantidad = mis.get(ev["ticker"])
            if cantidad:
                total = ev["monto"] * cantidad
                linea += f" → ~{fmt_monto(total, ev['moneda'], 0)}"
                if ev["moneda"] == "$":
                    total_clp += total
                    hay_total = True
        lineas.append(linea)

    if hay_total:
        lineas.append("")
        lineas.append(f"🧮 Total estimado en pesos: {fmt_monto(total_clp, '$', 0)} (bruto, antes de impuestos)")
    lineas.append("")
    lineas.append("(ref.) = monto de referencia, aún no confirmado.")
    return "\n".join(lineas)


# ---------------------------------------------------------------- Avisos
def ya_avisado(enviados, ticker, fecha):
    """Busca un aviso previo del mismo ticker con fecha cercana (±7 días)."""
    for clave, valor in enviados.items():
        t, _, f = clave.partition("|")
        if t == ticker + ".SN":
            fk = texto_a_fecha(f)
            if fk and abs((fk - fecha).days) <= 7:
                return clave, valor
    return None, None


def calcular_avisos(eventos, enviados, hoy, mis):
    minimo = hoy - dt.timedelta(days=DIAS_ATRAS)
    pendientes = []
    for ev in eventos:
        if ev["limite"] < minimo:
            continue
        pago_txt = ev["pago"].isoformat() if ev["pago"] else None
        clave_previa, previo = ya_avisado(enviados, ev["ticker"], ev["limite"])
        if previo is None:
            clave = f"{ev['ticker']}.SN|{ev['limite'].isoformat()}"
            pendientes.append((clave, pago_txt, armar_mensaje(ev, True, mis, hoy)))
        elif previo.get("pago") is None and pago_txt:
            pendientes.append((clave_previa, pago_txt, armar_mensaje(ev, False, mis, hoy)))
    return pendientes


# ---------------------------------------------------------------- Programa principal
def main():
    hoy = dt.date.today()
    acciones = leer_acciones()
    if not acciones:
        enviar("⚠️ No encontré acciones en acciones.txt. Revisa que el archivo exista y tenga al menos una.")
        return

    mis = leer_mis_acciones()
    estado = cargar_estado()
    enviados = estado.setdefault("enviados", {})

    calendario, diag_calendario = leer_calendario()
    eventos, sin_datos = construir_eventos(acciones, calendario)
    pendientes = calcular_avisos(eventos, enviados, hoy, mis)

    if MODO_PRUEBA:
        de_calendario = sum(1 for e in eventos if e["fuente"] == "Calendario")
        de_yahoo = sum(1 for e in eventos if e["fuente"] == "Yahoo")
        texto = "✅ El bot de dividendos funciona y puede escribirte.\n\n"
        texto += f"Acciones en tu lista: {len(acciones)}\n"
        texto += f"Sin datos en ninguna fuente ({len(sin_datos)}): {', '.join(sin_datos) or 'ninguna'}\n\n"
        texto += f"📅 Calendario de dividendos (Rankia): {diag_calendario}\n"
        texto += f"Eventos tomados del calendario: {de_calendario}\n"
        texto += f"Eventos tomados de Yahoo: {de_yahoo}\n\n"
        texto += f"🧮 Acciones con cantidad configurada: {', '.join(sorted(mis)) or 'ninguna'}\n"
        texto += f"📬 Avisos que se enviarían ahora: {len(pendientes)}"
        enviar_largo(texto)
        if FORZAR_RESUMEN:
            enviar_largo(armar_resumen(eventos, mis, hoy))
        return

    for clave, pago_txt, mensaje in pendientes:
        if enviar(mensaje):
            enviados[clave] = {"pago": pago_txt}

    if hoy.weekday() == 0 or FORZAR_RESUMEN:   # lunes
        enviar_largo(armar_resumen(eventos, mis, hoy))

    guardar_estado(estado)
    print(f"Listo. Eventos: {len(eventos)}, sin datos: {len(sin_datos)}, avisos: {len(pendientes)}")


if __name__ == "__main__":
    main()
