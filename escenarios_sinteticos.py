"""Construye velas 15m/1h sintéticas para 3 escenarios y verifica, con código
(no a ojo), que cada una cumple lo que se supone que cumple, antes de
mandarlas a Gemini. Si esto no pasa, el "caso de libro" no sirve como prueba."""
import random


def fmt_klines(velas):
    """velas: [(hora_str, o, h, l, c, vol), ...] cronológicas. Mismo formato que
    BybitClient.get_klines(): 'Hora | Open | High | Low | Close | Volume'."""
    lines = ["Hora | Open | High | Low | Close | Volume"]
    for hora, o, h, l, c, v in velas:
        lines.append(f"{hora} | {o:.4f} | {h:.4f} | {l:.4f} | {c:.4f} | {v:.0f}")
    return "\n".join(lines)


def horas(n, paso_min, inicio_h=8, inicio_m=0):
    out = []
    t = inicio_h * 60 + inicio_m
    for _ in range(n):
        out.append(f"{t // 60:02d}:{t % 60:02d}")
        t += paso_min
    return out


# ---------------------------------------------------------------------------
# ESCENARIO A: "de libro" — cumple todo el checklist de aprobación
# ---------------------------------------------------------------------------
def escenario_a():
    random.seed(1)
    precio_inicio = 1.000
    # 1h: caída moderada y contenida (no vertical, no pump previo) hasta soporte en 0.960,
    # luego 3 velas de recuperación con cierres al alza. Rango total 24h ~ -4%: no dispara
    # "cambio 1h < -4%" y tampoco "+15% sin retroceso".
    v1h = []
    precio = precio_inicio
    for i in range(9):  # caída lenta y escalonada
        o = precio
        c = round(o * (1 - random.uniform(0.002, 0.006)), 4)
        h = round(max(o, c) * 1.001, 4)
        l = round(min(o, c) * 0.999, 4)
        vol = 80_000 * random.uniform(0.9, 1.1)
        v1h.append((o, h, l, c, vol))
        precio = c
    soporte = precio  # ~0.960
    for i in range(3):  # rebote con velas verdes y volumen creciente
        o = precio
        c = round(o * (1 + random.uniform(0.004, 0.009)), 4)
        h = round(c * 1.001, 4)
        l = round(o * 0.999, 4)
        vol = 80_000 * (1.3 + i * 0.4)  # volumen claramente creciente
        v1h.append((o, h, l, c, vol))
        precio = c
    precio_actual = precio  # ~0.975 -> cambio 24h aprox -2.5%, dentro de rango sano
    cambio_1h = round((v1h[-1][2] / v1h[-2][2] - 1) * 100, 2)  # entre las 2 últimas velas 1h

    v1h_txt = [(h, o, hh, l, c, vv) for h, (o, hh, l, c, vv) in zip(horas(12, 60), v1h)]

    # 15m: últimas 20 velas, terminando con 2-3 velas de reversión tipo martillo
    # (mecha inferior larga, cierre cerca del máximo) y volumen creciente en las últimas 3.
    v15 = []
    precio = soporte * 0.998
    for i in range(15):
        o = precio
        c = round(o * (1 - random.uniform(0.0005, 0.0015)), 4)
        l = round(c * (1 - random.uniform(0.0005, 0.0015)), 4)
        h = round(o * 1.0005, 4)
        vol = 5_000 * random.uniform(0.8, 1.1)
        v15.append((o, h, l, c, vol))
        precio = c
    for i in range(5):  # velas martillo: mecha inferior marcada, cierre fuerte, volumen alto
        o = precio
        l = round(o * (1 - random.uniform(0.006, 0.010)), 4)  # mecha inferior larga
        c = round(o * (1 + random.uniform(0.003, 0.007)), 4)  # cierra por encima de la apertura
        h = round(c * 1.001, 4)
        vol = 5_000 * (1.4 + i * 0.5)  # volumen creciente y por encima del promedio previo
        v15.append((o, h, l, c, vol))
        precio = c
    v15_txt = [(h, o, hh, l, c, vv) for h, (o, hh, l, c, vv) in zip(horas(20, 15), v15)]

    return {
        "nombre": "A: de libro (rebote confirmado)",
        "symbol": "LIBROUSDC",
        "price": precio,
        "change_1h": cambio_1h,
        "change_24h": round((precio / precio_inicio - 1) * 100, 2),
        "volume": 25_000_000,  # por encima del filtro de liquidez de 10M
        "klines_15m": fmt_klines(v15_txt),
        "klines_1h": fmt_klines(v1h_txt),
        "esperado_criterios": {
            "cambio_1h_en_rango": -4 <= cambio_1h <= 5,
            "cambio_24h_no_extremo": -15 <= (precio / precio_inicio - 1) * 100 <= 15,
            "volumen_15m_creciente_final": v15[-1][4] > v15[-4][4],
            "hay_mecha_inferior_ultimas": any((c - l) > (h - c) for o, h, l, c, vv in v15[-3:]),
        },
    }


