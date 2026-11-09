import json
import os

# El AppImage corre desde una carpeta temporal (/tmp/.mount_XXXX) que cambia
# cada vez, por eso la config del usuario se guarda en su HOME.
# Modo de la app: "final" (normal) o "pruebas". La de pruebas usa su propia
# carpeta para no tocarle nada a la final.
MODO = os.environ.get("SAFEHOME_MODO", "final").strip().lower()
MODO_PRUEBAS = MODO == "pruebas"
_carpeta_final = os.path.join(os.path.expanduser("~"), ".config", "SafeHome")
CARPETA_CONFIG = os.environ.get(
    "SAFEHOME_CARPETA_CONFIG",
    _carpeta_final + "-Pruebas" if MODO_PRUEBAS else _carpeta_final,
)
ARCHIVO_CONFIG_USUARIO = os.path.join(CARPETA_CONFIG, "config.json")
# config.json que viene junto al backend (la base de inicio)
ARCHIVO_CONFIG_BASE = os.path.join(os.path.dirname(__file__), "config.json")

# Cuenta que MANDA los correos (usuario, contrasena, servidor). Va en un archivo
# aparte que comparten las dos apps; la app final nunca la pide ni la muestra.
CLAVES_REMITENTE = ("CORREO_USUARIO", "CORREO_CONTRASENA", "CORREO_SERVIDOR_SMTP", "CORREO_PUERTO_SMTP")
ARCHIVO_REMITENTE = os.path.join(
    CARPETA_CONFIG if "SAFEHOME_CARPETA_CONFIG" in os.environ else _carpeta_final,
    "remitente.json")
# respaldo: un remitente.json puesto junto al backend (no se sube al repositorio)
ARCHIVO_REMITENTE_BASE = os.path.join(os.path.dirname(__file__), "remitente.json")

# Nombres viejos (en ingles) de las claves -> nombres nuevos. Sirve para que
# las configs que ya estaban guardadas se sigan leyendo bien.
NOMBRES_ANTERIORES = {
    "LOCK_TIME_THRESHOLD": "UMBRAL_CERRADURA_S",
    "LOOK_AROUND_THRESHOLD": "VENTANA_MIRAR_S",
    "LOOK_AROUND_TURNS": "GIROS_MIRAR",
    "PUSH_TIME_THRESHOLD": "UMBRAL_EMPUJON_S",
    "DETECTION_TIME": "TIEMPO_DETECCION_S",
    "CLIMB_CONFIRM_SAMPLES": "CONFIRMACIONES_TREPAR",
    "POSE_FRAME_SKIP": "SALTO_CUADROS_POSE",
    "POSE_FRAME_SIZE": "TAMANO_IMAGEN_POSE",
    "CLIMB_WINDOW_SECONDS": "VENTANA_TREPAR_S",
    "CLIMB_MIN_RISE_PX": "TREPAR_SUBIDA_CADERA_PX",
    "CLIMB_MIN_BOTTOM_PX": "TREPAR_SUBIDA_PIES_PX",
    "CLIMB_DOOR_MARGIN": "TREPAR_MARGEN_PUERTA_PX",
    "CLIMB_HOLD_SECONDS": "TREPAR_SOSTENER_S",
    "CLIMB_HANDS_UP_PX": "TREPAR_MANOS_ARRIBA_PX",
    "CLIMB_MOTION_MIN_SECONDS": "TREPAR_MOVIMIENTO_MIN_S",
    "CLIMB_POSTURE_NEEDS_RISE": "TREPAR_POSTURA_EXIGE_SUBIDA",
    "CLIMB_MOTION_MIN_DOOR_FRAC": "TREPAR_MOVIMIENTO_FRAC_PUERTA",
    "LOOK_TURN_MIN": "MIRAR_GIRO_MIN",
    "LOOK_DOOR_MARGIN": "MIRAR_MARGEN_PUERTA_PX",
    "LOOK_MAX_MOVE": "MIRAR_MOVIMIENTO_MAX",
    "LOOK_MIN_DWELL": "MIRAR_PERMANENCIA_MIN_S",
    "LOOK_CONTACT_SECONDS": "MIRAR_CONTACTO_S",
    "PERSON_PERSISTENCE_FRAMES": "CUADROS_PERSISTENCIA_PERSONA",
    "DETECTION_MODE": "MODO_DETECCION",
    "VIDEO_FRAME_SKIP": "SALTO_CUADROS_VIDEO",
    "SHOW_ZONES": "MOSTRAR_ZONAS",
    "CAMERA_SOURCE": "FUENTE_CAMARA",
    "FRAME_WIDTH": "ANCHO_CUADRO",
    "FRAME_HEIGHT": "ALTO_CUADRO",
    "SAVE_IMAGES": "GUARDAR_IMAGENES",
    "IMAGE_LOG_DIR": "CARPETA_EVIDENCIAS",
    "ALERT_COOLDOWN": "ESPERA_ENTRE_ALERTAS_S",
    "EMAIL_SMTP_SERVER": "CORREO_SERVIDOR_SMTP",
    "EMAIL_SMTP_PORT": "CORREO_PUERTO_SMTP",
    "EMAIL_USER": "CORREO_USUARIO",
    "EMAIL_PASSWORD": "CORREO_CONTRASENA",
    "EMAIL_TO": "CORREO_DESTINATARIO",
    "ALERT_MESSAGE": "MENSAJE_ALERTA",
}


