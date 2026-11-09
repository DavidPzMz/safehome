import os
import ssl
import time
import smtplib
import threading
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

import cv2

# los datos del correo se leen de config cada vez que se usan
import config as _cfg


def _contrasena_correo():
    # la del panel (config.json) y si esta vacia la de la variable de entorno
    return (getattr(_cfg, "CORREO_CONTRASENA", "") or
            os.environ.get("VIGILANCIA_CORREO_CONTRASENA", ""))


class SistemaAlertas:
    """Guarda la foto de la alerta y manda el correo."""

    def __init__(self):
        self.hora_ultima_alerta = 0
        # aparte de la espera del servidor, aqui no se manda mas de 1 correo por minuto
        self.espera_entre_correos = 60
        self.tipos_alerta = []

    def guardar_evidencia(self, cuadro):
        # guarda la imagen del momento de la alerta en la carpeta de evidencias
        carpeta = _cfg.CARPETA_EVIDENCIAS
        os.makedirs(carpeta, exist_ok=True)
        marca = datetime.now().strftime("%Y%m%d_%H%M%S")
        nombre = f"{carpeta}/alerta_{marca}.jpg"
        cv2.imwrite(nombre, cuadro)
        print(f"Imagen guardada: {nombre}")

    def enviar_correo(self, cuadro):
        # arma el correo con la foto adjunta y lo manda en otro hilo
        usuario = getattr(_cfg, "CORREO_USUARIO", "")
        contrasena = _contrasena_correo()
        destinatario = getattr(_cfg, "CORREO_DESTINATARIO", "")
        servidor_smtp = getattr(_cfg, "CORREO_SERVIDOR_SMTP", "") or "smtp.gmail.com"  # vacio = Gmail
        puerto = int(getattr(_cfg, "CORREO_PUERTO_SMTP", 465) or 465)
        if not usuario or not contrasena or not destinatario:
            print("⚠️ Correo no configurado. Revisar la configuracion")
            return

        try:
            marca = datetime.now().strftime("%Y%m%d_%H%M%S")
            os.makedirs(_cfg.CARPETA_EVIDENCIAS, exist_ok=True)
            ruta_imagen = f"{_cfg.CARPETA_EVIDENCIAS}/correo_{marca}.jpg"
            cv2.imwrite(ruta_imagen, cuadro)

            mensaje = MIMEMultipart()
            mensaje["From"] = usuario
            mensaje["To"] = destinatario

            # lista de lo que se detecto, una linea por tipo
            detalle = "\n".join([f"- {t}" for t in self.tipos_alerta]) if self.tipos_alerta else "Actividad sospechosa detectada"
            ahora = datetime.now()
            asunto = f"ALERTA Vigilancia - {ahora.strftime('%H:%M:%S')}"
            cuerpo = (f"{_cfg.MENSAJE_ALERTA}\n\nFecha: {ahora.strftime('%Y-%m-%d')}\n"
                      f"Hora: {ahora.strftime('%H:%M:%S')}\n\nDetecciones:\n{detalle}")

            mensaje["Subject"] = asunto
            mensaje.attach(MIMEText(cuerpo, "plain"))

            with open(ruta_imagen, "rb") as archivo:
                imagen = MIMEImage(archivo.read())
                imagen.add_header("Content-Disposition", "attachment", filename=f"alerta_{marca}.jpg")
                mensaje.attach(imagen)

            def mandar_en_segundo_plano():
                # se manda aparte para que la deteccion no se trabe esperando a Gmail
                try:
                    # conexion segura que revisa el certificado del servidor
                    contexto = ssl.create_default_context()
                    if puerto == 465:
                        servidor = smtplib.SMTP_SSL(servidor_smtp, puerto, context=contexto, timeout=20)
                    else:
                        servidor = smtplib.SMTP(servidor_smtp, puerto, timeout=20)
                        servidor.starttls(context=contexto)
                    servidor.login(usuario, contrasena)
                    servidor.send_message(mensaje)
                    servidor.quit()
                    print("Alerta enviada por correo")
                except Exception as e:
                    print(f"Error enviando correo: {e}")

            threading.Thread(target=mandar_en_segundo_plano, daemon=True).start()
        except Exception as e:
            print(f"Error al preparar el correo: {e}")

    def disparar_alerta(self, cuadro, tipos_alerta=None):
        # si ya hubo una alerta hace poco no hace nada, si no guarda foto y manda correo
        ahora = time.time()
        if ahora - self.hora_ultima_alerta < self.espera_entre_correos:
            return

        self.hora_ultima_alerta = ahora
        print("🚨 ACTIVIDAD SOSPECHOSA DETECTADA!")

        if tipos_alerta:
            self.tipos_alerta = tipos_alerta

        if _cfg.GUARDAR_IMAGENES:
            self.guardar_evidencia(cuadro)
        self.enviar_correo(cuadro)
