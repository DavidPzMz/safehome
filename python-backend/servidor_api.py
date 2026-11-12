import os
import sys
import csv
import json
import asyncio
import queue
import threading
import time
from datetime import datetime
from collections import deque
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

import cv2
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

sys.path.insert(0, os.path.dirname(__file__))
from detector import DetectorPersonas
from alertas import SistemaAlertas, leer_historial, borrar_historial, ruta_imagen_alerta, marcar_correos_pendientes
from alertas import enviar_codigo, envio_listo, servicio_activo, servicio_pedir_codigo, servicio_confirmar
from verificacion import Verificacion, correo_valido
import config
from gestor_config import cargar as cargar_config, guardar as guardar_config
from gestor_config import MODO, MODO_PRUEBAS, CARPETA_CONFIG, CLAVES_REMITENTE, migrar_configuracion


class GestorConexiones:
    """Lleva la lista de paneles conectados por websocket para mandarles el estado."""

    def __init__(self):
        self.conexiones: List[WebSocket] = []

    async def conectar(self, websocket: WebSocket):
        await websocket.accept()
        self.conexiones.append(websocket)

    def desconectar(self, websocket: WebSocket):
        if websocket in self.conexiones:
            self.conexiones.remove(websocket)

    async def mandar_a_todos(self, mensaje: Dict[str, Any]):
        # manda el mensaje a todos y saca a los que ya se desconectaron
        muertas = []
        for conexion in self.conexiones:
            try:
                await conexion.send_json(mensaje)
            except Exception:
                muertas.append(conexion)
        for m in muertas:
            self.desconectar(m)


# --- Estado global del servidor ---
gestor = GestorConexiones()
hilo_deteccion: Optional[threading.Thread] = None
hilo_captura: Optional[threading.Thread] = None
detector_actual: Optional[DetectorPersonas] = None
alertas_actual: Optional[SistemaAlertas] = None
captura: Optional[cv2.VideoCapture] = None
captura_vista_previa: Optional[cv2.VideoCapture] = None
en_marcha = False
config_actual = {}
candado_cuadro = threading.Lock()
ultimo_cuadro: Optional[np.ndarray] = None
ultimo_estado: Dict[str, Any] = {}
escaneando_camaras = False
# Cola chiquita de cuadros por analizar. Si la deteccion no alcanza se tira el
# mas viejo y la captura sigue a su ritmo, asi el video nunca se alenta.
cola_deteccion: "queue.Queue" = queue.Queue(maxsize=2)
captura_lista = threading.Event()
fps_captura = 30.0
fps_captura_medido = 0.0
camara_activa = False
captura_es_video = False
cuadros_capturados = 0
# Numero de la ejecucion actual: si se detiene y se vuelve a iniciar, los
# hilos viejos ven que cambio y se terminan solos
num_ejecucion = 0

# --- Solo app de pruebas ---
# Zonas guardadas por cada video y registro de eventos para revisarlos despues
ARCHIVO_ZONAS_POR_VIDEO = os.path.join(CARPETA_CONFIG, "zonas_por_video.json")
ARCHIVO_REGISTRO_EVENTOS = os.path.join(CARPETA_CONFIG, "registro_eventos.csv")
CLAVES_ZONAS = ("ROI", "CERRADURAS", "PUERTAS")


# Formatos de lo que manda el panel en cada peticion
class PeticionControl(BaseModel):
    accion: str


class CambioConfiguracion(BaseModel):
    configuracion: Dict[str, Any]


class CambioZonas(BaseModel):
    zonas: Dict[str, Any]


class PeticionCorreo(BaseModel):
    paso: str                      # "actual" o "nuevo"
    correo: Optional[str] = None   # solo para pedir el codigo del correo nuevo
    codigo: Optional[str] = None   # solo para confirmar


def _abrir_captura(fuente):
    """Abre la camara o el video (acepta numero, nombre o ruta)."""
    resuelta = _resolver_fuente(fuente)
    if resuelta is None:
        return None
    try:
        return cv2.VideoCapture(resuelta)
    except Exception:
        return None


def _reiniciar_video(captura_video, fuente):
    """Regresa el video al inicio.

    Rebobinar con CAP_PROP_POS_FRAMES falla sin avisar en varios codecs (el
    read() sigue dando False y todo se congela). Si no sale un cuadro bueno,
    se vuelve a abrir el archivo completo.
    """
    try:
        captura_video.set(cv2.CAP_PROP_POS_FRAMES, 0)
        ok, _ = captura_video.read()
        if ok:
            captura_video.set(cv2.CAP_PROP_POS_FRAMES, 0)
            return captura_video
    except Exception:
        pass
    # no se pudo rebobinar: se abre de nuevo
    try:
        captura_video.release()
    except Exception:
        pass
    captura_video = _abrir_captura(fuente)
    if captura_video and captura_video.isOpened():
        return captura_video
    return None


def _vaciar_cola_deteccion():
    # saca todo lo que haya en la cola
    while True:
        try:
            cola_deteccion.get_nowait()
        except queue.Empty:
            return


def _nombre_fuente(fuente):
    """Nombre fijo de la fuente: el nombre del video (`video3.mp4`) o `camara_N`."""
    s = str(fuente).strip()
    if s.isdigit():
        return f"camara_{s}"
    return os.path.basename(s.replace("\\", "/")) or s


