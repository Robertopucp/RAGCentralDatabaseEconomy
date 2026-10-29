"""
Registro de los chats de la web para un fine-tuning posterior, y aviso por correo
cuando la web llega a AVISO_USUARIOS usuarios distintos.

Cada conversación se guarda en su propio archivo JSON en CHATS_WEB_DIR, con el
mismo formato de data/Finetuning/dataset_EconData.jsonl (categoria, fuente y
conversations con role y content), para sumarla después al dataset. El archivo se
reescribe en cada turno, así que una conversación a medias también queda guardada.

Nombre de cada archivo: <usuario>__<número de conversación>.json. El usuario es un
código anónimo derivado de su correo (id_anonimo): el correo no se guarda en ningún
archivo. "Nueva conversación" abre otro archivo para el mismo usuario.
"""
import hashlib
import json
import re
import smtplib
import threading
from datetime import datetime
from email.message import EmailMessage
from typing import Dict, List

from src.config import Config

# Marca que el aviso ya se envió, para no repetirlo al reiniciar la web
ARCHIVO_AVISO = "_aviso_enviado.json"

PATRON_CORREO = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def correo_valido(correo: str) -> bool:
    """Revisa el formato del correo (no comprueba que exista)"""
    return bool(PATRON_CORREO.match(correo.strip()))


def id_anonimo(correo: str) -> str:
    """
    Código del usuario: hash del correo en minúsculas. El mismo correo da siempre el
    mismo código, así su tope de consultas se mantiene entre sesiones, y el correo
    no se puede leer en los archivos
    """
    return hashlib.sha256(correo.strip().lower().encode("utf-8")).hexdigest()[:16]


class RegistroChats:
    """Guarda las conversaciones de la web y avisa al llegar a AVISO_USUARIOS usuarios"""

    def __init__(self):
        self.carpeta = Config.CHATS_WEB_DIR
        self.carpeta.mkdir(parents=True, exist_ok=True)

        # Varios usuarios pueden escribir a la vez
        self._lock = threading.Lock()

        # Conversación abierta de cada usuario: número, fecha de inicio y mensajes
        self.abiertas: Dict[str, Dict] = {}

        # Usuarios que ya chatearon, también los de ejecuciones anteriores de la web
        self.usuarios = {p.stem.split("__")[0] for p in self.carpeta.glob("*__*.json")}
        self.aviso_enviado = (self.carpeta / ARCHIVO_AVISO).exists()

    def consultas_por_usuario(self) -> Dict[str, int]:
        """Consultas guardadas de cada usuario, sumando todas sus conversaciones"""
        conteo: Dict[str, int] = {}
        for ruta in self.carpeta.glob("*__*.json"):
            try:
                mensajes = json.loads(ruta.read_text(encoding="utf-8"))["conversations"]
            except Exception:
                continue
            usuario = ruta.stem.split("__")[0]
            conteo[usuario] = conteo.get(usuario, 0) + sum(m["role"] == "user" for m in mensajes)
        return conteo

    def agregar_turno(self, user_id: str, pregunta: str, respuesta: str, sources: List[Dict]):
        """
        Agrega una pregunta y su respuesta a la conversación abierta del usuario y la
        guarda en disco. Un error al escribir no interrumpe el chat.
        """
        with self._lock:
            conv = self.abiertas.get(user_id)
            if conv is None:
                numero = len(list(self.carpeta.glob(f"{user_id}__*.json"))) + 1
                conv = self.abiertas[user_id] = {
                    "numero": numero,
                    "inicio": datetime.now().isoformat(timespec="seconds"),
                    "conversations": [],
                    "bases": [],
                }

            conv["conversations"] += [{"role": "user", "content": pregunta},
                                      {"role": "assistant", "content": respuesta}]
            # Bases recuperadas en cada turno, para revisar la conversación antes de entrenar
            conv["bases"].append([s["title"] for s in sources])

            registro = {
                "categoria": None,          # se asigna al revisar la conversación
                "fuente": "web",
                "sesion": user_id,
                "inicio": conv["inicio"],
                "actualizado": datetime.now().isoformat(timespec="seconds"),
                "bases": conv["bases"],
                "conversations": conv["conversations"],
            }
            try:
                ruta = self.carpeta / f"{user_id}__{conv['numero']}.json"
                ruta.write_text(json.dumps(registro, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as e:
                print(f"No se pudo guardar la conversación: {e}")

            nuevo_usuario = user_id not in self.usuarios
            self.usuarios.add(user_id)
            avisar = (nuevo_usuario and not self.aviso_enviado
                      and len(self.usuarios) >= Config.AVISO_USUARIOS)
            if avisar:
                self.aviso_enviado = True

        if avisar:
            # En otro hilo, para que el usuario no espere al servidor de correo
            threading.Thread(target=self._enviar_aviso, args=(len(self.usuarios),),
                             daemon=True).start()

    def nueva_conversacion(self, user_id: str):
        """Cierra la conversación abierta: el siguiente turno va a un archivo nuevo"""
        with self._lock:
            self.abiertas.pop(user_id, None)

    def _enviar_aviso(self, total_usuarios: int):
        """Envía el correo de aviso y deja la marca de que ya se envió"""
        if not (Config.AVISO_EMAIL_DESTINO and Config.SMTP_USUARIO and Config.SMTP_PASSWORD):
            print(f"Se llegó a {total_usuarios} usuarios, pero falta configurar el correo "
                  "(AVISO_EMAIL_DESTINO, SMTP_USUARIO y SMTP_PASSWORD en src/.env)")
            return

        correo = EmailMessage()
        correo["Subject"] = f"Kalish Manta Analytics: {total_usuarios} usuarios en la web"
        correo["From"] = Config.SMTP_USUARIO
        correo["To"] = Config.AVISO_EMAIL_DESTINO
        correo.set_content(
            f"La web de Kalish Manta Analytics llegó a {total_usuarios} usuarios distintos "
            f"({datetime.now():%d/%m/%Y %H:%M}).\n\n"
            f"Las conversaciones están en {self.carpeta}."
        )
        try:
            with smtplib.SMTP_SSL(Config.SMTP_HOST, Config.SMTP_PORT, timeout=30) as smtp:
                smtp.login(Config.SMTP_USUARIO, Config.SMTP_PASSWORD)
                smtp.send_message(correo)
            (self.carpeta / ARCHIVO_AVISO).write_text(json.dumps({
                "fecha": datetime.now().isoformat(timespec="seconds"),
                "usuarios": total_usuarios,
            }), encoding="utf-8")
            print(f"Aviso enviado a {Config.AVISO_EMAIL_DESTINO}: {total_usuarios} usuarios")
        except Exception as e:
            # Sin la marca en disco, el aviso se vuelve a intentar al reiniciar la web
            print(f"No se pudo enviar el aviso por correo: {e}")
