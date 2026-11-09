import cv2
import numpy as np
from collections import deque
from config import (
    ROI, CERRADURAS, PUERTAS,
    TIEMPO_DETECCION_S, UMBRAL_CERRADURA_S, CONFIRMACIONES_TREPAR,
    VENTANA_MIRAR_S, GIROS_MIRAR, UMBRAL_EMPUJON_S,
    SALTO_CUADROS_POSE, TAMANO_IMAGEN_POSE, MODO_DETECCION,
    CUADROS_PERSISTENCIA_PERSONA, VENTANA_TREPAR_S, TREPAR_SUBIDA_CADERA_PX,
    TREPAR_SUBIDA_PIES_PX, TREPAR_MARGEN_PUERTA_PX,
    TREPAR_SOSTENER_S, TREPAR_MANOS_ARRIBA_PX, TREPAR_MOVIMIENTO_MIN_S,
    MIRAR_GIRO_MIN, MIRAR_MARGEN_PUERTA_PX, MIRAR_MOVIMIENTO_MAX, MIRAR_PERMANENCIA_MIN_S,
)
# algunos valores se leen en vivo desde aqui para poder cambiarlos sin reiniciar
import config as _cfg


class DetectorPersonas:
    """Aqui esta toda la logica: busca a la persona y decide si hace algo sospechoso.

    Se usan 3 cosas juntas: HOG (persona rapido), YOLO-pose (puntos del cuerpo)
    y diferencia entre cuadros (movimiento, sirve cuando hay sombra).
    """

    def __init__(self):
        # zonas y umbrales (el servidor los cambia con los de la config)
        self.roi = ROI
        self.cerraduras = CERRADURAS
        self.puertas = PUERTAS
        self.tiempo_deteccion = TIEMPO_DETECCION_S
        self.umbral_cerradura = UMBRAL_CERRADURA_S
        self.ventana_mirar = VENTANA_MIRAR_S
        self.giros_mirar = GIROS_MIRAR
        self.umbral_empujon = UMBRAL_EMPUJON_S
        self.num_cuadro = 0
        self.fps = 30
        self.detectando = False
        self.cuadro_inicio_actividad = None
        self.cuadro_ultima_actividad = None

        # HOG para encontrar personas rapido
        self.hog = cv2.HOGDescriptor()
        self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

        # YOLOv8-pose para sacar los puntos del cuerpo
        self.modelo = None
        self.modelo_cargado = False
        self._cargar_modelo_pose()

        # Detector de movimiento comparando un cuadro con el anterior (jala con sombras)
        self.gris_anterior = None
        self.hay_movimiento = False
        self.caja_movimiento = None

        # Mirando alrededor: cuando volteo la cabeza, hacia donde, el extremo
        # del giro, donde estaba la cadera y desde cuando esta quieta
        self.cuadros_giro_cabeza = deque()
        self.ultima_direccion = 0
        self.giro_referencia = None
        self.caderas_mirar = deque()      # (cuadro, cadera_x, largo_torso)
        self.quieta_desde = None
        self.mirar_activo = False         # se queda prendido mientras siga ahi quieta
        self.metricas_mirar = {}
        self.cuadro_ultimo_contacto = None  # ultima vez que toco cerradura o puerta

        # YOLO no corre en todos los cuadros, se guarda el ultimo resultado
        self.ultimo_resultado_pose = None
        self.contador_cuadros_pose = 0

        # Contadores de cerradura y empujon (aguantan unos cuadros perdidos)
        self.cuadro_inicio_cerradura = None
        self.cuadros_sin_cerradura = 0
        self.max_cuadros_perdidos = 3  # cuadros sin verla antes de reiniciar el conteo
        self.cuadro_inicio_empujon = None
        self.cuadros_sin_empujon = 0

        # Lo que salio en el ultimo cuadro (lo usa el servidor para mostrarlo)
        self.ultimo_hay_persona = False
        self.ultimas_personas = []
        self.ultimos_puntos = []
        self.ultimo_manos_cerradura = False
        self.ultimo_alerta_trepar = False
        self.ultimo_empujon_puerta = False
        self.ultimo_mirando_alrededor = False
        self.ultimo_hay_persona_hog = False
        self.ultimo_hay_persona_yolo = False
        self.ultima_caja_movimiento = None

        # Ultima caja donde HOG vio a la persona
        self.ultima_caja_persona = None

        # Historiales de altura para detectar si va trepando
        self.historial_cuerpo = []       # (cuadro, nariz_y, cadera_y, nariz_x, pies_y, manos_arriba) de YOLO
        self.historial_movimiento = []   # (cuadro, arriba_y, centro_y) del movimiento
        self.espera_trepar = 0
        self.cuadros_espera_trepar = 60  # 2 segundos a 30fps
        self.confirmaciones_trepar = 0
        self.confirmaciones_necesarias = CONFIRMACIONES_TREPAR
        # hasta que cuadro se sostiene la alerta de trepar
        self.sostener_trepar_hasta = -1
        # numeros que uso la regla de trepar (la app de pruebas los ensena)
        self.metricas_trepar = {}

        # Varias personas: cada una tiene su propio "expediente" con sus
        # historiales (trepar, mirar, empujon) para que no se revuelvan
        self.expedientes = {}            # id -> datos de esa persona
        self.siguiente_id = 1
        self.id_principal = None         # la mas grande, sus metricas se ensenan
        self.ultimo_num_personas = 0     # cuantas personas vio YOLO

        # Si HOG pierde a la persona un momento (ej. se voltea) se sigue
        # contando como que esta ahi por unos cuadros
        self.cuadros_persistencia = CUADROS_PERSISTENCIA_PERSONA
        self.cuadros_sin_persona = 0
        self.persona_confirmada = False

    def _cargar_modelo_pose(self):
        # carga YOLO-pose, si no esta instalado se queda solo con HOG
        if MODO_DETECCION != "cascada":
            print(f"Modo: {MODO_DETECCION} (solo HOG)")
            return
        try:
            from ultralytics import YOLO
            print("Cargando modelo YOLO-pose...")
            self.modelo = YOLO('yolov8n-pose.pt')
            self.modelo_cargado = True
            print("Modelo YOLO-pose cargado correctamente")
        except ImportError:
            print("⚠️ ultralytics no instalado. Solo se usara deteccion HOG.")
            print("   Para activar analisis de postura: pip install ultralytics")

    def poner_fps(self, fps):
        # los tiempos en segundos se convierten a cuadros con esto
        self.fps = fps

    def obtener_roi(self):
        # regresa la ROI como esquinas (x1, y1, x2, y2)
        return (self.roi["x"], self.roi["y"],
                self.roi["x"] + self.roi["ancho"],
                self.roi["y"] + self.roi["alto"])

    def dibujar_roi(self, cuadro):
        # pinta la ROI: verde si todo bien, rojo si esta detectando algo
        x1, y1, x2, y2 = self.obtener_roi()
        color = (0, 0, 255) if self.detectando else (0, 255, 0)
        cv2.rectangle(cuadro, (x1, y1), (x2, y2), color, 2)
        texto = "PERSONA DETECTADA" if self.detectando else "ZONA ROI"
        cv2.putText(cuadro, texto, (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

    def dibujar_zonas(self, cuadro, zonas, color, prefijo):
        # pinta los recuadros de cerraduras o puertas con su numero
        for i, zona in enumerate(zonas):
            cv2.rectangle(cuadro, (zona["x1"], zona["y1"]), (zona["x2"], zona["y2"]), color, 2)
            cv2.putText(cuadro, f"{prefijo} {i+1}", (zona["x1"], zona["y1"] - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)

    def buscar_persona_hog(self, cuadro):
        """Busca personas con HOG dentro de la ROI."""
        x1, y1, x2, y2 = self.obtener_roi()
        recorte = cuadro[y1:y2, x1:x2]

        if recorte.size == 0:
            return False, []

        (personas, _) = self.hog.detectMultiScale(
            recorte, winStride=(8, 8), padding=(16, 16), scale=1.05
        )

        # quita las detecciones falsas: muy chiquitas o con forma rara
        filtradas = []
        for (px, py, pw, ph) in personas:
            # muy chica para ser una persona
            if pw < 30 or ph < 60:
                continue
            # una persona es mas alta que ancha
            proporcion = pw / ph
            if proporcion < 0.15 or proporcion > 0.85:
                continue
            filtradas.append((px, py, pw, ph))

        la_vio_hog = len(filtradas) > 0

        # si HOG la ve, se reinicia el contador de persistencia
        if la_vio_hog:
            self.cuadros_sin_persona = 0
            self.persona_confirmada = True
            self.ultima_caja_persona = np.array(filtradas)
            return True, np.array(filtradas)

        # si no la ve pero estaba hace poquito, se sigue contando que esta
        if self.persona_confirmada:
            self.cuadros_sin_persona += 1
            if self.cuadros_sin_persona < self.cuadros_persistencia:
                return True, self.ultima_caja_persona if self.ultima_caja_persona is not None else []

        # ahora si ya se fue
        self.persona_confirmada = False
        self.cuadros_sin_persona = 0
        self.ultima_caja_persona = None
        return False, []

    def _punto_en_zona(self, x, y, zona):
        # checa si el punto cae dentro del recuadro
        return zona["x1"] < x < zona["x2"] and zona["y1"] < y < zona["y2"]

    def _cerca_de_puerta(self, x, y, margen=30):
        """Checa si el punto esta dentro o cerquita de alguna puerta."""
        for puerta in self.puertas:
            px1 = puerta["x1"] - margen
            py1 = puerta["y1"] - margen
            px2 = puerta["x2"] + margen
            py2 = puerta["y2"] + margen
            if px1 < x < px2 and py1 < y < py2:
                return True
        return False

    def _recortar_historial_cuerpo(self):
        # tira las muestras que ya quedaron fuera de la ventana de tiempo
        cuadros_ventana = max(2, int(VENTANA_TREPAR_S * self.fps))
        mas_vieja = self.num_cuadro - cuadros_ventana
        while self.historial_cuerpo and self.historial_cuerpo[0][0] < mas_vieja:
            self.historial_cuerpo.pop(0)

    def _movimiento_indica_trepar(self):
        """Ve si la mancha de movimiento va subiendo (respaldo cuando no hay esqueleto).

        Pide al menos 3 muestras y descarta saltos imposibles: si el recuadro
        brinca cientos de pixeles en decimas de segundo es ruido (sombras,
        cambios de luz), no alguien trepando.
        """
        # tira muestras viejas (fuera de la ventana)
        cuadros_ventana = max(2, int(VENTANA_TREPAR_S * self.fps))
        mas_vieja = self.num_cuadro - cuadros_ventana
        while self.historial_movimiento and self.historial_movimiento[0][0] < mas_vieja:
            self.historial_movimiento.pop(0)
        if len(self.historial_movimiento) < 3:
            return False
        # la subida tiene que durar: alguien que entra al cuadro "crece" de golpe
        duracion = (self.historial_movimiento[-1][0] - self.historial_movimiento[0][0]) / self.fps
        if duracion < TREPAR_MOVIMIENTO_MIN_S:
            return False
        arriba_antes = self.historial_movimiento[0][1]
        centro_antes = self.historial_movimiento[0][2]
        arriba_ahora = self.historial_movimiento[-1][1]
        centro_ahora = self.historial_movimiento[-1][2]
        subio_arriba = arriba_antes - arriba_ahora
        subio_centro = centro_antes - centro_ahora
        return 20 < subio_arriba <= 50 and 8 < subio_centro <= 60

    def _cuerpo_trepando(self):
        """Ve si la cadera y los pies van subiendo junto a la puerta.

        Se promedian las 2 primeras muestras y las 2 ultimas para que un solo
        cuadro con la pose mal no cuente.
        """
        if len(self.historial_cuerpo) < 4:
            self.metricas_trepar = {"muestras": len(self.historial_cuerpo)}
            return False

        antiguas = self.historial_cuerpo[:2]
        recientes = self.historial_cuerpo[-2:]
        cadera_antes = sum(s[2] for s in antiguas) / 2.0
        cadera_ahora = sum(s[2] for s in recientes) / 2.0
        pies_antes = sum(s[4] for s in antiguas) / 2.0
        pies_ahora = sum(s[4] for s in recientes) / 2.0

        cerca_puerta = self._cerca_de_puerta(recientes[-1][3], cadera_ahora,
                                             margen=TREPAR_MARGEN_PUERTA_PX)
        manos_max = max(s[5] for s in self.historial_cuerpo)
        self.metricas_trepar = {
            "muestras": len(self.historial_cuerpo),
            "sube_cadera": round(cadera_antes - cadera_ahora),
            "sube_pies": round(pies_antes - pies_ahora),
            "manos_max": round(manos_max),
            "cerca_puerta": bool(cerca_puerta),
        }
        if not cerca_puerta:
            return False

        # Los pies tambien tienen que subir: caminar hacia la camara sube la
        # cadera en la imagen pero los pies siguen abajo. Alejarse sube cadera
        # y pies, pero el que trepa ademas levanta las manos para agarrarse.
        return (cadera_antes - cadera_ahora >= TREPAR_SUBIDA_CADERA_PX and
                pies_antes - pies_ahora >= TREPAR_SUBIDA_PIES_PX and
                manos_max >= TREPAR_MANOS_ARRIBA_PX)

    def _reiniciar_mirar(self):
        # borra todo lo de "mirando alrededor" para empezar de cero
        self.giro_referencia = None
        self.caderas_mirar.clear()
        self.quieta_desde = None
        self.mirar_activo = False
        self.cuadros_giro_cabeza.clear()
        self.metricas_mirar = {}

    def _mirando_alrededor(self, nariz, hombro_izq, hombro_der, y_cadera, x_cadera):
        """Persona quieta junto a la puerta volteando la cabeza de un lado a otro."""
        x_n, y_n = int(nariz[0]), int(nariz[1])
        x_hi, x_hd = int(hombro_izq[0]), int(hombro_der[0])
        if 0 in (x_n, y_n, x_hi, x_hd) or y_cadera <= 0 or x_cadera <= 0:
            return False

        torso = max(abs(y_cadera - y_n), 20.0)          # largo del torso
        giro = (x_n - (x_hi + x_hd) / 2.0) / torso      # nariz contra el centro de los hombros

        cerca = self._cerca_de_puerta(x_cadera, y_cadera, margen=MIRAR_MARGEN_PUERTA_PX)
        if not cerca:
            self._reiniciar_mirar()
            self.metricas_mirar = {"cerca_puerta": False}
            return False

        # Quieta = la cadera casi no se mueve dentro de la ventana de tiempo
        ventana = self.ventana_mirar * self.fps
        self.caderas_mirar.append((self.num_cuadro, x_cadera, torso))
        while self.caderas_mirar and self.num_cuadro - self.caderas_mirar[0][0] > ventana:
            self.caderas_mirar.popleft()
        xs = [c[1] for c in self.caderas_mirar]
        movimiento = (max(xs) - min(xs)) / torso
        if movimiento > MIRAR_MOVIMIENTO_MAX:
            # va caminando: se empieza de cero desde aqui
            self.quieta_desde = self.num_cuadro
            self.cuadros_giro_cabeza.clear()
            self.giro_referencia = None
            self.mirar_activo = False
        elif self.quieta_desde is None:
            self.quieta_desde = self.num_cuadro
        permanencia = (self.num_cuadro - self.quieta_desde) / self.fps

        # Cuenta un giro cuando la cabeza se regresa al otro lado al menos
        # MIRAR_GIRO_MIN desde el extremo anterior (asi el temblor no cuenta)
        if self.giro_referencia is None:
            self.giro_referencia = giro
            self.ultima_direccion = 0
        elif self.ultima_direccion == 0:
            if abs(giro - self.giro_referencia) >= MIRAR_GIRO_MIN:
                self.ultima_direccion = 1 if giro > self.giro_referencia else -1
                self.giro_referencia = giro
        elif (giro - self.giro_referencia) * self.ultima_direccion > 0:
            self.giro_referencia = giro                      # sigue volteando pal mismo lado
        elif abs(giro - self.giro_referencia) >= MIRAR_GIRO_MIN:
            self.cuadros_giro_cabeza.append(self.num_cuadro)
            self.ultima_direccion = -self.ultima_direccion
            self.giro_referencia = giro

        while self.cuadros_giro_cabeza and self.num_cuadro - self.cuadros_giro_cabeza[0] > ventana:
            self.cuadros_giro_cabeza.popleft()

        # Ya que se vio nerviosa, se queda prendido mientras siga quieta junto
        # a la puerta (se apaga si se mueve o se va)
        if (permanencia >= MIRAR_PERMANENCIA_MIN_S and movimiento <= MIRAR_MOVIMIENTO_MAX and
                len(self.cuadros_giro_cabeza) >= self.giros_mirar):
            self.mirar_activo = True

        self.metricas_mirar = {
            "cerca_puerta": True,
            "permanencia_s": round(permanencia, 1),
            "movimiento": round(movimiento, 2),
            "giros": len(self.cuadros_giro_cabeza),
        }
        return self.mirar_activo

    # --- Varias personas -------------------------------------------------
    # Lo que se guarda por persona. Los metodos de trepar y mirar trabajan con
    # self.*, entonces antes de analizar a alguien se "abre su expediente"
    # (se pasan sus datos a self) y al terminar se guarda de vuelta.
    CAMPOS_PERSONA = (
        "historial_cuerpo", "cuadro_inicio_empujon", "cuadros_sin_empujon",
        "cuadros_giro_cabeza", "ultima_direccion", "giro_referencia",
        "caderas_mirar", "quieta_desde", "mirar_activo", "metricas_mirar",
        "metricas_trepar", "cuadro_ultimo_contacto",
    )

    def _expediente_nuevo(self, centro, tamano):
        return {
            "centro": centro, "tamano": tamano, "visto": self.num_cuadro,
            "historial_cuerpo": [], "cuadro_inicio_empujon": None,
            "cuadros_sin_empujon": 0, "cuadros_giro_cabeza": deque(),
            "ultima_direccion": 0, "giro_referencia": None,
            "caderas_mirar": deque(), "quieta_desde": None,
            "mirar_activo": False, "metricas_mirar": {}, "metricas_trepar": {},
            "cuadro_ultimo_contacto": None,
        }

    def _abrir_expediente(self, exp):
        for campo in self.CAMPOS_PERSONA:
            setattr(self, campo, exp[campo])

    def _guardar_expediente(self, exp):
        for campo in self.CAMPOS_PERSONA:
            exp[campo] = getattr(self, campo)

    def _emparejar_personas(self, esqueletos):
        """Le asigna a cada esqueleto el expediente de la persona que ya venia.

        Se empareja por cercania: el esqueleto se queda con el expediente cuyo
        centro este mas cerca (y no tan lejos para su tamano). Si no hay
        ninguno cerca es alguien nuevo. Regresa una lista de (id, esqueleto).
        """
        datos = []
        for esq in esqueletos:
            xs = [float(p[0]) for p in esq if float(p[0]) != 0]
            ys = [float(p[1]) for p in esq if float(p[1]) != 0]
            centro = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
            tamano = max(max(xs) - min(xs), max(ys) - min(ys), 40.0)
            datos.append((esq, centro, tamano))

        # parejas posibles ordenadas de la mas cercana a la mas lejana
        parejas = []
        for i, (_, centro, tamano) in enumerate(datos):
            for id_p, exp in self.expedientes.items():
                distancia = ((centro[0] - exp["centro"][0]) ** 2 +
                             (centro[1] - exp["centro"][1]) ** 2) ** 0.5
                # con una sola persona antes y ahora siempre es la misma
                # (asi se portaba cuando solo se analizaba a una)
                if distancia <= max(tamano, exp["tamano"]) or \
                        (len(datos) == 1 and len(self.expedientes) == 1):
                    parejas.append((distancia, i, id_p))
        parejas.sort()

        asignado = {}
        usados = set()
        for _, i, id_p in parejas:
            if i in asignado or id_p in usados:
                continue
            asignado[i] = id_p
            usados.add(id_p)

        resultado = []
        for i, (esq, centro, tamano) in enumerate(datos):
            if i not in asignado:
                id_p = self.siguiente_id
                self.siguiente_id += 1
                self.expedientes[id_p] = self._expediente_nuevo(centro, tamano)
                asignado[i] = id_p
            exp = self.expedientes[asignado[i]]
            exp["centro"], exp["tamano"], exp["visto"] = centro, tamano, self.num_cuadro
            resultado.append((asignado[i], esq))
        return resultado

    def _contar_distintas(self, esqueletos):
        # a veces YOLO saca a la misma persona 2 veces casi encimada; para
        # contar cuantas hay, si hombros y caderas caen en el mismo lugar
        # (menos de 15 px) se cuenta una sola vez
        distintas = []
        for esq in esqueletos:
            repetido = any(
                max(abs(float(esq[j][c]) - float(otro[j][c]))
                    for j in (5, 6, 11, 12) for c in (0, 1)) < 15
                for otro in distintas)
            if not repetido:
                distintas.append(esq)
        return len(distintas)

    def _olvidar_ausentes(self, vistos):
        """A los que no salieron en este analisis se les reinicia lo de empujon
        y mirar (como antes cuando no habia nadie) y si ya llevan rato sin
        verse se borra su expediente."""
        for id_p in list(self.expedientes):
            if id_p in vistos:
                continue
            exp = self.expedientes[id_p]
            # el empujon aguanta unos analisis sin verla (se tapan entre ellos)
            if exp["cuadro_inicio_empujon"] is not None:
                exp["cuadros_sin_empujon"] += 1
                if exp["cuadros_sin_empujon"] > self.max_cuadros_perdidos:
                    exp["cuadro_inicio_empujon"] = None
                    exp["cuadros_sin_empujon"] = 0
            exp["ultima_direccion"] = 0
            exp["giro_referencia"] = None
            exp["caderas_mirar"] = deque()
            exp["quieta_desde"] = None
            exp["mirar_activo"] = False
            exp["metricas_mirar"] = {}
            exp["metricas_trepar"] = {}
            if self.num_cuadro - exp["visto"] > 3 * self.fps:
                del self.expedientes[id_p]

    def detectar_movimiento(self, cuadro):
        """Saca la mancha de movimiento comparando con el cuadro anterior (jala con sombras)."""
        x1, y1, x2, y2 = self.obtener_roi()
        recorte = cuadro[y1:y2, x1:x2]
        gris = cv2.cvtColor(recorte, cv2.COLOR_BGR2GRAY)
        gris = cv2.GaussianBlur(gris, (21, 21), 0)

        if self.gris_anterior is None:
            self.gris_anterior = gris
            return False, None

        # diferencia entre cuadros con umbral bajito para que agarre sombras
        diferencia = cv2.absdiff(self.gris_anterior, gris)
        mascara = cv2.threshold(diferencia, 15, 255, cv2.THRESH_BINARY)[1]
        mascara = cv2.dilate(mascara, None, iterations=3)

        # contornos de lo que se movio
        contornos, _ = cv2.findContours(mascara.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        self.gris_anterior = gris

        # se queda con el contorno mas grande (debe ser la persona)
        contorno_mayor = None
        area_mayor = 0
        for contorno in contornos:
            area = cv2.contourArea(contorno)
            if area > 300 and area > area_mayor:
                area_mayor = area
                contorno_mayor = contorno

        if contorno_mayor is not None:
            (mx, my, mw, mh) = cv2.boundingRect(contorno_mayor)
            # se pasa a coordenadas de la imagen completa
            self.caja_movimiento = (x1 + mx, y1 + my, mw, mh)
            return True, self.caja_movimiento

        return False, None

    def analizar_pose(self, cuadro):
        """Corre YOLO-pose y revisa cerradura, trepar, empujon y mirar alrededor."""
        # YOLO solo corre cada SALTO_CUADROS_POSE cuadros, si no se reusa el ultimo
        self.contador_cuadros_pose += 1
        if self.contador_cuadros_pose < SALTO_CUADROS_POSE:
            if self.ultimo_resultado_pose is not None:
                return self.ultimo_resultado_pose
        self.contador_cuadros_pose = 0

        manos_en_cerradura = False
        alerta_trepar = False
        trepar_candidato = False
        empujon_puerta = False
        mirando_alrededor = False
        puntos_cuerpo = []

        if not self.modelo_cargado:
            return (manos_en_cerradura, alerta_trepar, empujon_puerta,
                    mirando_alrededor, puntos_cuerpo)

        resultados = self.modelo(cuadro, conf=0.2, verbose=False, imgsz=TAMANO_IMAGEN_POSE)

        # Se juntan los esqueletos de todas las personas que vio YOLO
        esqueletos = []
        for r in resultados:
            puntos = r.keypoints
            if puntos is not None and len(puntos.xy) > 0:
                for candidato in puntos.xy:
                    if len(candidato) < 17:
                        continue
                    if any(float(p[0]) != 0 and float(p[1]) != 0 for p in candidato):
                        esqueletos.append(candidato)

        # cada persona se analiza por separado con su propio expediente
        # (la principal va al final para que sus datos queden en self)
        personas = self._emparejar_personas(esqueletos)
        def _area(par):
            esq = par[1]
            xs = [float(p[0]) for p in esq if float(p[0]) != 0]
            ys = [float(p[1]) for p in esq if float(p[1]) != 0]
            return (max(xs) - min(xs)) * (max(ys) - min(ys))
        personas.sort(key=_area)
        self.id_principal = personas[-1][0] if personas else self.id_principal
        self._olvidar_ausentes({id_p for id_p, _ in personas})
        self.ultimo_num_personas = self._contar_distintas(esqueletos)

        for id_persona, esqueleto in personas:
            self._abrir_expediente(self.expedientes[id_persona])
            contacto_puerta = False
            manos_esta = False   # esta persona tiene la mano en la cerradura
            if len(esqueleto) >= 17:
                # los 17 puntos de YOLO, aqui solo se usan estos
                nariz = esqueleto[0]
                hombro_izq = esqueleto[5]
                hombro_der = esqueleto[6]
                muneca_izq = esqueleto[9]
                muneca_der = esqueleto[10]
                cadera_izq = esqueleto[11]
                cadera_der = esqueleto[12]
                rodilla_izq = esqueleto[13]
                rodilla_der = esqueleto[14]
                tobillo_izq = esqueleto[15]
                tobillo_der = esqueleto[16]

                # se guardan para dibujar el esqueleto en el video
                pc = {
                    "nariz": (int(nariz[0]), int(nariz[1])),
                    "hombro_izq": (int(hombro_izq[0]), int(hombro_izq[1])),
                    "hombro_der": (int(hombro_der[0]), int(hombro_der[1])),
                    "muneca_izq": (int(muneca_izq[0]), int(muneca_izq[1])),
                    "muneca_der": (int(muneca_der[0]), int(muneca_der[1])),
                    "cadera_izq": (int(cadera_izq[0]), int(cadera_izq[1])),
                    "cadera_der": (int(cadera_der[0]), int(cadera_der[1])),
                    "rodilla_izq": (int(rodilla_izq[0]), int(rodilla_izq[1])),
                    "rodilla_der": (int(rodilla_der[0]), int(rodilla_der[1])),
                    "tobillo_izq": (int(tobillo_izq[0]), int(tobillo_izq[1])),
                    "tobillo_der": (int(tobillo_der[0]), int(tobillo_der[1])),
                }
                puntos_cuerpo.append(pc)

                # 1. Manos en la cerradura: alguna muneca dentro de una cerradura
                for muneca in [muneca_izq, muneca_der]:
                    x_m, y_m = int(muneca[0]), int(muneca[1])
                    if x_m != 0 and y_m != 0:
                        for cerradura in self.cerraduras:
                            if self._punto_en_zona(x_m, y_m, cerradura):
                                manos_en_cerradura = True
                                manos_esta = True

                # 2. Postura de trepar (solo si esta cerca de la puerta)
                y_nariz = int(nariz[1])
                x_nariz = int(nariz[0])
                y_hombro_izq = int(hombro_izq[1])
                y_hombro_der = int(hombro_der[1])
                y_cadera_izq = int(cadera_izq[1])
                y_cadera_der = int(cadera_der[1])
                y_muneca_izq = int(muneca_izq[1])
                y_muneca_der = int(muneca_der[1])
                y_rodilla_izq = int(rodilla_izq[1])
                y_rodilla_der = int(rodilla_der[1])
                y_cadera_prom = (y_cadera_izq + y_cadera_der) / 2

                cerca_puerta = self._cerca_de_puerta(x_nariz, y_nariz) or \
                    self._cerca_de_puerta(int((int(muneca_izq[0]) + int(muneca_der[0])) / 2), int(y_cadera_prom))

                # la postura sola no cuenta como trepar, tiene que subir el cuerpo
                if cerca_puerta and not getattr(_cfg, "TREPAR_POSTURA_EXIGE_SUBIDA", False):
                    # A: muneca bien arriba del hombro (agarrandose de algo)
                    if y_hombro_izq > 0 and y_muneca_izq > 0 and y_muneca_izq < y_hombro_izq - 25:
                        trepar_candidato = True
                    if y_hombro_der > 0 and y_muneca_der > 0 and y_muneca_der < y_hombro_der - 25:
                        trepar_candidato = True

                    # B: muneca arriba de la nariz (mano muy arriba)
                    if y_nariz > 0:
                        if y_muneca_izq > 0 and y_muneca_izq < y_nariz - 10:
                            trepar_candidato = True
                        if y_muneca_der > 0 and y_muneca_der < y_nariz - 10:
                            trepar_candidato = True

                    # C: rodilla arriba de la cadera (pierna levantada)
                    if y_cadera_prom > 0:
                        if y_rodilla_izq > 0 and y_rodilla_izq < y_cadera_prom - 30:
                            trepar_candidato = True
                        if y_rodilla_der > 0 and y_rodilla_der < y_cadera_prom - 30:
                            trepar_candidato = True

                # 3. Empujon a la puerta: hombros o cadera pegados a la
                #    puerta un buen rato (pasar enfrente no cuenta)
                for puerta in self.puertas:
                    for punto in [hombro_izq, hombro_der, cadera_izq, cadera_der]:
                        x_p, y_p = int(punto[0]), int(punto[1])
                        if x_p != 0 and y_p != 0:
                            if self._punto_en_zona(x_p, y_p, puerta):
                                contacto_puerta = True

                if contacto_puerta:
                    if self.cuadro_inicio_empujon is None:
                        self.cuadro_inicio_empujon = self.num_cuadro
                    self.cuadros_sin_empujon = 0
                elif self.cuadro_inicio_empujon is not None:
                    self.cuadros_sin_empujon += 1
                    if self.cuadros_sin_empujon > self.max_cuadros_perdidos:
                        self.cuadro_inicio_empujon = None
                        self.cuadros_sin_empujon = 0

                if self.cuadro_inicio_empujon is not None:
                    segundos_empujando = (self.num_cuadro - self.cuadro_inicio_empujon) / self.fps
                    if segundos_empujando >= self.umbral_empujon:
                        empujon_puerta = True
                # (el contador se queda en el expediente de esta persona)

                # 4. Mirando alrededor: quieta junto a la puerta volteando
                #    la cabeza (checando que nadie la vea)
                mira_esta = self._mirando_alrededor(
                    nariz, hombro_izq, hombro_der, y_cadera_prom,
                    (int(cadera_izq[0]) + int(cadera_der[0])) / 2)
                # Voltear la cabeza en la entrada tambien pasa cuando
                # platican. Solo es sospechoso si al mismo tiempo (o
                # hace poquito) le movio a la cerradura o a la puerta.
                if manos_esta or contacto_puerta:
                    self.cuadro_ultimo_contacto = self.num_cuadro
                ventana_contacto = getattr(_cfg, "MIRAR_CONTACTO_S", None)
                if mira_esta and ventana_contacto is not None:
                    mira_esta = (
                        self.cuadro_ultimo_contacto is not None and
                        self.num_cuadro - self.cuadro_ultimo_contacto
                        <= ventana_contacto * self.fps)
                mirando_alrededor = mirando_alrededor or mira_esta

                # 5. Trepar porque el cuerpo va subiendo. Se guarda la
                #    altura y se compara con lo de hace unos segundos;
                #    contra el cuadro anterior no sirve porque trepar
                #    es lento y cada paso sube poquitos pixeles.
                if y_nariz > 0 and y_cadera_prom > 0:
                    ys_visibles = [int(p[1]) for p in esqueleto if int(p[1]) > 0]
                    y_pies = max(ys_visibles) if ys_visibles else 0
                    # que tanto estan las manos arriba de la cadera
                    # (positivo = manos arriba, agarrandose para subir)
                    ys_munecas = [y for y in (y_muneca_izq, y_muneca_der) if y > 0]
                    manos_arriba = (y_cadera_prom - min(ys_munecas)) if ys_munecas else 0
                    self.historial_cuerpo.append(
                        (self.num_cuadro, y_nariz, y_cadera_prom,
                         x_nariz, y_pies, manos_arriba)
                    )
                    self._recortar_historial_cuerpo()
                    if self._cuerpo_trepando():
                        trepar_candidato = True

            self._guardar_expediente(self.expedientes[id_persona])

        # Al final self se queda con los datos de la persona principal
        if self.id_principal in self.expedientes:
            self._abrir_expediente(self.expedientes[self.id_principal])

        # si iba trepando y desaparece, se salio de la toma por arriba (cuenta como confirmacion)
        if not puntos_cuerpo and self.confirmaciones_trepar >= 1:
            self._recortar_historial_cuerpo()
            if self._cuerpo_trepando():
                trepar_candidato = True

        # Si no hay nadie se limpian los contadores para no arrastrar alertas
        if not puntos_cuerpo:
            self.metricas_trepar = {}
            self._reiniciar_mirar()
            self.ultima_direccion = 0
            self.cuadro_inicio_empujon = None
            self.cuadros_sin_empujon = 0
            ventana_giros = self.ventana_mirar * self.fps
            while (self.cuadros_giro_cabeza and
                   self.num_cuadro - self.cuadros_giro_cabeza[0] > ventana_giros):
                self.cuadros_giro_cabeza.popleft()

        # Trepar se confirma en varios analisis seguidos, asi un cuadro
        # suelto con la pose mal no dispara nada
        if trepar_candidato:
            self.confirmaciones_trepar += 1
        else:
            self.confirmaciones_trepar = 0
        if self.confirmaciones_trepar >= self.confirmaciones_necesarias:
            alerta_trepar = True

        resultado = (manos_en_cerradura, alerta_trepar, empujon_puerta,
                     mirando_alrededor, puntos_cuerpo)
        self.ultimo_resultado_pose = resultado
        return resultado

    def detectar(self, cuadro):
        """Analiza un cuadro y regresa True si hay que mandar la alarma."""
        self.num_cuadro += 1
        cuadros_para_alarma = int(self.tiempo_deteccion * self.fps)
        cuadros_sin_actividad = int(2.0 * self.fps)

        # Paso 1: buscar persona rapido con HOG
        hay_persona_hog, personas = self.buscar_persona_hog(cuadro)

        # Paso 2: movimiento entre cuadros (respaldo para cuando hay sombra)
        hay_movimiento, caja_movimiento = self.detectar_movimiento(cuadro)

        # Paso 3: postura con YOLO-pose (aunque HOG no la vea, YOLO a veces si)
        manos_cerradura = False
        alerta_trepar = False
        empujon_puerta = False
        mirando_alrededor = False
        puntos_cuerpo = []

        if MODO_DETECCION == "cascada":
            (manos_cerradura, alerta_trepar, empujon_puerta,
             mirando_alrededor, puntos_cuerpo) = self.analizar_pose(cuadro)

        # YOLO vio a alguien si saco puntos del cuerpo
        hay_persona_yolo = len(puntos_cuerpo) > 0

        # hay persona si cualquiera de los 3 la ve
        hay_persona = hay_persona_hog or hay_persona_yolo or hay_movimiento

        # Si solo hay movimiento (ni HOG ni YOLO), se usa la caja de movimiento
        # para seguir la altura y detectar si trepa en la sombra
        if hay_movimiento and not hay_persona_hog and not hay_persona_yolo:
            personas = np.array([caja_movimiento]) if caja_movimiento else []

            if caja_movimiento:
                _, my, _, mh = caja_movimiento
                centro_y = my + mh // 2
                arriba_y = my

                # checa si esta junto a la puerta
                centro_x = caja_movimiento[0] + caja_movimiento[2] // 2
                cerca_puerta = self._cerca_de_puerta(centro_x, centro_y)
                # Alguien lejos (al fondo) se puede encimar con la puerta en la
                # imagen y al alejarse parece que sube. El que trepa esta junto
                # a la puerta, entonces su silueta es grande comparada con ella.
                fraccion_min = getattr(_cfg, "TREPAR_MOVIMIENTO_FRAC_PUERTA", 0)
                if cerca_puerta and fraccion_min > 0 and self.puertas:
                    alto_puerta = max(p["y2"] - p["y1"] for p in self.puertas)
                    if mh < fraccion_min * alto_puerta:
                        cerca_puerta = False

                if cerca_puerta:
                    # Se va llenando el historial de movimiento. Si hubo un hueco
                    # largo sin movimiento se empieza de cero: la subida tiene
                    # que ser seguida, no pedazos sueltos.
                    if (self.historial_movimiento and
                            self.num_cuadro - self.historial_movimiento[-1][0] > 0.5 * self.fps):
                        self.historial_movimiento = []
                    self.historial_movimiento.append((self.num_cuadro, arriba_y, centro_y))
                    if len(self.historial_movimiento) > 10:
                        self.historial_movimiento.pop(0)

                    # es menos preciso que el esqueleto, por eso pide subida
                    # clara y seguida y descarta brincos raros
                    if self._movimiento_indica_trepar():
                        alerta_trepar = True
                else:
                    # se salio de la puerta, se borra el historial
                    self.historial_movimiento = []

        # Mantiene la alerta de trepar un ratito aunque se pierda la pose
        if alerta_trepar:
            self.sostener_trepar_hasta = self.num_cuadro + int(TREPAR_SOSTENER_S * self.fps)
        elif hay_persona and self.num_cuadro <= self.sostener_trepar_hasta:
            alerta_trepar = True

        # se guarda todo para que el servidor lo ensene
        self.ultimo_hay_persona = hay_persona
        self.ultimas_personas = personas if (hay_persona_hog or hay_movimiento) else []
        self.ultimos_puntos = puntos_cuerpo
        self.ultimo_manos_cerradura = manos_cerradura
        self.ultimo_alerta_trepar = alerta_trepar
        self.ultimo_empujon_puerta = empujon_puerta
        self.ultimo_mirando_alrededor = mirando_alrededor
        self.ultimo_hay_persona_hog = hay_persona_hog
        self.ultimo_hay_persona_yolo = hay_persona_yolo
        self.hay_movimiento = hay_movimiento
        self.ultima_caja_movimiento = caja_movimiento

        if not hay_persona:
            # Ya no se ve a nadie: checa si iba subiendo justo antes de
            # desaparecer (se salto la barda)
            if self.espera_trepar > 0:
                self.espera_trepar -= 1
            elif self._movimiento_indica_trepar():
                # iba subiendo segun el movimiento
                alerta_trepar = True
                self.ultimo_alerta_trepar = True
                self.detectando = True
                self.cuadro_inicio_actividad = self.num_cuadro
                self.cuadro_ultima_actividad = self.num_cuadro
                self.espera_trepar = self.cuadros_espera_trepar
                self.historial_movimiento = []
                print("  ALERTA TREPAR por respaldo de movimiento!")
                return True  # alarma de una vez
            else:
                # iba subiendo segun el esqueleto (misma regla que con la persona a la vista)
                self._recortar_historial_cuerpo()
                if self._cuerpo_trepando():
                    alerta_trepar = True
                    self.ultimo_alerta_trepar = True
                    self.detectando = True
                    self.cuadro_inicio_actividad = self.num_cuadro
                    self.cuadro_ultima_actividad = self.num_cuadro
                    self.espera_trepar = self.cuadros_espera_trepar
                    self.historial_cuerpo.clear()  # tambien borra el del expediente
                    return True  # alarma de una vez

            # si ya paso un rato sin nadie se reinicia todo
            if self.cuadro_inicio_actividad is not None:
                if self.num_cuadro - self.cuadro_ultima_actividad > cuadros_sin_actividad:
                    self.cuadro_inicio_actividad = None
                    self.detectando = False
                    self.cuadro_inicio_cerradura = None
                    self.cuadros_sin_cerradura = 0
            return False

        # Tiempo con las manos en la cerradura (aguanta unos cuadros perdidos).
        # Tocar la cerradura no es sospechoso (abrir con llave es rapido), lo
        # sospechoso es quedarse forcejeando mas de UMBRAL_CERRADURA_S.
        if manos_cerradura:
            if self.cuadro_inicio_cerradura is None:
                self.cuadro_inicio_cerradura = self.num_cuadro
                print(f"CERRADURA: empieza en cuadro={self.num_cuadro}")
            self.cuadros_sin_cerradura = 0
        elif self.cuadro_inicio_cerradura is not None:
            self.cuadros_sin_cerradura += 1
            if self.cuadros_sin_cerradura > self.max_cuadros_perdidos:
                print(f"CERRADURA: se reinicia tras {self.cuadros_sin_cerradura} cuadros sin verla")
                self.cuadro_inicio_cerradura = None
                self.cuadros_sin_cerradura = 0
        forzando_cerradura = (manos_cerradura and
                              self.tiempo_cerradura() >= self.umbral_cerradura)

        # solo cuenta si hace algo sospechoso (estar en la ROI no basta)
        hay_sospecha = (forzando_cerradura or alerta_trepar or
                        empujon_puerta or mirando_alrededor)

        # Trepar = alarma de una vez (no espera TIEMPO_DETECCION_S)
        if alerta_trepar:
            self.detectando = True
            self.cuadro_inicio_actividad = self.num_cuadro
            self.cuadro_ultima_actividad = self.num_cuadro

            if self.espera_trepar > 0:
                self.espera_trepar -= 1
            else:
                self.espera_trepar = self.cuadros_espera_trepar
                self.historial_movimiento = []
                print("  ALERTA TREPAR DISPARADA!")

            return True

        # Forzar la cerradura = alarma en cuanto pasa el umbral
        if forzando_cerradura:
            print(f"ALERTA CERRADURA: {self.tiempo_cerradura():.1f}s >= {self.umbral_cerradura}s")
            self.detectando = True
            self.cuadro_inicio_actividad = self.num_cuadro
            self.cuadro_ultima_actividad = self.num_cuadro
            return True

        # lo demas (empujon, mirar) tiene que durar TIEMPO_DETECCION_S
        if hay_sospecha:
            if self.cuadro_inicio_actividad is None:
                self.cuadro_inicio_actividad = self.num_cuadro
            self.cuadro_ultima_actividad = self.num_cuadro
            self.detectando = True
        elif self.cuadro_inicio_actividad is not None:
            if self.num_cuadro - self.cuadro_ultima_actividad > cuadros_sin_actividad:
                self.cuadro_inicio_actividad = None
                self.detectando = False
                self.cuadro_inicio_cerradura = None
                self.cuadros_sin_cerradura = 0

        if self.detectando:
            cuadros_transcurridos = self.num_cuadro - self.cuadro_inicio_actividad
            if cuadros_transcurridos >= cuadros_para_alarma:
                return True

        return False

    def tiempo_cerradura(self):
        # segundos que lleva con las manos en la cerradura
        if self.cuadro_inicio_cerradura:
            segundos = (self.num_cuadro - self.cuadro_inicio_cerradura - self.cuadros_sin_cerradura) / self.fps
            return max(0, segundos)
        return 0

    def tiempo_transcurrido(self):
        # segundos desde que empezo la actividad sospechosa
        if self.cuadro_inicio_actividad:
            return (self.num_cuadro - self.cuadro_inicio_actividad) / self.fps
        return 0

    def dibujar_personas(self, cuadro, personas):
        # recuadro azul en cada persona que encontro HOG
        x1, y1, _, _ = self.obtener_roi()
        for (px, py, pw, ph) in personas:
            cv2.rectangle(cuadro, (x1 + px, y1 + py), (x1 + px + pw, y1 + py + ph), (255, 0, 0), 2)
            cv2.putText(cuadro, "Persona", (x1 + px, y1 + py - 5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 2)

    # que puntos se unen con linea para dibujar el esqueleto (app de pruebas)
    UNIONES_ESQUELETO = [
        ("hombro_izq", "hombro_der"), ("cadera_izq", "cadera_der"),
        ("hombro_izq", "cadera_izq"), ("hombro_der", "cadera_der"),
        ("hombro_izq", "muneca_izq"), ("hombro_der", "muneca_der"),
        ("cadera_izq", "rodilla_izq"), ("cadera_der", "rodilla_der"),
        ("rodilla_izq", "tobillo_izq"), ("rodilla_der", "tobillo_der"),
    ]

    def dibujar_esqueleto(self, cuadro, puntos_cuerpo, con_lineas=False):
        # pinta los puntos del cuerpo y si se pide tambien las lineas
        for pc in puntos_cuerpo:
            if con_lineas:
                for a, b in self.UNIONES_ESQUELETO:
                    pa, pb = pc.get(a), pc.get(b)
                    if pa and pb and 0 not in pa and 0 not in pb:
                        cv2.line(cuadro, pa, pb, (0, 255, 255), 2)
            for _nombre, (px, py) in pc.items():
                if px != 0 and py != 0:
                    cv2.circle(cuadro, (px, py), 3, (0, 255, 0), -1)