def _leer_zonas_por_video():
    # lee el json de zonas por video (y las pasa al formato nuevo)
    try:
        with open(ARCHIVO_ZONAS_POR_VIDEO) as archivo:
            datos = json.load(archivo)
        if not isinstance(datos, dict):
            return {}
        return {video: migrar_configuracion(zonas) for video, zonas in datos.items()}
    except (OSError, json.JSONDecodeError):
        return {}


def _guardar_zonas_video(fuente, cfg):
    """Guarda las zonas actuales como las de este video (solo pruebas)."""
    datos = _leer_zonas_por_video()
    datos[_nombre_fuente(fuente)] = {k: cfg[k] for k in CLAVES_ZONAS if k in cfg}
    try:
        os.makedirs(CARPETA_CONFIG, exist_ok=True)
        with open(ARCHIVO_ZONAS_POR_VIDEO, "w") as archivo:
            json.dump(datos, archivo, indent=2)
    except OSError as e:
        print(f"No se pudieron guardar las zonas por video: {e}")


def _aplicar_zonas_video(fuente, cfg):
    """Mete en `cfg` las zonas guardadas de este video. True si habia."""
    zonas = _leer_zonas_por_video().get(_nombre_fuente(fuente))
    if not zonas:
        return False
    cfg.update(zonas)
    return True


def _pasar_zonas_al_detector(cfg):
    # le cambia las zonas al detector que esta corriendo
    if detector_actual:
        detector_actual.roi = cfg.get("ROI", detector_actual.roi)
        detector_actual.cerraduras = list(cfg.get("CERRADURAS", detector_actual.cerraduras))
        detector_actual.puertas = list(cfg.get("PUERTAS", detector_actual.puertas))


def _registrar_evento(fuente, segundo_video, tipo, metricas):
    """Agrega una fila al CSV de eventos (solo pruebas)."""
    es_nuevo = not os.path.exists(ARCHIVO_REGISTRO_EVENTOS)
    try:
        os.makedirs(CARPETA_CONFIG, exist_ok=True)
        with open(ARCHIVO_REGISTRO_EVENTOS, "a", newline="") as archivo:
            escritor = csv.writer(archivo)
            if es_nuevo:
                escritor.writerow(["fecha_hora", "fuente", "segundo_video", "evento", "metricas"])
            escritor.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                _nombre_fuente(fuente),
                "" if segundo_video is None else f"{segundo_video:.2f}",
                tipo,
                json.dumps(metricas, ensure_ascii=False),
            ])
    except OSError as e:
        print(f"No se pudo escribir el registro de eventos: {e}")


