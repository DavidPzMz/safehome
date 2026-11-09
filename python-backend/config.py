import json
import os

# Valores por defecto del sistema. Si existe config.json se sobreescriben con
# lo que tenga ese archivo (y luego con la config guardada del usuario).

# --- Zonas de la imagen (en pixeles, sobre la imagen de 640x480) ---

# ROI = region de interes, el cuadro grande donde se busca a la persona
ROI = {
    "x": 78,
    "y": 105,
    "ancho": 299,
    "alto": 391
}

# Recuadros donde esta la cerradura (si las munecas caen aqui = manos en la cerradura)
CERRADURAS = [
    {"x1": 107, "y1": 327, "x2": 141, "y2": 380},
    {"x1": 87, "y1": 331, "x2": 108, "y2": 388},
]

# Recuadros de la puerta (para empujones, trepar y mirar alrededor)
PUERTAS = [
    {"x1": 85, "y1": 145, "x2": 155, "y2": 482},
]

# --- Umbrales de tiempo ---

# Abrir con llave toma como 2-5 s, forzar la cerradura dura mas (8-20 s en
# las pruebas). En la evaluacion funciono bien entre 3 y 6 s.
UMBRAL_CERRADURA_S = 4.5
# Ventana de tiempo (s) en la que se cuentan los giros de cabeza
VENTANA_MIRAR_S = 10
# Cuantas veces tiene que voltear la cabeza para contar como "mirando alrededor"
GIROS_MIRAR = 3
# Hombros/cadera pegados a la puerta. Pasar o recargarse al entrar dura menos
# de 4 s; forcejear la puerta dura 17-25 s. Funciona bien entre 3 y 8 s.
UMBRAL_EMPUJON_S = 5
# Segundos que debe durar la actividad sospechosa para mandar la alarma
TIEMPO_DETECCION_S = 3
# Cuantos analisis seguidos tienen que ver a la persona trepando para creerle
CONFIRMACIONES_TREPAR = 2

# --- Rendimiento ---

# YOLO solo corre 1 de cada N cuadros (los demas reusan el ultimo resultado)
SALTO_CUADROS_POSE = 5
# Tamano de imagen que se le pasa a YOLO
TAMANO_IMAGEN_POSE = 640

# --- Trepar: el cuerpo (cadera y pies) sube de forma sostenida junto a la puerta ---

# Segundos hacia atras que se miran para ver si el cuerpo va subiendo
VENTANA_TREPAR_S = 3.0
# Cuantos px tiene que subir la cadera
TREPAR_SUBIDA_CADERA_PX = 18
# Cuantos px tienen que subir los pies
TREPAR_SUBIDA_PIES_PX = 25
# Que tan lejos de la puerta (px) todavia cuenta como "junto a la puerta"
TREPAR_MARGEN_PUERTA_PX = 45
# Mantiene la alerta prendida este rato aunque YOLO pierda la pose un momento
# (pasa seguido mientras la persona va subiendo)
TREPAR_SOSTENER_S = 1.5
# Al trepar la persona se agarra: las munecas quedan arriba de la cadera.
# Alejarse caminando sube los pies en la imagen (perspectiva) pero no las manos.
TREPAR_MANOS_ARRIBA_PX = 40
# Respaldo por movimiento (sin esqueleto): la subida tiene que durar esto,
# para no confundirla con alguien que va entrando al cuadro
TREPAR_MOVIMIENTO_MIN_S = 1.0
# Solo la postura (manos o rodilla arriba) no basta para trepar: bajarse de la
# bici o estirarse para abrir tambien la hacen. Se pide que el cuerpo suba.
TREPAR_POSTURA_EXIGE_SUBIDA = True
# Respaldo por movimiento: la silueta debe medir al menos esta fraccion del
# alto de la puerta (asi se descarta gente lejos que se encima en la imagen)
TREPAR_MOVIMIENTO_FRAC_PUERTA = 0.4

# --- Mirando alrededor: persona quieta junto a la puerta volteando la cabeza ---
# El giro se mide con la nariz contra el centro de los hombros, en fracciones
# del largo del torso, asi caminar no cuenta como voltear.

MIRAR_GIRO_MIN = 0.12            # lo minimo que tiene que girar la cabeza (fraccion del torso)
MIRAR_MARGEN_PUERTA_PX = 60      # px alrededor de la puerta que cuentan como "junto a"
MIRAR_MOVIMIENTO_MAX = 1.0       # cuanto se puede mover la cadera y seguir "quieta" (en torsos)
MIRAR_PERMANENCIA_MIN_S = 2.0    # segundos que tiene que llevar ahi quieta
# Mirar alrededor solo cuenta si toco la cerradura o la puerta en estos
# ultimos segundos (platicar en la entrada tambien hace voltear la cabeza)
MIRAR_CONTACTO_S = 5.0

# Cuadros que se sigue "viendo" a la persona aunque HOG la pierda un momento
CUADROS_PERSISTENCIA_PERSONA = 10
# "cascada" = HOG + YOLO-pose (completo), "hog" = solo HOG (rapido)
MODO_DETECCION = "cascada"
# En videos solo se analiza 1 de cada N cuadros
SALTO_CUADROS_VIDEO = 3
# Dibujar las zonas encima del video
MOSTRAR_ZONAS = True

# --- Camara ---

# Numero de camara (0, 1...) o ruta/nombre de un video
FUENTE_CAMARA = 0
ANCHO_CUADRO = 640
ALTO_CUADRO = 480

# --- Alertas ---

GUARDAR_IMAGENES = True
CARPETA_EVIDENCIAS = "logs"
# Segundos minimos entre una alarma y otra
ESPERA_ENTRE_ALERTAS_S = 5

CORREO_SERVIDOR_SMTP = "smtp.gmail.com"
CORREO_PUERTO_SMTP = 465
CORREO_USUARIO = ""
# Contrasena de aplicacion de Gmail. NO va en el codigo: se agarra de la
# variable de entorno VIGILANCIA_CORREO_CONTRASENA o de config.json (local).
# Se genera en https://myaccount.google.com/apppasswords
CORREO_CONTRASENA = os.environ.get("VIGILANCIA_CORREO_CONTRASENA", "")
CORREO_DESTINATARIO = ""
MENSAJE_ALERTA = "ALERTA: Actividad sospechosa detectada en la puerta"

# --- Lee config.json y pisa los valores de arriba con lo que traiga ---
_ruta_json = os.path.join(os.path.dirname(__file__), "config.json")
if os.path.exists(_ruta_json):
    try:
        with open(_ruta_json) as _archivo:
            _cambios = json.load(_archivo)
        for _clave, _valor in _cambios.items():
            # solo se aceptan claves que ya existen arriba (en mayusculas)
            if _clave.isupper() and _clave in globals():
                globals()[_clave] = _valor
    except Exception:
        pass
