"""Bot de dividendos (Bolsa de Santiago) que avisa por Telegram."""
import datetime as dt
import json
import os
import time

import requests
import yfinance as yf

# ============ AQUÍ EDITAS TU LISTA DE ACCIONES ============
# Formato: NEMOTECNICO.SN  (el .SN indica Bolsa de Santiago en Yahoo)
ACCIONES = [
    "CHILE.SN", "BCI.SN", "BSANTANDER.SN", "COPEC.SN", "CMPC.SN",
    "ENELAM.SN", "ENELCHILE.SN", "COLBUN.SN", "CCU.SN", "CENCOSUD.SN",
    "FALABELLA.SN", "ITAUCL.SN", "AGUAS-A.SN", "ANDINA-B.SN", "ENTEL.SN",
    "PARAUCO.SN", "MALLPLAZA.SN", "VAPORES.SN", "SQM-B.SN", "CAP.SN",
    "ILC.SN", "ZOFRI.SN", "SMU.SN", "QUINENCO.SN", "CFINRENTAS.SN",
    "ENTEL.SN", "HABITAT.SN", "CFMITNIPSA.SN", "LTM.SN", "IAM.SN",
    "CONCHATORO.SN", "BICE.SN", "CENCOMALLS.SN", "ENELGXCH.SN", "ENELAM.SN",
    "LIPIGAS.SN", "RIPLEY.SN", "GASCO.SN", "SALFACORP.SN", "SONDA.SN",
]
# Solo se avisan dividendos cuya fecha ex sea de los últimos N días o futura
DIAS_ATRAS = 10
# ==========================================================

ARCHIVO_ESTADO = "estado.json"
TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
MODO_PRUEBA = os.environ.get("MODO_PRUEBA") == "1"


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


def a_fecha(valor):
    """Convierte distintos formatos de fecha a datetime.date."""
    if valor is None:
        return None
    if isinstance(valor, dt.datetime):
        return valor.date()
    if isinstance(valor, dt.date):
        return valor
    if isinstance(valor, (int, float)):
        return dt.datetime.fromtimestamp(valor, dt.timezone.utc).date()
    return None


def fmt_fecha(f):
    return f.strftime("%d-%m-%Y")


def fmt_monto(m):
    s = f"{m:,.2f}"
    return "$" + s.replace(",", "X").replace(".", ",").replace("X", ".")


def consultar(ticker):
    """Pide a Yahoo Finance los datos de dividendos de una acción."""
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
    # La fecha de pago solo sirve si corresponde a este mismo dividendo
    if pago is not None and ex_final is not None and pago < ex_final:
        pago = None

    return {
        "ticker": ticker,
        "ex": ex_final,
        "pago": pago,
        "monto": monto,
        "monto_exacto": (fecha_monto is not None and fecha_monto == ex_final),
    }


def armar_mensaje(d, es_nuevo):
    hoy = dt.date.today()
    nombre = d["ticker"].replace(".SN", "")
    dias = (d["ex"] - hoy).days
    if dias > 0:
        cuando = f"en {dias} día(s)"
    elif dias == 0:
        cuando = "HOY"
    else:
        cuando = f"hace {-dias} día(s)"

    titulo = "💰 Dividendo" if es_nuevo else "🔔 Actualización de fecha de pago"
    lineas = [
        f"{titulo}: {nombre}",
        f"📅 Fecha ex-dividendo (corte, según Yahoo): {fmt_fecha(d['ex'])} ({cuando})",
    ]
    if d["pago"]:
        lineas.append(f"💵 Fecha de pago: {fmt_fecha(d['pago'])}")
    else:
        lineas.append("💵 Fecha de pago: no disponible en Yahoo (confirmar en CMF)")
    if d["monto"] is not None:
        if d["monto_exacto"]:
            lineas.append(f"📈 Monto por acción: {fmt_monto(d['monto'])}")
        else:
            lineas.append(
                f"📈 Monto de referencia (último dividendo registrado): {fmt_monto(d['monto'])}"
            )
    lineas.append("")
    lineas.append("Confirma siempre en cmfchile.cl o en la Bolsa de Santiago antes de decidir.")
    return "\n".join(lineas)


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


def main():
    estado = cargar_estado()
    enviados = estado.setdefault("enviados", {})
    hoy = dt.date.today()
    limite = hoy - dt.timedelta(days=DIAS_ATRAS)

    con_datos, sin_datos, pendientes = [], [], []

    for tk in ACCIONES:
        d = consultar(tk)
        time.sleep(1)  # pausa para no saturar a Yahoo
        if d["ex"] is None:
            sin_datos.append(tk)
            continue
        con_datos.append(tk)
        if d["ex"] < limite:
            continue

        clave = f"{tk}|{d['ex'].isoformat()}"
        pago_txt = d["pago"].isoformat() if d["pago"] else None
        previo = enviados.get(clave)
        if previo is None:
            pendientes.append((clave, pago_txt, armar_mensaje(d, True)))
        elif previo.get("pago") is None and pago_txt:
            pendientes.append((clave, pago_txt, armar_mensaje(d, False)))

    if MODO_PRUEBA:
        texto = "✅ El bot de dividendos funciona y puede escribirte.\n\n"
        texto += f"Acciones con datos en Yahoo ({len(con_datos)}): " + (", ".join(con_datos) or "ninguna")
        texto += f"\n\nAcciones SIN datos ({len(sin_datos)}): " + (", ".join(sin_datos) or "ninguna")
        texto += f"\n\nAvisos que se enviarían ahora: {len(pendientes)}"
        enviar(texto)
        return

    for clave, pago_txt, mensaje in pendientes:
        if enviar(mensaje):
            enviados[clave] = {"pago": pago_txt}

    guardar_estado(estado)
    print(f"Listo. Con datos: {len(con_datos)}, sin datos: {len(sin_datos)}, avisos: {len(pendientes)}")


if __name__ == "__main__":
    main()