def _dibujar_encima(imagen, cfg):
    """Pinta sobre la imagen lo ultimo que encontro el detector (solo app de pruebas).

    Corre en el hilo de captura con lo que dejo el hilo de deteccion. Esas
    asignaciones son atomicas, lo peor que pasa es dibujar lo de un cuadro antes.
    """
    det = detector_actual
    if det is None:
        return
    det.dibujar_roi(imagen)
    if cfg.get("MODO_DETECCION", "cascada") == "cascada":
        det.dibujar_zonas(imagen, det.cerraduras, (255, 0, 0), "cerradura")
        det.dibujar_zonas(imagen, det.puertas, (0, 165, 255), "puerta")

    personas = getattr(det, "ultimas_personas", [])
    if getattr(det, "ultimo_hay_persona", False) and len(personas) > 0:
        det.dibujar_personas(imagen, personas)

    caja = getattr(det, "ultima_caja_movimiento", None)
    if getattr(det, "hay_movimiento", False) and caja:
        mx, my, mw, mh = caja
        cv2.rectangle(imagen, (mx, my), (mx + mw, my + mh), (255, 0, 255), 2)
        cv2.putText(imagen, "Movimiento", (mx, my - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 255), 2)

    puntos = getattr(det, "ultimos_puntos", [])
    if len(puntos) > 0 and cfg.get("MODO_DETECCION", "cascada") == "cascada":
        det.dibujar_esqueleto(imagen, puntos, con_lineas=True)

    if getattr(det, "ultimo_alerta_trepar", False):
        cv2.rectangle(imagen, (0, 0),
                      (imagen.shape[1], imagen.shape[0]), (0, 0, 255), 4)
        cv2.putText(imagen, "ALERTA: POSTURA DE ESCALAMIENTO", (20, 50),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
    if (getattr(det, "ultimo_manos_cerradura", False)
            and det.tiempo_cerradura() >= cfg.get("UMBRAL_CERRADURA_S", 4.5)):
        cv2.putText(imagen, "ALERTA: FORZADO DE CERRADURA", (50, 100),
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 3)


def bucle_captura(ejecucion):
    """Lee la camara o el video a su velocidad normal y deja cada cuadro listo.

    Es el unico hilo que toca `captura`. La deteccion agarra cuadros de una
    cola chiquita, asi si va lenta solo se brincan cuadros y el video no se frena.
    """
    global en_marcha, captura, ultimo_cuadro, config_actual
    global fps_captura, captura_es_video, cuadros_capturados
    global fps_captura_medido, camara_activa

    cfg = config_actual
    fuente = cfg.get("FUENTE_CAMARA", 0)
    es_video = isinstance(fuente, str)
    fuente_resuelta = _resolver_fuente(fuente)

    captura = _abrir_captura(fuente)
    if not captura or not captura.isOpened():
        print(f"Error: No se pudo abrir la fuente: {fuente}")
        camara_activa = False
        en_marcha = False
        captura_lista.set()
        return

    captura.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.get("ANCHO_CUADRO", 640))
    captura.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.get("ALTO_CUADRO", 480))

    fps = captura.get(cv2.CAP_PROP_FPS)
    if not fps or fps != fps or fps <= 0:  # None, NaN o <= 0
        fps = 30.0
    fps_captura = fps
    fps_captura_medido = 0.0
    camara_activa = True
    captura_es_video = es_video
    cuadros_capturados = 0
    salto_video = max(1, int(cfg.get("SALTO_CUADROS_VIDEO", 3)))

    # FPS reales: promedio con las horas de los ultimos cuadros. En un video
    # debe dar sus fps normales; en camara lo que de verdad entrega.
    horas_cuadros = deque(maxlen=45)

    # Un archivo se lee mucho mas rapido de lo que dura, hay que frenarlo a la
    # velocidad real del video. monotonic() no brinca si cambia la hora.
    periodo_video = 1.0 / fps if es_video else 0.0
    siguiente_t = time.monotonic()
    captura_lista.set()

    try:
        while en_marcha and ejecucion == num_ejecucion:
            cfg = config_actual  # se recarga para agarrar cambios de zonas al vuelo

            if periodo_video > 0:
                ahora = time.monotonic()
                if ahora < siguiente_t:
                    # se duerme poquito para hacer caso rapido a detener
                    time.sleep(min(siguiente_t - ahora, 0.05))
                    continue
                siguiente_t += periodo_video
                if ahora - siguiente_t > 0.5:
                    siguiente_t = ahora

            ok, cuadro = captura.read()
            if not ok or cuadro is None:
                if es_video:
                    # se acabo el video: se reinicia (evita que se congele al final)
                    captura = _reiniciar_video(captura, fuente_resuelta)
                    if captura is None:
                        print(f"Error: No se pudo reabrir el video: {fuente}")
                        en_marcha = False
                        break
                    siguiente_t = time.monotonic()
                    time.sleep(0.05)
                    continue
                break

            cuadros_capturados += 1
            try:
                cuadro = cv2.resize(cuadro, (cfg.get("ANCHO_CUADRO", 640),
                                             cfg.get("ALTO_CUADRO", 480)))
            except Exception as e:
                print(f"No se pudo redimensionar: {e}")
                continue

            horas_cuadros.append(time.monotonic())
            if len(horas_cuadros) >= 2:
                lapso = horas_cuadros[-1] - horas_cuadros[0]
                if lapso > 0:
                    fps_captura_medido = (len(horas_cuadros) - 1) / lapso

            # Solo 1 de cada SALTO_CUADROS_VIDEO cuadros va a la cola; si esta
            # llena se tira el mas viejo
            segundo_video = (captura.get(cv2.CAP_PROP_POS_MSEC) / 1000.0) if es_video else None
            if not es_video or (cuadros_capturados % salto_video == 0):
                elemento = (time.monotonic(), cuadro, segundo_video)
                try:
                    cola_deteccion.put_nowait(elemento)
                except queue.Full:
                    try:
                        cola_deteccion.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        cola_deteccion.put_nowait(elemento)
                    except queue.Full:
                        pass

            imagen = cuadro.copy()
            # en la app final se muestra el video limpio; los recuadros,
            # esqueletos y zonas solo se dibujan en la app de pruebas
            if MODO_PRUEBAS:
                try:
                    _dibujar_encima(imagen, cfg)
                except Exception as e:
                    print(f"Error dibujando encima del video: {e}")
                if segundo_video is not None:
                    cv2.putText(imagen, f"t={segundo_video:.1f}s", (10, imagen.shape[0] - 12),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

            with candado_cuadro:
                ultimo_cuadro = imagen
    finally:
        camara_activa = False
        fps_captura_medido = 0.0
        if captura:
            try:
                captura.release()
            except Exception:
                pass


def bucle_deteccion(ejecucion):
    """Saca cuadros de la cola, los pasa al detector y avisa al panel."""
    global en_marcha, detector_actual, alertas_actual, ultimo_estado, config_actual

    cfg = config_actual
    fuente = cfg.get("FUENTE_CAMARA", 0)
    es_video = isinstance(fuente, str)

    # espera a que la captura abra la fuente para saber sus fps reales
    captura_lista.wait(timeout=10)
    if not en_marcha or ejecucion != num_ejecucion:
        return

    salto_video = max(1, int(cfg.get("SALTO_CUADROS_VIDEO", 3)))
    fps_nominal = fps_captura / salto_video if es_video else fps_captura
    if fps_nominal <= 0:
        fps_nominal = 30.0

    detector = DetectorPersonas()
    # si mientras cargaba el modelo se detuvo o reinicio, este hilo ya no
    # manda y no debe pisar el detector de la ejecucion nueva
    if not en_marcha or ejecucion != num_ejecucion:
        return
    detector_actual = detector
    # En video solo se analiza 1 de cada salto_video cuadros, por eso el
    # detector usa ese fps para que sus tiempos sean segundos reales
    detector_actual.poner_fps(fps_nominal)

    # se le pasan las zonas y umbrales de la config
    detector_actual.roi = cfg.get("ROI", detector_actual.roi)
    detector_actual.cerraduras = list(cfg.get("CERRADURAS", detector_actual.cerraduras))
    detector_actual.puertas = list(cfg.get("PUERTAS", detector_actual.puertas))
    detector_actual.umbral_cerradura = cfg.get("UMBRAL_CERRADURA_S", detector_actual.umbral_cerradura)
    detector_actual.ventana_mirar = cfg.get("VENTANA_MIRAR_S", detector_actual.ventana_mirar)
    detector_actual.giros_mirar = cfg.get("GIROS_MIRAR", detector_actual.giros_mirar)
    detector_actual.umbral_empujon = cfg.get("UMBRAL_EMPUJON_S", detector_actual.umbral_empujon)
    detector_actual.confirmaciones_necesarias = cfg.get("CONFIRMACIONES_TREPAR", detector_actual.confirmaciones_necesarias)
    detector_actual.tiempo_deteccion = cfg.get("TIEMPO_DETECCION_S", detector_actual.tiempo_deteccion)

    alertas_actual = SistemaAlertas()

    hora_ultima_alerta = 0
    espera_alertas = cfg.get("ESPERA_ENTRE_ALERTAS_S", 5)
    modo_deteccion = cfg.get("MODO_DETECCION", "cascada")
    fps_efectivo = fps_nominal
    hora_cuadro_anterior = None
    eventos_activos = set()  # para registrar cada evento solo cuando empieza

    while en_marcha and ejecucion == num_ejecucion:
        try:
            hora_cuadro, cuadro, segundo_video = cola_deteccion.get(timeout=0.2)
        except queue.Empty:
            continue

        # Si la deteccion va justa y se tiran cuadros, el tiempo real entre
        # analisis crece: se ajusta el fps del detector para que los umbrales
        # en segundos sigan midiendo tiempo de reloj
        if hora_cuadro_anterior is not None:
            dt = hora_cuadro - hora_cuadro_anterior
            if 0 < dt < 5.0:
                fps_instante = 1.0 / dt
                if abs(fps_instante - fps_efectivo) > 0.05 * fps_efectivo:
                    fps_efectivo = 0.8 * fps_efectivo + 0.2 * fps_instante
                    detector_actual.poner_fps(fps_efectivo)
        hora_cuadro_anterior = hora_cuadro

        try:
            hay_alarma = detector_actual.detectar(cuadro)

            hay_persona = detector_actual.ultimo_hay_persona
            manos_cerradura = detector_actual.ultimo_manos_cerradura
            alerta_trepar = detector_actual.ultimo_alerta_trepar
            empujon_puerta = detector_actual.ultimo_empujon_puerta
            mirando_alrededor = detector_actual.ultimo_mirando_alrededor
            hay_movimiento = getattr(detector_actual, "hay_movimiento", False)

            # textos de lo que se detecto (van en el correo)
            tipos_alerta = []
            if manos_cerradura:
                tipos_alerta.append(f"Manos en cerradura ({detector_actual.tiempo_cerradura():.1f}s)")
            if alerta_trepar:
                tipos_alerta.append("Postura de escalamiento")
            if empujon_puerta:
                tipos_alerta.append("Empujon a la puerta")
            if mirando_alrededor:
                tipos_alerta.append("Mirando alrededor sospechosamente")
            # cuantas personas vio YOLO (si son varias se avisa en el correo)
            num_personas = getattr(detector_actual, "ultimo_num_personas", 0)
            if tipos_alerta and num_personas >= 2:
                tipos_alerta.append(f"Hay {num_personas} personas en la escena")

            # lo que se le manda al panel
            estado = {
                "hay_persona": hay_persona,
                "num_personas": num_personas,
                "manos_cerradura": manos_cerradura,
                "alerta_trepar": alerta_trepar,
                "empujon_puerta": empujon_puerta,
                "mirando_alrededor": mirando_alrededor,
                "hay_movimiento": hay_movimiento,
                "tiempo_cerradura": detector_actual.tiempo_cerradura(),
                "tiempo_transcurrido": detector_actual.tiempo_transcurrido(),
                "cuadro": cuadros_capturados,
                "fps": round(fps_captura_medido, 1),
                "camara_activa": camara_activa,
                "fuente": str(fuente),
                "modo": modo_deteccion,
                "modo_app": MODO,
                "marca_tiempo": datetime.now().isoformat(),
            }

            if MODO_PRUEBAS:
                # en pruebas tambien van los numeros de las reglas y sus umbrales
                metricas = dict(getattr(detector_actual, "metricas_trepar", {}))
                metricas["confirmaciones"] = detector_actual.confirmaciones_trepar
                estado["metricas"] = metricas
                estado["metricas_mirar"] = dict(getattr(detector_actual, "metricas_mirar", {}))
                estado["segundo_video"] = None if segundo_video is None else round(segundo_video, 1)
                estado["umbrales"] = {
                    "sube_cadera": config.TREPAR_SUBIDA_CADERA_PX,
                    "sube_pies": config.TREPAR_SUBIDA_PIES_PX,
                    "manos_max": config.TREPAR_MANOS_ARRIBA_PX,
                    "confirmaciones": detector_actual.confirmaciones_necesarias,
                    "giros": detector_actual.giros_mirar,
                    "permanencia_s": config.MIRAR_PERMANENCIA_MIN_S,
                    "movimiento": config.MIRAR_MOVIMIENTO_MAX,
                }
                activos = {
                    "Manos en cerradura": manos_cerradura,
                    "Escalamiento": alerta_trepar,
                    "Empujon a la puerta": empujon_puerta,
                    "Mirando alrededor": mirando_alrededor,
                }
                for tipo, activo in activos.items():
                    if activo and tipo not in eventos_activos:
                        datos = {**metricas, **estado["metricas_mirar"]} if tipo == "Mirando alrededor" else metricas
                        _registrar_evento(fuente, segundo_video, tipo, datos)
                        eventos_activos.add(tipo)
                    elif not activo:
                        eventos_activos.discard(tipo)

            with candado_cuadro:
                ultimo_estado = estado

            asyncio.run_coroutine_threadsafe(gestor.mandar_a_todos(estado), bucle_eventos)

            # alarma de verdad: foto + correo (respetando la espera entre alertas)
            if hay_alarma and tipos_alerta:
                ahora = time.time()
                if ahora - hora_ultima_alerta >= espera_alertas:
                    alertas_actual.disparar_alerta(cuadro, tipos_alerta)
                    hora_ultima_alerta = ahora
                    if MODO_PRUEBAS:
                        # esta es la alarma que se manda de verdad (lo que mide
                        # la evaluacion); los otros eventos son senales sueltas
                        _registrar_evento(fuente, segundo_video, "ALARMA", {"tipos": tipos_alerta})
        except Exception as e:
            print(f"Error en deteccion (cuadro {cuadros_capturados}): {e}")


bucle_eventos = None


@asynccontextmanager
async def ciclo_de_vida(_aplicacion: FastAPI):
    # al arrancar carga la config; al cerrar detiene los hilos
    global bucle_eventos, config_actual
    bucle_eventos = asyncio.get_event_loop()
    config_actual = cargar_config()
    marcar_correos_pendientes()
    if MODO_PRUEBAS:
        _aplicar_zonas_video(config_actual.get("FUENTE_CAMARA", 0), config_actual)
    yield
    global en_marcha
    en_marcha = False
    if hilo_captura and hilo_captura.is_alive():
        hilo_captura.join(timeout=3)
    if hilo_deteccion and hilo_deteccion.is_alive():
        hilo_deteccion.join(timeout=3)


aplicacion = FastAPI(title="SafeHome Servidor", lifespan=ciclo_de_vida)

# solo el panel de la app (y localhost) puede hablarle al servidor
ORIGENES_PERMITIDOS = ["tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"]
PATRON_ORIGEN_LOCAL = r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$"


def origen_permitido(origen):
    # sin Origin = no es un navegador (curl, scripts locales): se deja pasar
    import re
    return (not origen or origen in ORIGENES_PERMITIDOS or
            re.match(PATRON_ORIGEN_LOCAL, origen) is not None)


aplicacion.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGENES_PERMITIDOS,
    allow_origin_regex=PATRON_ORIGEN_LOCAL,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Camara-Activa"],
)


