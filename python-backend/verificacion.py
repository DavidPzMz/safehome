import re
import time
import secrets
import threading

# Verificacion del correo de alertas con un codigo de 4 digitos.
# Pasos:
#   "actual" = confirmar el correo que ya estaba (antes de cambiarlo)
#   "nuevo"  = confirmar el correo nuevo
# Los codigos solo viven en memoria: si se cierra la app hay que pedir otro.

VIGENCIA_CODIGO_S = 600        # el codigo sirve 10 minutos
MAX_INTENTOS = 5               # despues de 5 errores hay que pedir otro codigo
ESPERA_REENVIO_S = 30          # para no mandar codigos seguidos (spam)
VIGENCIA_ACTUAL_OK_S = 900     # tras confirmar el correo actual hay 15 min para poner el nuevo

_FORMATO_CORREO = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def correo_valido(correo):
    return bool(_FORMATO_CORREO.fullmatch(correo or "")) and len(correo) <= 254


class Verificacion:
    def __init__(self):
        self._candado = threading.Lock()
        self._codigos = {}          # paso -> {correo, codigo, vence, intentos, enviado}
        self._actual_ok_hasta = 0   # hasta cuando vale haber confirmado el correo actual

    def nuevo_codigo(self, paso, correo):
        # genera un codigo aleatorio de 4 digitos para ese paso y correo
        with self._candado:
            anterior = self._codigos.get(paso)
            ahora = time.time()
            if anterior and anterior["correo"] == correo and ahora - anterior["enviado"] < ESPERA_REENVIO_S:
                falta = int(ESPERA_REENVIO_S - (ahora - anterior["enviado"])) + 1
                raise ValueError(f"Espera {falta} s para pedir otro código")
            codigo = f"{secrets.randbelow(10000):04d}"
            self._codigos[paso] = {"correo": correo, "codigo": codigo, "vence": ahora + VIGENCIA_CODIGO_S,
                                   "intentos": 0, "enviado": ahora}
            return codigo

    def descartar(self, paso):
        # si el correo no se pudo mandar, el codigo no sirve
        with self._candado:
            self._codigos.pop(paso, None)

    def confirmar(self, paso, codigo):
        # revisa el codigo; regresa el correo confirmado o lanza ValueError
        with self._candado:
            datos = self._codigos.get(paso)
            if not datos:
                raise ValueError("Primero pide un código")
            if time.time() > datos["vence"]:
                del self._codigos[paso]
                raise ValueError("El código ya venció, pide otro")
            datos["intentos"] += 1
            if not secrets.compare_digest(str(codigo).strip(), datos["codigo"]):
                if datos["intentos"] >= MAX_INTENTOS:
                    del self._codigos[paso]
                    raise ValueError("Demasiados intentos, pide otro código")
                raise ValueError(f"Código incorrecto (quedan {MAX_INTENTOS - datos['intentos']} intentos)")
            del self._codigos[paso]
            if paso == "actual":
                self._actual_ok_hasta = time.time() + VIGENCIA_ACTUAL_OK_S
            return datos["correo"]

    def pendiente(self, paso):
        # correo al que se le pidio codigo en ese paso (cuando el codigo lo
        # revisa el servicio en la nube y no esta clase)
        with self._candado:
            datos = self._codigos.get(paso)
            if not datos or time.time() > datos["vence"]:
                raise ValueError("Primero pide un código")
            return datos["correo"]

    def aceptar(self, paso):
        # el servicio dijo que el codigo era correcto
        with self._candado:
            self._codigos.pop(paso, None)
            if paso == "actual":
                self._actual_ok_hasta = time.time() + VIGENCIA_ACTUAL_OK_S

    def actual_confirmado(self):
        with self._candado:
            return time.time() < self._actual_ok_hasta

    def reiniciar(self):
        # se llama al cancelar o al terminar el cambio de correo
        with self._candado:
            self._codigos.clear()
            self._actual_ok_hasta = 0