def migrar_configuracion(datos):
    """Pasa una config vieja (claves en ingles) a los nombres nuevos."""
    if not isinstance(datos, dict):
        return datos
    nuevos = {}
    for clave, valor in datos.items():
        nuevos[NOMBRES_ANTERIORES.get(clave, clave)] = valor
    # el ROI antes usaba width/height, ahora ancho/alto
    roi = nuevos.get("ROI")
    if isinstance(roi, dict):
        roi = dict(roi)
        if "width" in roi:
            roi["ancho"] = roi.pop("width")
        if "height" in roi:
            roi["alto"] = roi.pop("height")
        nuevos["ROI"] = roi
    # el modo completo antes se llamaba "cascade"
    if nuevos.get("MODO_DETECCION") == "cascade":
        nuevos["MODO_DETECCION"] = "cascada"
    return nuevos


def _copiar(valor):
    # copia listas y diccionarios para no modificar los originales de config.py
    if isinstance(valor, list):
        return [dict(v) if isinstance(v, dict) else v for v in valor]
    if isinstance(valor, dict):
        return dict(valor)
    return valor


def valores_por_defecto():
    """Saca todas las constantes en MAYUSCULAS de config.py en un diccionario."""
    import config as base
    cfg = {}
    for clave in dir(base):
        if clave.isupper():
            cfg[clave] = _copiar(getattr(base, clave))
    return cfg


def _leer_json(ruta):
    # lee un json y si no existe o esta roto regresa None
    try:
        with open(ruta, "r") as archivo:
            return json.load(archivo)
    except (OSError, json.JSONDecodeError):
        return None


def cargar():
    """Arma la config final: defaults + lo que haya guardado el usuario."""
    import config as base
    cfg = valores_por_defecto()
    # Primero la config del usuario (la que persiste), si no la base
    guardada = _leer_json(ARCHIVO_CONFIG_USUARIO)
    if guardada is None and MODO_PRUEBAS:
        # Primera vez en pruebas: arranca con la config de la app final
        guardada = _leer_json(os.path.join(_carpeta_final, "config.json"))
    if guardada is None:
        guardada = _leer_json(ARCHIVO_CONFIG_BASE)
    if guardada:
        for clave, valor in migrar_configuracion(guardada).items():
            if clave in cfg and clave not in CLAVES_REMITENTE:
                cfg[clave] = valor
                # tambien se cambia en el modulo config para que el detector lo vea
                setattr(base, clave, valor)
    # la cuenta que manda los correos sale de remitente.json (si existe)
    remitente = _leer_json(ARCHIVO_REMITENTE) or _leer_json(ARCHIVO_REMITENTE_BASE)
    if isinstance(remitente, dict):
        for clave in CLAVES_REMITENTE:
            if clave in remitente:
                cfg[clave] = remitente[clave]
                setattr(base, clave, remitente[clave])
    return cfg


def _guardar_remitente(cfg):
    # solo la app de pruebas puede cambiar la cuenta que manda los correos
    datos = {clave: cfg.get(clave) for clave in CLAVES_REMITENTE}
    try:
        os.makedirs(os.path.dirname(ARCHIVO_REMITENTE), exist_ok=True)
        with open(ARCHIVO_REMITENTE, "w") as archivo:
            json.dump(datos, archivo, indent=2)
        os.chmod(ARCHIVO_REMITENTE, 0o600)  # tiene la contrasena: solo el dueno
    except OSError as e:
        print(f"No se pudo guardar remitente.json: {e}")


def guardar(cfg):
    """Guarda la config en el HOME del usuario (o junto al backend si no se puede)."""
    import config as base
    para_json = {}
    for clave, valor in cfg.items():
        if clave.isupper():
            setattr(base, clave, valor)
            if clave in CLAVES_REMITENTE:
                continue  # la cuenta remitente no va en config.json
            try:
                json.dumps(valor)
                para_json[clave] = valor
            except (TypeError, OverflowError):
                para_json[clave] = str(valor)
    if MODO_PRUEBAS:
        _guardar_remitente(cfg)
    try:
        os.makedirs(CARPETA_CONFIG, exist_ok=True)
        with open(ARCHIVO_CONFIG_USUARIO, "w") as archivo:
            json.dump(para_json, archivo, indent=2)
        try:
            os.chmod(ARCHIVO_CONFIG_USUARIO, 0o600)
        except OSError:
            pass
    except OSError:
        # si no se puede escribir en el HOME (AppImage) se guarda en local
        with open(ARCHIVO_CONFIG_BASE, "w") as archivo:
            json.dump(para_json, archivo, indent=2)