@aplicacion.get("/")
async def inicio():
    # para saber si el servidor esta vivo y en que modo
    return {"estado": "ok", "servicio": "SafeHome Servidor", "en_marcha": en_marcha,
            "modo": MODO}


def _buscar_camaras():
    """Revisa /dev/video* y prueba cuales abren con OpenCV."""
    dispositivos = []
    if sys.platform.startswith("linux") and os.path.isdir("/dev"):
        videos = [d for d in os.listdir("/dev") if d.startswith("video")]
        if not videos:
            # si no hay ninguno se prueban la 0 y la 1 directo
            videos = ["video0", "video1"]
        for d in sorted(videos, key=lambda x: int(x[5:] if x[5:].isdigit() else 0)):
            ruta = f"/dev/{d}"
            nombre = ""
            archivo_nombre = f"/sys/class/video4linux/{d}/name"
            if os.path.exists(archivo_nombre):
                try:
                    with open(archivo_nombre) as archivo:
                        nombre = archivo.read().strip()
                except OSError:
                    pass
            indice = int(d[5:]) if d[5:].isdigit() else 0
            disponible = _probar_camara(indice)
            dispositivos.append({
                "indice": indice,
                "ruta": ruta,
                "nombre": nombre or f"Cámara {indice}",
                "disponible": disponible,
            })
    return dispositivos