# ---------------------------------------------------------------------------
# ESCENARIO B: caída vertical sin freno — control negativo (debe seguir siendo VETO)
# ---------------------------------------------------------------------------
def escenario_b():
    random.seed(2)
    precio = 1.000
    v1h, v15 = [], []
    for i in range(12):
        o = precio
        c = round(o * (1 - random.uniform(0.01, 0.02)), 4)  # caída fuerte y sostenida
        h, l = round(o * 1.001, 4), round(c * 0.998, 4)
        vol = 60_000 * random.uniform(0.7, 1.0)  # volumen sin repuntar
        v1h.append((o, h, l, c, vol))
        precio = c
    precio_actual = precio
    cambio_1h = round((v1h[-1][2] / v1h[-2][2] - 1) * 100, 2)
    v1h_txt = [(h, o, hh, l, c, vv) for h, (o, hh, l, c, vv) in zip(horas(12, 60), v1h)]

    p15 = precio_actual * 1.02
    for i in range(20):
        o = p15
        c = round(o * (1 - random.uniform(0.006, 0.012)), 4)  # todas rojas, sin reversión
        h, l = round(o * 1.0005, 4), round(c * 0.998, 4)
        vol = 4_000 * random.uniform(0.6, 0.9)
        v15.append((o, h, l, c, vol))
        p15 = c
    v15_txt = [(h, o, hh, l, c, vv) for h, (o, hh, l, c, vv) in zip(horas(20, 15), v15)]

    return {
        "nombre": "B: caída vertical sin freno (control, debe seguir vetado)",
        "symbol": "CONTROLUSDC",
        "price": p15,
        "change_1h": cambio_1h,
        "change_24h": round((p15 / 1.000 - 1) * 100, 2),
        "volume": 25_000_000,
        "klines_15m": fmt_klines(v15_txt),
        "klines_1h": fmt_klines(v1h_txt),
        "esperado_criterios": {"caida_sin_velas_verdes": all(c < o for o, h, l, c, vv in v15[-5:])},
    }


# ---------------------------------------------------------------------------
# ESCENARIO C: intermedio — rebote plausible pero volumen plano (borderline)
# ---------------------------------------------------------------------------
def escenario_c():
    random.seed(3)
    precio = 1.000
    v1h = []
    for i in range(9):
        o = precio
        c = round(o * (1 - random.uniform(0.002, 0.006)), 4)
        h, l = round(o * 1.001, 4), round(c * 0.999, 4)
        v1h.append((o, h, l, c, 70_000 * random.uniform(0.9, 1.1)))
        precio = c
    for i in range(3):
        o = precio
        c = round(o * (1 + random.uniform(0.002, 0.005)), 4)  # rebote más tibio
        h, l = round(c * 1.001, 4), round(o * 0.999, 4)
        v1h.append((o, h, l, c, 70_000 * random.uniform(0.95, 1.05)))  # volumen PLANO, no creciente
        precio = c
    cambio_1h = round((v1h[-1][2] / v1h[-2][2] - 1) * 100, 2)
    v1h_txt = [(h, o, hh, l, c, vv) for h, (o, hh, l, c, vv) in zip(horas(12, 60), v1h)]

    p15 = precio * 0.999
    v15 = []
    for i in range(20):
        o = p15
        c = round(o * (1 + random.uniform(-0.002, 0.002)), 4)  # lateral, sin mecha marcada
        h, l = round(max(o, c) * 1.0008, 4), round(min(o, c) * 0.9992, 4)
        v15.append((o, h, l, c, 4_500 * random.uniform(0.9, 1.1)))
        p15 = c
    v15_txt = [(h, o, hh, l, c, vv) for h, (o, hh, l, c, vv) in zip(horas(20, 15), v15)]

    return {
        "nombre": "C: intermedio (rebote tibio, volumen plano)",
        "symbol": "BORDEUSDC",
        "price": p15,
        "change_1h": cambio_1h,
        "change_24h": round((p15 / 1.000 - 1) * 100, 2),
        "volume": 25_000_000,
        "klines_15m": fmt_klines(v15_txt),
        "klines_1h": fmt_klines(v1h_txt),
        "esperado_criterios": {},
    }


