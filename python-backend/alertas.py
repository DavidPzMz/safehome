import os
import re
import ssl
import json
import time
import base64
import smtplib
import threading
import urllib.request
import urllib.error
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.header import Header

import cv2

# los datos del correo se leen de config cada vez que se usan
import config as _cfg
from gestor_config import CARPETA_CONFIG, MODO_PRUEBAS

# historial de alertas: se guarda en la carpeta del usuario para que no se pierda
CARPETA_HISTORIAL = os.path.join(CARPETA_CONFIG, "alertas")
ARCHIVO_HISTORIAL = os.path.join(CARPETA_HISTORIAL, "historial.json")
MAX_ALERTAS = 200  # las mas viejas se borran
_candado_historial = threading.Lock()


def leer_historial():
    # regresa la lista de alertas guardadas (la mas nueva al final)
    try:
        with open(ARCHIVO_HISTORIAL, encoding="utf-8") as archivo:
            datos = json.load(archivo)
        return datos if isinstance(datos, list) else []
    except (OSError, ValueError):
        return []


def _escribir_historial(lista):
    os.makedirs(CARPETA_HISTORIAL, exist_ok=True)
    temporal = ARCHIVO_HISTORIAL + ".tmp"
    with open(temporal, "w", encoding="utf-8") as archivo:
        json.dump(lista, archivo, ensure_ascii=False, indent=1)
    os.replace(temporal, ARCHIVO_HISTORIAL)


def _agregar_alerta(alerta):
    # agrega una alerta y borra las que pasen del limite (con su foto)
    with _candado_historial:
        lista = leer_historial()
        lista.append(alerta)
        sobrantes = lista[:-MAX_ALERTAS]
        for vieja in sobrantes:
            try:
                os.remove(ruta_imagen_alerta(vieja.get("id", "")))
            except (OSError, ValueError):
                pass
        _escribir_historial(lista[-MAX_ALERTAS:])


def _actualizar_correo(id_alerta, estado, detalle=""):
    # cambia el estado del correo de una alerta ya guardada
    with _candado_historial:
        lista = leer_historial()
        for alerta in lista:
            if alerta.get("id") == id_alerta:
                alerta["correo"] = estado
                alerta["detalle_correo"] = detalle
        _escribir_historial(lista)


def marcar_correos_pendientes():
    # si la app se cerro mientras mandaba un correo, ese correo ya no salio
    with _candado_historial:
        lista = leer_historial()
        if any(a.get("correo") == "enviando" for a in lista):
            for alerta in lista:
                if alerta.get("correo") == "enviando":
                    alerta["correo"] = "error"
                    alerta["detalle_correo"] = "La app se cerro antes de enviarlo"
            _escribir_historial(lista)


def borrar_historial():
    # borra todas las alertas guardadas y sus fotos
    with _candado_historial:
        for alerta in leer_historial():
            try:
                os.remove(ruta_imagen_alerta(alerta.get("id", "")))
            except (OSError, ValueError):
                pass
        _escribir_historial([])


def ruta_imagen_alerta(id_alerta):
    # ruta de la foto de una alerta (el id solo puede tener numeros y _)
    if not re.fullmatch(r"[0-9_]+", id_alerta or ""):
        raise ValueError("id de alerta no valido")
    return os.path.join(CARPETA_HISTORIAL, f"alerta_{id_alerta}.jpg")


def _explicar_error(e):
    # pasa el error del correo a algo que se entienda
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return "Usuario o contraseña del correo incorrectos"
    if isinstance(e, (OSError, smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected)):
        return "Sin conexión a internet o al servidor de correo"
    return str(e)


def _contrasena_correo():
    # la de remitente.json y si esta vacia la de la variable de entorno
    return (getattr(_cfg, "CORREO_CONTRASENA", "") or
            os.environ.get("VIGILANCIA_CORREO_CONTRASENA", ""))


def remitente_listo():
    # True si esta configurada la cuenta que manda los correos
    return bool(getattr(_cfg, "CORREO_USUARIO", "") and _contrasena_correo())


# --- Servicio de correo en la nube (Cloudflare Worker + Brevo) ---
# Si hay URL, los codigos y las alertas los manda el servicio y la app no
# necesita ninguna contrasena. Si esta vacia se usa el SMTP de remitente.json.
URL_SERVICIO_CORREO = os.environ.get(
    "SAFEHOME_SERVICIO_CORREO", "https://safehome-correo.guaspossupersta.workers.dev").rstrip("/")