def _probar_camara(indice):
    """Abre la camara y trata de leer hasta 6 cuadros (algunas tardan en arrancar)."""
    try:
        c = cv2.VideoCapture(indice)
        if not c.isOpened():
            c.release()
            return False
        for _ in range(6):
            ok, img = c.read()
            if ok and img is not None and img.size > 0:
                c.release()
                return True
            time.sleep(0.15)
        c.release()
        return False
    except Exception:
        return False


def _buscar_videos():
    """Lista los videos que hay en la carpeta del backend."""
    carpeta_backend = os.path.dirname(__file__)
    videos = []
    for sub in ("", "videos"):
        base = os.path.join(carpeta_backend, sub)
        if not os.path.isdir(base):
            continue
        for archivo in sorted(os.listdir(base)):
            minusculas = archivo.lower()
            if minusculas.endswith((".mp4", ".avi", ".mov", ".mkv", ".webm", ".mjpeg", ".mjpg")):
                videos.append({
                    "ruta": os.path.join(base, archivo),
                    "nombre": archivo,
                    "grupo": "videos" if sub == "videos" else "raiz",
                })
    return videos


@aplicacion.get("/camaras")
async def obtener_camaras():
    # La vista previa acapara la webcam (V4L2 solo deja 1). Se marca que se
    # estan buscando camaras para que /cuadro no la toque mientras.
    global escaneando_camaras
    with candado_cuadro:
        _cerrar_vista_previa()
        escaneando_camaras = True
    try:
        dispositivos = _buscar_camaras()
        videos = _buscar_videos()
    finally:
        with candado_cuadro:
            escaneando_camaras = False
    return {
        "fuente_actual": config_actual.get("FUENTE_CAMARA", 0),
        "dispositivos": dispositivos,
        "videos": videos,
    }