# ---------------------------------------------------------------------------
# ESCENARIO D: intermedio malo — rebote débil, volumen solo en 1 vela
# ---------------------------------------------------------------------------
def escenario_d():
    random.seed(4)
    precio = 1.000
    v1h = []
    for i in range(9):
        o = precio
        c = round(o * (1 - random.uniform(0.002, 0.006)), 4)
        h, l = round(o * 1.001, 4), round(c * 0.999, 4)
        v1h.append((o, h, l, c, 70_000 * random.uniform(0.9, 1.1)))
        precio = c
    for i in range(3):
        o = precio
        c = round(o * (1 + random.uniform(0.002, 0.005)), 4)
        h, l = round(c * 1.001, 4), round(o * 0.999, 4)
        v1h.append((o, h, l, c, 70_000 * random.uniform(0.95, 1.05)))
        precio = c
    cambio_1h = round((v1h[-1][2] / v1h[-2][2] - 1) * 100, 2)
    v1h_txt = [(h, o, hh, l, c, vv) for h, (o, hh, l, c, vv) in zip(horas(12, 60), v1h)]

    p15 = precio * 0.999
    v15 = []
    for i in range(19):
        o = p15
        c = round(o * (1 + random.uniform(-0.002, 0.002)), 4)
        h, l = round(max(o, c) * 1.0008, 4), round(min(o, c) * 0.9992, 4)
        v15.append((o, h, l, c, 4_500 * random.uniform(0.9, 1.1)))
        p15 = c
    # Última vela con volumen alto, pero cierre pobre y mecha superior larga (rechazo alcista falso)
    o = p15
    h = round(o * 1.005, 4)
    l = round(o * 0.998, 4)
    c = round(o * 1.001, 4)
    v15.append((o, h, l, c, 12_000))
    p15 = c
    v15_txt = [(h, o, hh, l, c, vv) for h, (o, hh, l, c, vv) in zip(horas(20, 15), v15)]

    return {
        "nombre": "D: intermedio malo (rebote débil, trampa de volumen en 1 vela)",
        "symbol": "TRAMPAUSDC",
        "price": p15,
        "change_1h": cambio_1h,
        "change_24h": round((p15 / 1.000 - 1) * 100, 2),
        "volume": 25_000_000,
        "klines_15m": fmt_klines(v15_txt),
        "klines_1h": fmt_klines(v1h_txt),
        "esperado_criterios": {},
    }

ESCENARIOS = [escenario_a, escenario_b, escenario_c, escenario_d]


if __name__ == "__main__":
    # Verificación de que cada escenario cumple lo que dice cumplir, ANTES de gastar
    # una llamada a Gemini con él.
    for fn in ESCENARIOS:
        e = fn()
        print(f"\n=== {e['nombre']} ({e['symbol']}) ===")
        print(f"  precio={e['price']:.4f}  cambio_1h={e['change_1h']:+.2f}%  cambio_24h={e['change_24h']:+.2f}%")
        for k, v in e.get("esperado_criterios", {}).items():
            estado = "OK" if v else "FALLA"
            print(f"  [{estado}] {k}")
        fallas = [k for k, v in e.get("esperado_criterios", {}).items() if not v]
        if fallas:
            print(f"  !! Este escenario NO cumple: {fallas}. No lo uses como 'caso de libro' hasta corregirlo.")
