import math
import os
import random
import socket
import sys
import threading
import webbrowser

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room

def recurso(ruta):
    """Ruta de un archivo (templates, static): funciona con `python server.py` y dentro del .exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, ruta)


app = Flask(__name__, template_folder=recurso("templates"), static_folder=recurso("static"))
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ---------- Ajustes (tocá acá para cambiar la sensación del juego) ----------
ANCHO, ALTO = 600, 400
PALETA_W, PALETA_H, RADIO, MARGEN = 12, 80, 8, 20
PUNTOS_PARA_GANAR = 10
VELOCIDADES = {"normal": 4.5, "rapido": 6.0}  # px por frame (a 60 fps)
ACELERACION = 1.06   # la bola acelera un 6 % en cada golpe de paleta
VEL_MAX = 2.4        # tope: velocidad base x 2.4
ANGULO_PALETA_MAX = 25            # inclinación máxima de la paleta (grados, con dos dedos)
ANGULO_BORDE = math.radians(20)   # cuánto curva el golpe según dónde pega (centro = 0, punta = máx.)
SALIDA_MAX = math.radians(65)     # ángulo máximo de salida respecto de la horizontal
PAUSA_INICIO, PAUSA_GOL, PAUSA_REANUDAR = 5.0, 1.0, 3.0  # segundos de cuenta regresiva (inicio, tras gol, al reanudar)

# ---------- Power-ups ----------
PU_RADIO = 13
PU_TIPOS = ["rapida", "lenta", "achicar"]
PU_DURACION = {"rapida": 5.0, "lenta": 5.0, "achicar": 6.0}  # segundos de efecto
PU_FACTOR = {"rapida": 1.6, "lenta": 0.55}  # multiplica la velocidad de la bola
PALETA_CHICA = 40  # alto de la paleta del rival mientras dura el efecto
PU_APARICION = (4, 8)  # segundos entre un power-up y el siguiente

jugadores = {}  # {sid: 1 o 2}
obj_ang = {1: 0.0, 2: 0.0}  # inclinación pedida por cada jugador (grados)

estado = {
    "p1_y": 160, "p2_y": 160,
    "bola_x": ANCHO / 2, "bola_y": ALTO / 2, "vx": 0.0, "vy": 0.0,
    "puntos_p1": 0, "puntos_p2": 0,
    "jugando": False, "ganador": None,
    "pausa": 0.0, "racha": 0, "velocidad": "normal",
    "pu": None, "t_pu": 6.0, "factor": 1.0, "t_vel": 0.0, "ultimo": None,
    "pausado": False, "dos": False, "listos": [],
    "a1": 0.0, "a2": 0.0,  # inclinación actual de cada paleta (grados)
    "h1": PALETA_H, "h2": PALETA_H, "t_h1": 0.0, "t_h2": 0.0,
}


def ip_local():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


URL_MANDO = f"http://{ip_local()}:5000"


def emit_marcador(anota=0, a=None):
    socketio.emit("marcador", {
        "p1": estado["puntos_p1"], "p2": estado["puntos_p2"],
        "ganador": estado["ganador"], "jugando": estado["jugando"], "anota": anota,
        "dos": estado["dos"], "listos": estado["listos"], "pausado": estado["pausado"],
    }, to=a)


def sacar(direccion=None, pausa=PAUSA_GOL):
    base = VELOCIDADES[estado["velocidad"]]
    ang = math.radians(random.uniform(-30, 30))
    d = direccion or random.choice([-1, 1])
    estado.update(bola_x=ANCHO / 2, bola_y=ALTO / 2,
                  vx=d * base * math.cos(ang), vy=base * math.sin(ang),
                  pausa=pausa, racha=0,
                  pu=None, factor=1.0, t_vel=0.0, ultimo=None,
                  h1=PALETA_H, h2=PALETA_H, t_h1=0.0, t_h2=0.0,
                  t_pu=random.uniform(*PU_APARICION))


def reiniciar_partida():
    estado.update(puntos_p1=0, puntos_p2=0, ganador=None, p1_y=160, p2_y=160, pausado=False, listos=[], a1=0.0, a2=0.0)
    obj_ang.update({1: 0.0, 2: 0.0})
    sacar(pausa=PAUSA_INICIO)
    estado["jugando"] = (len(jugadores) == 2)


def preparar_sala():
    """Hay dos jugadores conectados: la partida espera a que ambos toquen «Estoy listo»."""
    reiniciar_partida()
    estado["jugando"] = False


def punto(j):
    estado[f"puntos_p{j}"] += 1
    if estado[f"puntos_p{j}"] >= PUNTOS_PARA_GANAR:
        estado["jugando"] = False
        estado["ganador"] = j
        socketio.emit("sonido", {"tipo": "victoria"})
    else:
        socketio.emit("sonido", {"tipo": "gol"})
        sacar(direccion=-1 if j == 2 else 1)
    emit_marcador(anota=j)


def choque_paleta(j):
    """La paleta es un segmento con grosor. La bola rebota como en un espejo: el ángulo de salida
    depende de la inclinación de la paleta, de cómo llegó la bola y de en qué parte pegó."""
    e = estado
    sentido = 1 if j == 1 else -1  # hacia dónde sale la bola
    hl = e[f"h{j}"] / 2
    cx = MARGEN + PALETA_W / 2 if j == 1 else ANCHO - MARGEN - PALETA_W / 2
    cy = e[f"p{j}_y"] + PALETA_H / 2
    a = math.radians(e[f"a{j}"])
    ex, ey = math.sin(a), math.cos(a)                       # eje de la paleta (de arriba hacia abajo)
    nx, ny = sentido * math.cos(a), -sentido * math.sin(a)  # normal: apunta hacia la cancha
    px, py = e["bola_x"] - cx, e["bola_y"] - cy
    t = px * ex + py * ey          # posición sobre la paleta (0 = centro)
    d = px * nx + py * ny          # distancia frente a la paleta
    fuera = max(0.0, abs(t) - hl)  # cuánto se pasa de la punta
    if d < 0 or math.hypot(fuera, d) > RADIO + PALETA_W / 2:
        return
    if e["vx"] * nx + e["vy"] * ny >= 0:  # la bola ya se está alejando
        return
    b = sentido * max(-1.0, min(1.0, t / hl)) * ANGULO_BORDE  # pegar arriba/abajo curva un poco la normal
    mx = nx * math.cos(b) - ny * math.sin(b)
    my = nx * math.sin(b) + ny * math.cos(b)
    vn = e["vx"] * mx + e["vy"] * my
    rx, ry = e["vx"] - 2 * vn * mx, e["vy"] - 2 * vn * my  # reflexión tipo espejo
    ang = math.atan2(ry, sentido * rx)
    if abs(ang) > SALIDA_MAX:  # nunca sale casi vertical ni hacia atrás
        ang = math.copysign(SALIDA_MAX, ang)
    v = min(math.hypot(e["vx"], e["vy"]) * ACELERACION, VELOCIDADES[e["velocidad"]] * VEL_MAX)
    e["vx"], e["vy"] = sentido * v * math.cos(ang), v * math.sin(ang)
    sal = RADIO + PALETA_W / 2 - d  # saca la bola de la paleta
    e["bola_x"] += nx * sal
    e["bola_y"] += ny * sal
    e["racha"] += 1
    e["ultimo"] = j
    socketio.emit("sonido", {"tipo": "paleta", "racha": e["racha"], "lado": j})


def activar(tipo, dueno):
    e = estado
    if tipo == "achicar":
        rival = 3 - dueno
        e[f"h{rival}"] = PALETA_CHICA
        e[f"t_h{rival}"] = PU_DURACION[tipo]
    else:
        e["factor"] = PU_FACTOR[tipo]
        e["t_vel"] = PU_DURACION[tipo]
    socketio.emit("sonido", {"tipo": "powerup", "pu": tipo})
    socketio.emit("aviso", {"tipo": tipo, "lado": dueno})


def gestionar_powerups(dt):
    e = estado
    s = dt / 60
    for k in ("t_vel", "t_h1", "t_h2"):
        e[k] = max(0.0, e[k] - s)
    if e["t_vel"] == 0:
        e["factor"] = 1.0
    for j in (1, 2):
        if e[f"t_h{j}"] == 0:
            e[f"h{j}"] = PALETA_H
    if e["pu"] is None:
        e["t_pu"] -= s
        if e["t_pu"] <= 0:
            e["pu"] = {"x": random.uniform(190, 410), "y": random.uniform(50, ALTO - 50),
                       "tipo": random.choice(PU_TIPOS), "vida": 9.0}
        return
    pu = e["pu"]
    pu["vida"] -= s
    if pu["vida"] <= 0:
        e["pu"], e["t_pu"] = None, random.uniform(*PU_APARICION)
        return
    if math.hypot(e["bola_x"] - pu["x"], e["bola_y"] - pu["y"]) <= RADIO + PU_RADIO:
        activar(pu["tipo"], e["ultimo"] or (1 if e["vx"] > 0 else 2))
        e["pu"], e["t_pu"] = None, random.uniform(*PU_APARICION)


def paso(dt):
    e = estado
    if e["pausa"] > 0:
        antes = math.ceil(e["pausa"])
        e["pausa"] = max(0.0, e["pausa"] - dt / 60)
        if e["pausa"] == 0:
            socketio.emit("sonido", {"tipo": "saque"})
        elif math.ceil(e["pausa"]) != antes:
            socketio.emit("sonido", {"tipo": "cuenta"})
        return

    k = dt * e["factor"]
    pasos = max(1, math.ceil(max(abs(e["vx"]), abs(e["vy"])) * k / 6))  # subpasos de ≤ 6 px: no atraviesa la paleta
    for _ in range(pasos):
        e["bola_x"] += e["vx"] * k / pasos
        e["bola_y"] += e["vy"] * k / pasos
        if e["bola_y"] < RADIO:
            e["bola_y"], e["vy"] = RADIO, abs(e["vy"])
            socketio.emit("sonido", {"tipo": "pared"})
        elif e["bola_y"] > ALTO - RADIO:
            e["bola_y"], e["vy"] = ALTO - RADIO, -abs(e["vy"])
            socketio.emit("sonido", {"tipo": "pared"})
        choque_paleta(1)
        choque_paleta(2)
        if e["bola_x"] < -RADIO:
            return punto(2)
        if e["bola_x"] > ANCHO + RADIO:
            return punto(1)
    gestionar_powerups(dt)


def bucle_juego():
    import time
    previo = time.perf_counter()
    while True:
        ahora = time.perf_counter()
        dt = min((ahora - previo) * 60, 3)  # en "frames" de 1/60 s; compensa los tirones
        previo = ahora
        for j in (1, 2):  # la inclinación se suaviza: los dedos tiemblan
            estado[f"a{j}"] += (obj_ang[j] - estado[f"a{j}"]) * min(1.0, 0.3 * dt)
        if estado["jugando"] and not estado["pausado"]:
            paso(dt)
        socketio.emit("actualizar_tv", estado, to="tv")  # solo a la TV, no a los celulares
        socketio.sleep(1 / 60)


@app.route("/")
def mando():
    return render_template("mando.html")


@app.route("/tv")
def tv():
    return render_template("tv.html")


@socketio.on("connect")
def conectar():
    if request.args.get("tipo") == "tv":
        join_room("tv")
        emit("actualizar_tv", estado)
        emit("info", {"url": URL_MANDO})
        emit_marcador(a=request.sid)
        return

    rol = next((r for r in (1, 2) if r not in jugadores.values()), 0)
    if rol:
        jugadores[request.sid] = rol
    emit("rol", {"jugador": rol})
    emit_marcador(a=request.sid)
    if not rol:
        return  # un tercer celular ya no reinicia la partida en curso

    if len(jugadores) == 2:
        preparar_sala()
        estado["dos"] = True
        socketio.emit("estado_sala", {"msg": "Los dos están conectados: ¡cada uno toca «Estoy listo»!"})
        emit_marcador()
    else:
        socketio.emit("estado_sala", {"msg": f"Esperando jugadores ({len(jugadores)}/2)"})


@socketio.on("disconnect")
def desconectar():
    if request.sid in jugadores:
        del jugadores[request.sid]
        estado.update(jugando=False, dos=False, pausado=False, listos=[])
        socketio.emit("estado_sala", {"msg": f"Se fue un jugador. Esperando ({len(jugadores)}/2)"})
        emit_marcador()


@socketio.on("mover")
def mover(data):
    j = jugadores.get(request.sid)
    try:
        p = max(0.0, min(1.0, float(data["porcentaje"])))
        ang = max(-ANGULO_PALETA_MAX, min(ANGULO_PALETA_MAX, float(data.get("angulo", 0))))
    except (KeyError, TypeError, ValueError):
        return
    if j in (1, 2):
        estado[f"p{j}_y"] = p * (ALTO - PALETA_H)
        obj_ang[j] = ang


@socketio.on("revancha")
def revancha():
    if len(jugadores) == 2 and estado["ganador"]:  # solo cuando terminó la partida
        reiniciar_partida()
        socketio.emit("estado_sala", {"msg": "¡Revancha!"})
        emit_marcador()


@socketio.on("listo")
def listo():
    rol = jugadores.get(request.sid)
    if not rol or estado["jugando"] or len(jugadores) != 2:
        return
    if rol not in estado["listos"]:
        estado["listos"].append(rol)
    if len(estado["listos"]) == 2:  # empieza recién cuando los dos están listos
        reiniciar_partida()
        socketio.emit("estado_sala", {"msg": f"¡A jugar! Primero a {PUNTOS_PARA_GANAR}"})
    else:
        socketio.emit("estado_sala", {"msg": f"Jugador {rol} listo. Esperando al otro…"})
    emit_marcador()


@socketio.on("pausar")
def pausar():
    if not estado["jugando"] or estado["ganador"]:
        return
    estado["pausado"] = not estado["pausado"]
    if not estado["pausado"]:
        estado["pausa"] = max(estado["pausa"], PAUSA_REANUDAR)  # cuenta regresiva para retomar
    socketio.emit("pausa", {"pausado": estado["pausado"]})


@socketio.on("velocidad")
def velocidad(data):
    nivel = (data or {}).get("nivel")
    if nivel in VELOCIDADES:
        estado["velocidad"] = nivel  # se aplica desde el próximo saque


if __name__ == "__main__":
    try:
        print(f"\n  TV:      {URL_MANDO}/tv\n  Celular: {URL_MANDO}\n")
        print("  Para cerrar el juego, cerrá esta ventana.\n")
        socketio.start_background_task(bucle_juego)
        threading.Timer(1.2, lambda: webbrowser.open("http://127.0.0.1:5000/tv")).start()
        try:
            socketio.run(app, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)
        except TypeError:  # versiones viejas de Flask-SocketIO no conocen ese parámetro
            socketio.run(app, host="0.0.0.0", port=5000)
    except Exception as err:  # que la ventana no desaparezca sin explicar qué pasó
        print(f"\n  No se pudo iniciar el servidor: {err}")
        print("  (¿Ya hay otra copia del juego abierta usando el puerto 5000?)")
        input("\n  Presioná Enter para cerrar...")