def servicio_activo():
    return bool(URL_SERVICIO_CORREO)


def envio_listo():
    # True si hay alguna forma de mandar correos
    return servicio_activo() or remitente_listo()


def _llamar_servicio(ruta, datos):
    # POST con JSON al servicio; si falla lanza RuntimeError con el motivo
    peticion = urllib.request.Request(
        URL_SERVICIO_CORREO + ruta, data=json.dumps(datos).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "SafeHome/1.0"})
    try:
        with urllib.request.urlopen(peticion, timeout=20, context=ssl.create_default_context()) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            motivo = json.loads(e.read().decode("utf-8")).get("error")
        except Exception:
            motivo = None
        raise RuntimeError(motivo or f"El servicio de correo respondió {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError("Sin conexión a internet o al servicio de correo") from e


def servicio_pedir_codigo(correo):
    _llamar_servicio("/codigo", {"correo": correo})


def servicio_confirmar(correo, codigo):
    # regresa el token que deja mandar alertas a ese correo
    return _llamar_servicio("/confirmar", {"correo": correo, "codigo": codigo})["token"]


def _mandar(mensaje):
    # se conecta al servidor de correo (con conexion segura) y manda el mensaje
    usuario = getattr(_cfg, "CORREO_USUARIO", "")
    servidor_smtp = getattr(_cfg, "CORREO_SERVIDOR_SMTP", "") or "smtp.gmail.com"  # vacio = Gmail
    puerto = int(getattr(_cfg, "CORREO_PUERTO_SMTP", 465) or 465)
    contexto = ssl.create_default_context()
    if puerto == 465:
        servidor = smtplib.SMTP_SSL(servidor_smtp, puerto, context=contexto, timeout=20)
    else:
        servidor = smtplib.SMTP(servidor_smtp, puerto, timeout=20)
        servidor.starttls(context=contexto)
    try:
        servidor.login(usuario, _contrasena_correo())
        mensaje["From"] = usuario
        servidor.send_message(mensaje)
    finally:
        try:
            servidor.quit()
        except Exception:
            pass


def enviar_codigo(destinatario, codigo):
    # manda el codigo de verificacion; si falla lanza RuntimeError con el motivo
    if not remitente_listo():
        raise RuntimeError("La app no tiene configurada la cuenta que envía los correos")
    mensaje = MIMEText(
        f"Tu código de verificación de SafeHome es: {codigo}\n\n"
        "Vence en 10 minutos. Si no lo pediste, ignora este correo.", "plain", "utf-8")
    mensaje["To"] = destinatario
    mensaje["Subject"] = Header(f"SafeHome - código de verificación {codigo}", "utf-8")
    try:
        _mandar(mensaje)
    except Exception as e:
        raise RuntimeError(_explicar_error(e)) from e


class SistemaAlertas:
    """Guarda la alerta en el historial (con su foto) y manda el correo."""

    def __init__(self):
        self.hora_ultima_alerta = 0
        # aparte de la espera del servidor, aqui no se manda mas de 1 alerta por minuto
        self.espera_entre_correos = 60
        self.tipos_alerta = []

    def guardar_evidencia(self, cuadro):
        # copia extra de la imagen en la carpeta de evidencias
        carpeta = _cfg.CARPETA_EVIDENCIAS
        os.makedirs(carpeta, exist_ok=True)
        marca = datetime.now().strftime("%Y%m%d_%H%M%S")
        nombre = f"{carpeta}/alerta_{marca}.jpg"
        cv2.imwrite(nombre, cuadro)
        print(f"Imagen guardada: {nombre}")

    def enviar_correo(self, id_alerta, ruta_imagen):
        # arma el correo con la foto adjunta y lo manda en otro hilo
        destinatario = getattr(_cfg, "CORREO_DESTINATARIO", "")
        token = getattr(_cfg, "CORREO_TOKEN", "")
        verificado = destinatario and destinatario == getattr(_cfg, "CORREO_VERIFICADO", "")
        if servicio_activo() and token and verificado:
            self._enviar_por_servicio(id_alerta, ruta_imagen, destinatario, token)
            return
        if not remitente_listo() or not destinatario:
            print("⚠️ Correo no configurado. Revisar la configuracion")
            _actualizar_correo(id_alerta, "sin_configurar", "Falta configurar el correo")
            return
        # en la app final solo se manda al correo que se comprobo con el codigo
        if not MODO_PRUEBAS and destinatario != getattr(_cfg, "CORREO_VERIFICADO", ""):
            _actualizar_correo(id_alerta, "sin_configurar", "El correo no está verificado")
            return

        try:
            mensaje = MIMEMultipart()
            mensaje["To"] = destinatario

            ahora = datetime.now()
            mensaje["Subject"] = f"ALERTA Vigilancia - {ahora.strftime('%H:%M:%S')}"
            # el correo no dice que se detecto: eso solo se ve en la app
            cuerpo = (f"{_cfg.MENSAJE_ALERTA}\n\nFecha: {ahora.strftime('%Y-%m-%d')}\n"
                      f"Hora: {ahora.strftime('%H:%M:%S')}\n\nRevisa la foto adjunta.")
            mensaje.attach(MIMEText(cuerpo, "plain"))

            with open(ruta_imagen, "rb") as archivo:
                imagen = MIMEImage(archivo.read())
                imagen.add_header("Content-Disposition", "attachment", filename=f"alerta_{id_alerta}.jpg")
                mensaje.attach(imagen)
        except Exception as e:
            print(f"Error al preparar el correo: {e}")
            _actualizar_correo(id_alerta, "error", str(e))
            return

        def mandar_en_segundo_plano():
            # se manda aparte para que la deteccion no se trabe esperando al correo
            try:
                _mandar(mensaje)
                print("Alerta enviada por correo")
                _actualizar_correo(id_alerta, "enviado", f"Enviado a {destinatario}")
            except Exception as e:
                print(f"Error enviando correo: {e}")
                _actualizar_correo(id_alerta, "error", _explicar_error(e))

        threading.Thread(target=mandar_en_segundo_plano, daemon=True).start()

    def _enviar_por_servicio(self, id_alerta, ruta_imagen, destinatario, token):
        # la alerta se manda por el servicio en la nube (sin contrasena en la app)
        ahora = datetime.now()
        try:
            with open(ruta_imagen, "rb") as archivo:
                imagen = base64.b64encode(archivo.read()).decode("ascii")
        except OSError:
            imagen = None
        datos = {
            "correo": destinatario, "token": token,
            "fecha": ahora.strftime("%Y-%m-%d"), "hora": ahora.strftime("%H:%M:%S"),
            "mensaje": _cfg.MENSAJE_ALERTA, "imagen": imagen,
        }

        def mandar_en_segundo_plano():
            try:
                _llamar_servicio("/alerta", datos)
                print("Alerta enviada por correo (servicio)")
                _actualizar_correo(id_alerta, "enviado", f"Enviado a {destinatario}")
            except RuntimeError as e:
                print(f"Error enviando correo: {e}")
                _actualizar_correo(id_alerta, "error", str(e))

        threading.Thread(target=mandar_en_segundo_plano, daemon=True).start()

    def disparar_alerta(self, cuadro, tipos_alerta=None):
        # si ya hubo una alerta hace poco no hace nada; si no, la guarda y manda el correo
        ahora = time.time()
        if ahora - self.hora_ultima_alerta < self.espera_entre_correos:
            return

        self.hora_ultima_alerta = ahora
        print("🚨 ACTIVIDAD SOSPECHOSA DETECTADA!")

        if tipos_alerta:
            self.tipos_alerta = tipos_alerta

        # foto y registro en el historial
        momento = datetime.now()
        id_alerta = momento.strftime("%Y%m%d_%H%M%S")
        ruta_imagen = ruta_imagen_alerta(id_alerta)
        os.makedirs(CARPETA_HISTORIAL, exist_ok=True)
        cv2.imwrite(ruta_imagen, cuadro)
        _agregar_alerta({
            "id": id_alerta,
            "fecha": momento.strftime("%Y-%m-%d"),
            "hora": momento.strftime("%H:%M:%S"),
            "tipos": list(self.tipos_alerta) or ["Actividad sospechosa"],
            "correo": "enviando",
            "detalle_correo": "",
        })

        if _cfg.GUARDAR_IMAGENES:
            self.guardar_evidencia(cuadro)
        self.enviar_correo(id_alerta, ruta_imagen)