def _imagen_aviso(texto, x, escala, grosor=2):
    # imagen negra con un letrero (cuando no hay camara)
    imagen = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.putText(imagen, texto, (x, 240), cv2.FONT_HERSHEY_SIMPLEX, escala, (255, 255, 255), grosor)
    return imagen


@aplicacion.get("/cuadro")
async def obtener_cuadro():
    # regresa el ultimo cuadro como JPG (el panel lo pide muchas veces por segundo)
    global camara_activa
    try:
        with candado_cuadro:
            cuadro = ultimo_cuadro.copy() if ultimo_cuadro is not None else None
        if cuadro is not None:
            camara_activa = True
        else:
            cuadro = _cuadro_vista_previa()
        ok, jpg = cv2.imencode('.jpg', cuadro, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if ok:
            return Response(
                content=jpg.tobytes(),
                media_type="image/jpeg",
                headers={"X-Camara-Activa": "1" if camara_activa else "0"},
            )
        return Response(status_code=500)
    except Exception as e:
        cuadro = _imagen_aviso(f"Error: {str(e)}", 50, 0.7)
        ok, jpg = cv2.imencode('.jpg', cuadro, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if ok:
            return Response(
                content=jpg.tobytes(),
                media_type="image/jpeg",
                headers={"X-Camara-Activa": "0"},
            )
        return Response(status_code=500)


def _resolver_fuente(fuente):
    """Convierte la fuente en algo que abra OpenCV: numero de camara o ruta del video."""
    if isinstance(fuente, str):
        s = fuente.strip()
        # ruta completa
        if s.startswith('/') or ('\\' in s):
            return s if os.path.exists(s) else None
        # numero escrito como texto = camara
        if s.isdigit():
            return int(s)
        # nombre de archivo: se busca en videos/ y en la carpeta del backend
        carpeta_backend = os.path.dirname(__file__)
        for base in (os.path.join(carpeta_backend, "videos"), carpeta_backend):
            ruta = os.path.join(base, s)
            if os.path.exists(ruta):
                return ruta
        return None
    return fuente


def _acortar_ruta_video(ruta):
    """Si el video esta dentro del backend se guarda solo su ruta relativa.

    El AppImage se monta en una carpeta temporal distinta cada vez, entonces
    guardar la ruta completa no sirve; el nombre si se mantiene.
    """
    s = str(ruta).strip()
    if not s.startswith('/'):
        return s
    carpeta_backend = os.path.dirname(__file__)
    carpeta_videos = os.path.join(carpeta_backend, "videos")
    for base in (carpeta_videos, carpeta_backend):
        if s == base:
            return os.path.basename(s)
        if s.startswith(base + os.sep):
            return os.path.relpath(s, carpeta_backend)
    return s


def _cuadro_vista_previa():
    """Saca UN cuadro para la vista previa (cuando la vigilancia no esta corriendo)."""
    global captura_vista_previa, camara_activa
    if escaneando_camaras:
        # no se toca la webcam mientras se buscan camaras
        camara_activa = False
        return _imagen_aviso("Detectando cámaras...", 150, 0.8)

    if en_marcha:
        # con la vigilancia prendida la camara es del hilo de captura, aqui no
        # se lee para no robarle cuadros
        camara_activa = False
        return _imagen_aviso("Iniciando cámara...", 150, 0.8)

    if not captura_vista_previa or not captura_vista_previa.isOpened():
        fuente = _resolver_fuente(config_actual.get("FUENTE_CAMARA", 0))
        if fuente is None:
            captura_vista_previa = None
        else:
            try:
                if captura_vista_previa:
                    captura_vista_previa.release()
            except Exception:
                pass
            try:
                captura_vista_previa = cv2.VideoCapture(fuente)
            except Exception:
                captura_vista_previa = None

    if captura_vista_previa and captura_vista_previa.isOpened():
        ok, cuadro = captura_vista_previa.read()
        if ok:
            camara_activa = True
            return cv2.resize(cuadro, (640, 480))
        # si es video y ya se acabo se reinicia (rebobinar a veces falla
        # sin avisar y se congela la vista previa)
        fuente = _resolver_fuente(config_actual.get("FUENTE_CAMARA", 0))
        if isinstance(fuente, str):
            try:
                captura_vista_previa.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, cuadro = captura_vista_previa.read()
                if ok:
                    camara_activa = True
                    return cv2.resize(cuadro, (640, 480))
            except Exception:
                pass
            # no se pudo rebobinar: se abre de nuevo
            try:
                captura_vista_previa.release()
            except Exception:
                pass
            try:
                captura_vista_previa = cv2.VideoCapture(fuente)
            except Exception:
                captura_vista_previa = None
            if captura_vista_previa and captura_vista_previa.isOpened():
                ok, cuadro = captura_vista_previa.read()
                if ok:
                    camara_activa = True
                    return cv2.resize(cuadro, (640, 480))

    camara_activa = False
    return _imagen_aviso("Cámara no disponible", 140, 0.9)


def _cerrar_vista_previa():
    # suelta la camara de la vista previa
    global captura_vista_previa
    if captura_vista_previa:
        try:
            captura_vista_previa.release()
        except Exception:
            pass
        captura_vista_previa = None


@aplicacion.websocket("/ws/estado")
async def websocket_estado(websocket: WebSocket):
    # el panel se conecta aqui para recibir el estado en vivo
    # (el CORS no aplica a websockets, por eso se revisa el origen a mano)
    if not origen_permitido(websocket.headers.get("origin")):
        await websocket.close(code=1008)
        return
    await gestor.conectar(websocket)
    try:
        while True:
            texto = await websocket.receive_text()
            mensaje = json.loads(texto)
            if mensaje.get("tipo") == "latido":
                await websocket.send_json({"tipo": "respuesta_latido"})
    except WebSocketDisconnect:
        gestor.desconectar(websocket)


@aplicacion.post("/control")
async def control(peticion: PeticionControl):
    # iniciar o detener la vigilancia
    global en_marcha, hilo_deteccion, hilo_captura, ultimo_cuadro, ultimo_estado
    global num_ejecucion

    if peticion.accion == "iniciar":
        if not en_marcha:
            en_marcha = True
            ultimo_cuadro = None
            ultimo_estado = {}
            _vaciar_cola_deteccion()
            captura_lista.clear()
            num_ejecucion += 1
            hilo_captura = threading.Thread(target=bucle_captura, args=(num_ejecucion,), daemon=True)
            hilo_deteccion = threading.Thread(target=bucle_deteccion, args=(num_ejecucion,), daemon=True)
            hilo_captura.start()
            hilo_deteccion.start()
            return {"estado": "iniciado"}
        return {"estado": "ya_estaba_corriendo"}

    elif peticion.accion == "detener":
        en_marcha = False
        if hilo_captura and hilo_captura.is_alive():
            hilo_captura.join(timeout=3)
        if hilo_deteccion and hilo_deteccion.is_alive():
            hilo_deteccion.join(timeout=3)
        _vaciar_cola_deteccion()
        return {"estado": "detenido"}

    raise HTTPException(status_code=400, detail="Acción inválida")


@aplicacion.get("/estado")
async def obtener_estado():
    # el ultimo estado del detector
    return ultimo_estado


@aplicacion.get("/configuracion")
async def obtener_configuracion():
    if MODO_PRUEBAS:
        return config_actual
    # la app final no ve la cuenta que manda los correos (ni su contrasena)
    return {k: v for k, v in config_actual.items() if k not in CLAVES_REMITENTE and k != "CORREO_TOKEN"}


@aplicacion.post("/configuracion")
async def cambiar_configuracion(cambio: CambioConfiguracion):
    # guarda lo que mando el panel (se mezcla con lo que ya habia)
    global config_actual
    fuente_anterior = config_actual.get("FUENTE_CAMARA")
    cfg = migrar_configuracion(cambio.configuracion)
    # el correo verificado solo cambia con el codigo; en la app final ningun
    # dato del correo se cambia por aqui (se usa /correo/...)
    for clave in list(cfg):
        if clave in ("CORREO_VERIFICADO", "CORREO_TOKEN") or (not MODO_PRUEBAS and clave.startswith("CORREO_")):
            del cfg[clave]
    # si el video esta dentro del backend se guarda la ruta corta (el
    # AppImage se monta en /tmp distinto cada vez)
    fuente = cfg.get("FUENTE_CAMARA")
    if isinstance(fuente, str):
        cfg["FUENTE_CAMARA"] = _acortar_ruta_video(fuente)
    # se mezcla con lo que ya habia: el formulario no manda las zonas y antes
    # al reemplazar todo se borraban
    nueva = dict(config_actual)
    nueva.update(cfg)
    if MODO_PRUEBAS and nueva.get("FUENTE_CAMARA") != fuente_anterior:
        # se guardan las zonas del video anterior y se cargan las del nuevo
        _guardar_zonas_video(fuente_anterior, config_actual)
        if _aplicar_zonas_video(nueva.get("FUENTE_CAMARA"), nueva):
            _pasar_zonas_al_detector(nueva)
    config_actual = nueva
    guardar_config(config_actual)
    if config_actual.get("FUENTE_CAMARA") != fuente_anterior:
        _cerrar_vista_previa()
        # si la vigilancia esta corriendo la camara la usa el hilo de captura:
        # se cierra al detener y la fuente nueva se usa al volver a iniciar
        if not en_marcha and captura is not None:
            try:
                captura.release()
            except Exception:
                pass
    return {"estado": "guardado"}


@aplicacion.get("/zonas")
async def obtener_zonas():
    return {
        "ROI": config_actual.get("ROI", {}),
        "CERRADURAS": config_actual.get("CERRADURAS", []),
        "PUERTAS": config_actual.get("PUERTAS", []),
    }


@aplicacion.post("/zonas")
async def cambiar_zonas(cambio: CambioZonas):
    # guarda las zonas dibujadas y se las pasa al detector en vivo
    global config_actual
    config_actual.update(cambio.zonas)
    guardar_config(config_actual)
    if MODO_PRUEBAS:
        _guardar_zonas_video(config_actual.get("FUENTE_CAMARA", 0), config_actual)
    _pasar_zonas_al_detector(config_actual)
    return {"estado": "guardado"}


@aplicacion.get("/alertas")
async def obtener_alertas():
    """Historial de alertas, la mas nueva primero."""
    return {"alertas": leer_historial()[::-1]}


@aplicacion.get("/alertas/{id_alerta}/imagen")
async def imagen_alerta(id_alerta: str):
    """Foto que se tomo en la alerta."""
    try:
        ruta = ruta_imagen_alerta(id_alerta)
    except ValueError:
        raise HTTPException(status_code=400, detail="id no valido")
    if not os.path.exists(ruta):
        raise HTTPException(status_code=404, detail="No hay imagen")
    return FileResponse(ruta, media_type="image/jpeg")


@aplicacion.post("/alertas/borrar")
async def borrar_alertas():
    """Borra todo el historial de alertas."""
    borrar_historial()
    return {"estado": "borrado"}


# --- Correo de alertas con verificacion por codigo (app final) ---

verificacion = Verificacion()


def _correo_verificado():
    # el correo guardado, solo si se comprobo con el codigo
    destino = config_actual.get("CORREO_DESTINATARIO", "")
    return destino if destino and destino == config_actual.get("CORREO_VERIFICADO", "") else ""


@aplicacion.get("/correo")
async def estado_correo():
    return {
        "correo": config_actual.get("CORREO_DESTINATARIO", ""),
        "verificado": bool(_correo_verificado()),
        "remitente_listo": envio_listo(),
    }


@aplicacion.post("/correo/codigo")
async def pedir_codigo(peticion: PeticionCorreo):
    """Manda un codigo de 4 digitos al correo actual o al nuevo."""
    actual = _correo_verificado()
    if peticion.paso == "actual":
        if not actual:
            raise HTTPException(status_code=400, detail="No hay un correo verificado")
        destino = actual
    elif peticion.paso == "nuevo":
        destino = (peticion.correo or "").strip().lower()
        if not correo_valido(destino):
            raise HTTPException(status_code=400, detail="Escribe un correo válido")
        if destino == actual:
            raise HTTPException(status_code=400, detail="Ese ya es tu correo verificado")
        # para cambiar un correo verificado primero se confirma el anterior
        if actual and not verificacion.actual_confirmado():
            raise HTTPException(status_code=403, detail="Primero confirma tu correo actual")
    else:
        raise HTTPException(status_code=400, detail="Paso inválido")

    try:
        codigo = verificacion.nuevo_codigo(peticion.paso, destino)
    except ValueError as e:
        raise HTTPException(status_code=429, detail=str(e))
    try:
        # el envio tarda unos segundos: se hace en otro hilo. Con el servicio
        # en la nube el codigo lo genera y lo revisa el servicio
        if servicio_activo():
            await asyncio.to_thread(servicio_pedir_codigo, destino)
        else:
            await asyncio.to_thread(enviar_codigo, destino, codigo)
    except RuntimeError as e:
        verificacion.descartar(peticion.paso)
        raise HTTPException(status_code=502, detail=str(e))
    return {"estado": "enviado", "correo": destino}


@aplicacion.post("/correo/confirmar")
async def confirmar_codigo(peticion: PeticionCorreo):
    """Revisa el codigo. En el paso "nuevo" guarda el correo como verificado."""
    global config_actual
    if peticion.paso not in ("actual", "nuevo"):
        raise HTTPException(status_code=400, detail="Paso inválido")
    if peticion.paso == "nuevo" and _correo_verificado() and not verificacion.actual_confirmado():
        raise HTTPException(status_code=403, detail="Primero confirma tu correo actual")
    token = ""
    try:
        if servicio_activo():
            correo = verificacion.pendiente(peticion.paso)
            token = await asyncio.to_thread(servicio_confirmar, correo, (peticion.codigo or "").strip())
            verificacion.aceptar(peticion.paso)
        else:
            correo = verificacion.confirmar(peticion.paso, peticion.codigo or "")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    if peticion.paso == "nuevo":
        config_actual = dict(config_actual, CORREO_DESTINATARIO=correo, CORREO_VERIFICADO=correo,
                             CORREO_TOKEN=token)
        guardar_config(config_actual)
        verificacion.reiniciar()
    return {"estado": "confirmado", "correo": correo}


@aplicacion.post("/correo/cancelar")
async def cancelar_cambio_correo():
    verificacion.reiniciar()
    return {"estado": "cancelado"}


@aplicacion.get("/pruebas/eventos")
async def obtener_eventos():
    """Ultimos eventos del registro (solo app de pruebas)."""
    if not MODO_PRUEBAS:
        raise HTTPException(status_code=404, detail="Solo en la app de pruebas")
    filas = []
    try:
        with open(ARCHIVO_REGISTRO_EVENTOS, newline="") as archivo:
            filas = list(csv.DictReader(archivo))
    except OSError:
        pass
    return {"archivo": ARCHIVO_REGISTRO_EVENTOS, "eventos": filas[-30:][::-1]}


if __name__ == "__main__":
    uvicorn.run(aplicacion, host="127.0.0.1", port=8765, log_level="info")
