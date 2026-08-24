#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Panel macro Argentina - descarga y consolidacion de series.

Corre una vez por dia (GitHub Actions, 11:00 hora Argentina).
Escribe:
    datos/series.csv        historico consolidado, una fila por fecha
    datos/granos.csv        pizarra Rosario acumulada dia a dia
    datos/incidencias.csv   registro de fuentes que fallaron
    index.html              el panel, con los datos embebidos

Principio: si una fuente falla, se conserva lo que ya habia y se registra
la incidencia. Nunca se escribe un cero donde falta un dato.
"""

import io
import json
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

RAIZ = Path(__file__).resolve().parent
DATOS = RAIZ / "datos"
DATOS.mkdir(exist_ok=True)

TZ_AR = timezone(timedelta(hours=-3))
HOY = datetime.now(TZ_AR).date()

UA = {"User-Agent": "Mozilla/5.0 (compatible; panel-macro/1.0)"}

incidencias = []


def avisar(fuente, detalle):
    """Registra una falla sin cortar la corrida."""
    print(f"  [!] {fuente}: {detalle}", file=sys.stderr)
    incidencias.append(
        {"fecha_corrida": HOY.isoformat(), "fuente": fuente, "detalle": str(detalle)[:300]}
    )


def pedir(url, intentos=4, espera=3, **kw):
    ultimo = None
    for n in range(intentos):
        try:
            r = requests.get(url, headers=UA, timeout=45, **kw)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            ultimo = e
            time.sleep(espera * (n + 1))
    raise RuntimeError(f"{url} -> {ultimo}")


# ---------------------------------------------------------------- BCRA

BCRA_BASE = "https://api.bcra.gob.ar/estadisticas/v4.0/monetarias"

# id de variable -> nombre de columna en el panel
SERIES_BCRA = {
    5: "Dolar A3500",
    1: "Reservas internacionales",
    7: "Tasa BADLAR",
    44: "Tasa TAMAR",
    31: "UVA",
    27: "IPC mensual",
    28: "IPC interanual",
}


def bajar_bcra(id_var, desde="2000-01-01"):
    """Devuelve una Serie fecha->valor. Pagina de a 3000 registros."""
    filas, offset, vueltas = [], 0, 0
    vistas = set()
    while vueltas < 40:  # tope de seguridad: 120.000 registros
        vueltas += 1
        url = (
            f"{BCRA_BASE}/{id_var}?desde={desde}&hasta={HOY.isoformat()}"
            f"&limit=3000&offset={offset}"
        )
        js = pedir(url, verify=False).json()
        det = js["results"][0]["detalle"] if js.get("results") else []
        if not det:
            break
        nuevas = [d for d in det if d["fecha"] not in vistas]
        if not nuevas:  # la API ignoro el offset: cortar en vez de girar en falso
            break
        vistas.update(d["fecha"] for d in nuevas)
        filas.extend(nuevas)
        if len(det) < 3000:
            break
        offset += 3000
    if not filas:
        raise RuntimeError("sin datos")
    s = pd.Series(
        {pd.to_datetime(d["fecha"]).date(): float(d["valor"]) for d in filas}
    ).sort_index()
    return s[~s.index.duplicated(keep="last")]


# ---------------------------------------------------------------- BNA


def bajar_bna():
    """Dolar Banco Nacion billete, comprador y vendedor, historico completo."""
    js = pedir("https://api.argentinadatos.com/v1/cotizaciones/dolares/oficial").json()
    comp, vend = {}, {}
    for d in js:
        f = pd.to_datetime(d["fecha"]).date()
        if d.get("compra") is not None:
            comp[f] = float(d["compra"])
        if d.get("venta") is not None:
            vend[f] = float(d["venta"])
    if not comp:
        raise RuntimeError("sin datos")
    return pd.Series(comp).sort_index(), pd.Series(vend).sort_index()


# ---------------------------------------------------------------- Bitcoin


def bajar_btc():
    """Cierre diario BTC/USD. Bitstamp como primaria, Coinbase como respaldo."""
    puntos = {}
    inicio = int(datetime(2011, 8, 1, tzinfo=timezone.utc).timestamp())
    fin = int(datetime.now(timezone.utc).timestamp())
    while inicio < fin:
        url = (
            "https://www.bitstamp.net/api/v2/ohlc/btcusd/"
            f"?step=86400&limit=1000&start={inicio}"
        )
        js = pedir(url).json()
        velas = js.get("data", {}).get("ohlc", [])
        if not velas:
            break
        for v in velas:
            f = datetime.fromtimestamp(int(v["timestamp"]), tz=timezone.utc).date()
            puntos[f] = float(v["close"])
        inicio = int(velas[-1]["timestamp"]) + 86400
        if len(velas) < 1000:
            break
    if not puntos:
        raise RuntimeError("Bitstamp sin datos")
    # el cierre de hoy todavia no existe en OHLC: lo completa el spot de Coinbase
    try:
        spot = pedir("https://api.coinbase.com/v2/prices/BTC-USD/spot").json()
        puntos[HOY] = float(spot["data"]["amount"])
    except Exception as e:  # noqa: BLE001
        avisar("Bitcoin spot Coinbase", e)
    return pd.Series(puntos).sort_index()


# ---------------------------------------------------------------- Granos

GRANOS = ["Trigo", "Maiz", "Girasol", "Soja", "Sorgo"]


def _sin_tildes(t):
    return "".join(
        c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn"
    )


def bajar_pizarra():
    """
    Precios de pizarra de la Camara Arbitral de Cereales (Bolsa de Rosario).
    La pagina publica solo el ultimo dia habil, por eso se acumula en CSV.
    Devuelve un dict con la fecha de la pizarra y los precios en $/t y US$/t.
    """
    html = pedir("https://www.cac.bcr.com.ar/es/precios-de-pizarra").text
    texto = re.sub(r"<[^>]+>", " ", html)
    texto = _sin_tildes(texto.replace("&nbsp;", " "))
    texto = re.sub(r"\s+", " ", texto)

    m = re.search(r"Precios Pizarra del dia\s+(\d{2})/(\d{2})/(\d{4})", texto, re.I)
    if not m:
        raise RuntimeError("no se encontro la fecha de la pizarra")
    f_pizarra = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))

    def num(s):
        # formato argentino: 525.000,00
        return float(s.replace(".", "").replace(",", "."))

    fila = {"fecha": f_pizarra.isoformat()}
    faltan = []
    for g in GRANOS:
        # despues del nombre del grano vienen el precio en pesos y el precio en dolares
        bloque = re.search(
            rf"\b{g}\b(.{{0,160}}?)\$\s*([\d\.]+,\d{{2}})(.{{0,120}}?)US\$\s*(?:\(E\)\s*)?([\d\.]+,\d{{2}})",
            texto,
            re.I | re.S,
        )
        if bloque:
            fila[f"{g} $/t"] = num(bloque.group(2))
            fila[f"{g} US$/t"] = num(bloque.group(4))
        else:
            faltan.append(g)
    if faltan:
        avisar("Pizarra Rosario", f"sin precio para: {', '.join(faltan)}")
    if len(fila) == 1:
        raise RuntimeError("no se pudo leer ningun precio")

    # bonus: el tipo de cambio BNA divisas comprador que publica la misma pagina
    tc = re.search(r"Comprador\s+\d{2}/\d{2}/\d{4}:\s*\$\s*([\d\.]+,\d{2})", texto)
    if tc:
        fila["Dolar BNA divisa comprador"] = num(tc.group(1))
    return fila


def acumular_granos(fila):
    ruta = DATOS / "granos.csv"
    prev = pd.read_csv(ruta) if ruta.exists() else pd.DataFrame()
    nuevo = pd.DataFrame([fila])
    df = pd.concat([prev, nuevo], ignore_index=True)
    df = df.drop_duplicates(subset=["fecha"], keep="last").sort_values("fecha")
    df.to_csv(ruta, index=False)
    return df


# ---------------------------------------------------------------- Bandas cambiarias

# Regla verificada contra valores publicados por el BCRA:
#   - 14/04/2025: arranca el regimen con piso 1000 y techo 1400.
#   - hasta el 31/12/2025: techo +1% mensual y piso -1% mensual, capitalizado
#     por dia corrido.
#   - desde el 02/01/2026: el ajuste mensual pasa a ser el ultimo IPC mensual
#     conocido al inicio del mes (el del mes M-2), repartido por dia corrido.
# Control: el techo calculado para el 24/08/2026 da 1871,95 y el BCRA publico
# 1871,95. Fin de agosto 2026: calculado 1879,93 contra 1879,92 publicado.

INICIO_BANDAS = date(2025, 4, 14)
PISO_INICIAL = 1000.0
TECHO_INICIAL = 1400.0
FIN_REGIMEN_1 = date(2025, 12, 31)

ANCLAS = {  # fecha -> (piso, techo) publicados, para control
    date(2025, 12, 31): (916.28, 1526.60),
    date(2026, 1, 2): (914.78, 1529.03),
    date(2026, 8, 24): (None, 1871.95),
}


def dias_del_mes(a, m):
    return (date(a + (m == 12), m % 12 + 1, 1) - date(a, m, 1)).days


def calcular_bandas(ipc_mensual):
    """
    ipc_mensual: Serie indexada por fecha de fin de mes con el IPC en %.
    Devuelve DataFrame diario con piso y techo.
    """
    ipc = {(f.year, f.month): v / 100.0 for f, v in ipc_mensual.items()}

    filas = []
    # --- etapa 1: +-1% mensual capitalizado por dia corrido.
    # El BCRA no publico la formula al centavo, asi que la curva se calibra para
    # cerrar exactamente en el ancla oficial del 31/12/2025. El desvio repartido
    # es de ~0,16% sobre 8 meses y medio: los extremos quedan exactos, los puntos
    # intermedios son una reconstruccion.
    fin1 = min(FIN_REGIMEN_1, HOY)
    n_total = (FIN_REGIMEN_1 - INICIO_BANDAS).days / 30.4375
    piso_ok, techo_ok = ANCLAS[FIN_REGIMEN_1]
    k_p = (piso_ok / (PISO_INICIAL * 0.99**n_total)) if n_total else 1.0
    k_t = (techo_ok / (TECHO_INICIAL * 1.01**n_total)) if n_total else 1.0
    f = INICIO_BANDAS
    while f <= fin1:
        n = (f - INICIO_BANDAS).days / 30.4375  # meses transcurridos
        peso = n / n_total if n_total else 0.0  # calibracion progresiva
        piso = PISO_INICIAL * (0.99**n) * (k_p**peso)
        techo = TECHO_INICIAL * (1.01**n) * (k_t**peso)
        filas.append({"fecha": f, "Banda inferior": piso, "Banda superior": techo})
        f += timedelta(days=1)

    # --- etapa 2: ajuste por IPC del mes M-2, capitalizado por dia del mes
    if HOY > FIN_REGIMEN_1:
        # el ancla oficial del cierre 2025 manda sobre el calculo de la etapa 1
        piso_base, techo_base = ANCLAS[FIN_REGIMEN_1]
        for r in filas:
            if r["fecha"] == FIN_REGIMEN_1:
                r["Banda inferior"], r["Banda superior"] = piso_base, techo_base
        f = date(2026, 1, 1)
        while f <= HOY:
            if f.day == 1 and f != date(2026, 1, 1):
                piso_base, techo_base = piso_mes_fin, techo_mes_fin  # noqa: F821
            # IPC del mes M-2
            m2 = (f.year, f.month)
            am, mm = m2
            mm -= 2
            while mm <= 0:
                mm += 12
                am -= 1
            i = ipc.get((am, mm))
            if i is None:
                avisar("Bandas cambiarias", f"falta IPC de {am}-{mm:02d}")
                break
            N = dias_del_mes(f.year, f.month)
            piso = piso_base * ((1 - i) ** (f.day / N))
            techo = techo_base * ((1 + i) ** (f.day / N))
            filas.append({"fecha": f, "Banda inferior": piso, "Banda superior": techo})
            if f.day == N:
                piso_mes_fin, techo_mes_fin = piso, techo
            f += timedelta(days=1)

    df = pd.DataFrame(filas).drop_duplicates(subset=["fecha"], keep="last")
    df = df.set_index("fecha").sort_index()

    # --- control contra los valores publicados
    for f_ancla, (p_ok, t_ok) in ANCLAS.items():
        if f_ancla in df.index:
            if t_ok is not None:
                dif = df.loc[f_ancla, "Banda superior"] - t_ok
                print(
                    f"  control techo {f_ancla}: calculado "
                    f"{df.loc[f_ancla, 'Banda superior']:.2f} / publicado {t_ok:.2f} "
                    f"/ diferencia {dif:+.2f}"
                )
                if abs(dif) > 1.0:
                    avisar("Bandas cambiarias", f"desvio de {dif:+.2f} en {f_ancla}")
    return df


# ---------------------------------------------------------------- armado


def main():
    print("Panel macro - corrida", HOY.isoformat())
    columnas = {}

    for id_var, nombre in SERIES_BCRA.items():
        try:
            columnas[nombre] = bajar_bcra(id_var)
            print(f"  BCRA {nombre}: {len(columnas[nombre])} datos")
        except Exception as e:  # noqa: BLE001
            avisar(f"BCRA {nombre} (id {id_var})", e)

    try:
        c, v = bajar_bna()
        columnas["Dolar BNA comprador"] = c
        columnas["Dolar BNA vendedor"] = v
        print(f"  BNA: {len(c)} datos")
    except Exception as e:  # noqa: BLE001
        avisar("Dolar BNA", e)

    try:
        columnas["Bitcoin USD"] = bajar_btc()
        print(f"  Bitcoin: {len(columnas['Bitcoin USD'])} datos")
    except Exception as e:  # noqa: BLE001
        avisar("Bitcoin", e)

    # pizarra de granos: se acumula porque la fuente solo publica el ultimo dia
    try:
        fila = bajar_pizarra()
        gr = acumular_granos(fila)
        print(f"  Pizarra Rosario: {fila['fecha']} ({len(gr)} dias acumulados)")
    except Exception as e:  # noqa: BLE001
        avisar("Pizarra Rosario", e)
        ruta = DATOS / "granos.csv"
        gr = pd.read_csv(ruta) if ruta.exists() else pd.DataFrame()

    if len(gr):
        gr2 = gr.copy()
        gr2["fecha"] = pd.to_datetime(gr2["fecha"]).dt.date
        gr2 = gr2.set_index("fecha").sort_index()
        for col in gr2.columns:
            columnas[col] = pd.to_numeric(gr2[col], errors="coerce").dropna()

    # bandas cambiarias (calculadas, no publicadas como serie por el BCRA)
    if "IPC mensual" in columnas:
        try:
            bandas = calcular_bandas(columnas["IPC mensual"])
            columnas["Banda inferior"] = bandas["Banda inferior"]
            columnas["Banda superior"] = bandas["Banda superior"]
        except Exception as e:  # noqa: BLE001
            avisar("Bandas cambiarias", e)
    else:
        avisar("Bandas cambiarias", "no hay IPC mensual para calcularlas")

    if not columnas:
        raise SystemExit("ninguna fuente respondio: no se reescribe nada")

    df = pd.DataFrame(columnas).sort_index()
    df.index.name = "fecha"

    # --------- controles antes de publicar
    print("\nControles:")
    resumen = []
    for col in df.columns:
        s = df[col].dropna()
        resumen.append(
            {
                "serie": col,
                "datos": int(len(s)),
                "faltantes_en_rango": int(len(df.loc[s.index.min():, col]) - len(s))
                if len(s)
                else 0,
                "desde": s.index.min().isoformat() if len(s) else "",
                "hasta": s.index.max().isoformat() if len(s) else "",
                "ultimo_valor": float(s.iloc[-1]) if len(s) else None,
            }
        )
        print(
            f"  {col:32s} {len(s):6d} datos  "
            f"{resumen[-1]['desde']} -> {resumen[-1]['hasta']}  "
            f"ultimo: {resumen[-1]['ultimo_valor']}"
        )
    pd.DataFrame(resumen).to_csv(DATOS / "control.csv", index=False)

    if incidencias:
        pd.DataFrame(incidencias).to_csv(DATOS / "incidencias.csv", index=False)
        print(f"\n  {len(incidencias)} incidencia(s) registrada(s)")
    elif (DATOS / "incidencias.csv").exists():
        (DATOS / "incidencias.csv").unlink()

    df.to_csv(DATOS / "series.csv", float_format="%.6f")

    # --------- panel
    plantilla = (RAIZ / "plantilla.html").read_text(encoding="utf-8")
    payload = {
        "actualizado": datetime.now(TZ_AR).strftime("%d/%m/%Y %H:%M"),
        "fechas": [f.isoformat() for f in df.index],
        "series": {
            c: [None if pd.isna(x) else round(float(x), 6) for x in df[c]]
            for c in df.columns
        },
        "control": resumen,
        "incidencias": incidencias,
    }
    salida = plantilla.replace(
        "/*__DATOS__*/null", json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )
    (RAIZ / "index.html").write_text(salida, encoding="utf-8")
    print(f"\nListo: {len(df)} fechas, {len(df.columns)} series.")


if __name__ == "__main__":
    requests.packages.urllib3.disable_warnings()  # el BCRA usa una cadena TLS propia
    main()
