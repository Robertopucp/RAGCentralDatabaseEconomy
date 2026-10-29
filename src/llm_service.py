"""
Servicio del chatbot de Kalish Manta Analytics: orquesta la memoria por usuario y el RAG.

Cada mensaje recupera los chunks de las fichas de bases de datos más parecidos
(rag_service.py) y genera la respuesta con el Qwen3.5-4B fine-tuned en modo no
thinking. Lo usan la web (app.py, Streamlit) y la API REST (main.py).

Uso por consola, desde la raíz del repositorio:
    python -m src.llm_service
"""
import json
import threading
import time
from datetime import datetime
from typing import Dict, List

from src.config import Config
from src.rag_service import RAGService
from src.registro_chats import RegistroChats


class LimiteChatsError(Exception):
    """El usuario ya usó sus CHAT_MAX_POR_USUARIO consultas"""


class MemoryManager:
    """
    Gestiona la memoria de conversación de un usuario.

    Guarda solo los últimos mensajes para que el historial que recibe el LLM
    no crezca sin límite y el prompt siga entrando en la memoria de la GPU.
    """

    def __init__(self, max_messages: int = 8):
        self.max_messages = max_messages
        self.messages: List[Dict] = []

    def add_user_message(self, content: str):
        """Agrega un mensaje del usuario"""
        self._add("user", content)

    def add_assistant_message(self, content: str):
        """Agrega un mensaje del asistente"""
        self._add("assistant", content)

    def _add(self, role: str, content: str):
        self.messages.append({
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat()
        })
        # Mantiene solo los últimos max_messages mensajes
        self.messages = self.messages[-self.max_messages:]

    def get_messages(self) -> List[Dict]:
        """Obtiene los mensajes recientes"""
        return self.messages.copy()

    def clear(self):
        """Limpia la memoria"""
        self.messages = []


class ChatbotService:
    """
    Chatbot de acceso a bases de datos para investigación: RAG + memoria por usuario
    """

    def __init__(self, rag_service: RAGService, preload_llm: bool = True):
        """
        Inicializa el servicio de chatbot

        Args:
            rag_service: Servicio RAG con el índice FAISS ya cargado
            preload_llm: Cargar el LLM al iniciar (si no, se carga con el primer mensaje)
        """
        print("Inicializando servicio de chatbot...")

        if not rag_service.is_ready():
            raise RuntimeError("El índice FAISS no está cargado. Indexa los PDFs primero.")

        self.rag = rag_service

        # Memorias por usuario
        self.user_memories: Dict[str, MemoryManager] = {}

        # Varios usuarios pueden escribir a la vez en el registro de interacciones
        self._lock_registro = threading.Lock()

        # Conversaciones completas para fine-tuning y aviso por correo
        self.registro_chats = RegistroChats()

        # Consultas hechas por cada usuario, para el tope CHAT_MAX_POR_USUARIO. Se
        # leen de los chats guardados para que reiniciar la web no reinicie el tope
        self.chats_por_usuario: Dict[str, int] = self.registro_chats.consultas_por_usuario()
        self._lock_chats = threading.Lock()

        if preload_llm:
            self.rag.load_llm()

        print(f"  Modelo: {Config.LLM_MODEL_PATH}")
        print("Servicio de chatbot inicializado\n")

    def get_or_create_memory(self, user_id: str) -> MemoryManager:
        """Obtiene o crea un memory manager para un usuario"""
        if user_id not in self.user_memories:
            self.user_memories[user_id] = MemoryManager(Config.MEMORY_K_MESSAGES)
        return self.user_memories[user_id]

    def chats_restantes(self, user_id: str) -> int:
        """Consultas que le quedan al usuario"""
        return max(Config.CHAT_MAX_POR_USUARIO - self.chats_por_usuario.get(user_id, 0), 0)

    def handle_chat(self, user_id: str, message: str, top_k_sources: int = None,
                    threshold: float = None) -> Dict:
        """
        Responde un mensaje del usuario con el contexto de su conversación

        Args:
            user_id: ID del usuario (o de la sesión web)
            message: Mensaje del usuario
            top_k_sources: Máximo de bases de datos en la respuesta (None = valor de Config)
            threshold: Similitud mínima de los chunks (None = valor de Config)

        Returns:
            Dict con answer (respuesta con links de Dropbox), llm_answer (solo la
            respuesta del LLM) y sources (bases de datos usadas, con su link)

        Raises:
            LimiteChatsError: si el usuario ya hizo CHAT_MAX_POR_USUARIO consultas
        """
        message = message.strip()

        # La consulta se cuenta antes de generar, para que dos envíos simultáneos
        # no pasen ambos el tope; si la generación falla, se devuelve
        with self._lock_chats:
            if self.chats_restantes(user_id) == 0:
                raise LimiteChatsError(
                    f"Llegaste al máximo de {Config.CHAT_MAX_POR_USUARIO} consultas por usuario.")
            self.chats_por_usuario[user_id] = self.chats_por_usuario.get(user_id, 0) + 1

        memory = self.get_or_create_memory(user_id)
        inicio = time.time()

        try:
            result = self.rag.answer_question(message, history=memory.get_messages(),
                                              threshold=threshold, top_k_sources=top_k_sources)
        except Exception:
            with self._lock_chats:
                self.chats_por_usuario[user_id] -= 1
            raise

        # En la memoria va la respuesta sin los links: el LLM no necesita releerlos
        # y así no intenta copiarlos (y alterarlos) en respuestas siguientes
        memory.add_user_message(message)
        memory.add_assistant_message(result["llm_answer"])

        self._registrar_interaccion(user_id, message, result, top_k_sources, threshold,
                                    time.time() - inicio)
        self.registro_chats.agregar_turno(user_id, message, result["llm_answer"], result["sources"])

        return {
            "answer": result["answer"],
            "llm_answer": result["llm_answer"],
            "sources": result["sources"]
        }

    def _registrar_interaccion(self, user_id: str, message: str, result: Dict,
                               top_k_sources: int, threshold: float, segundos: float):
        """
        Agrega la interacción al registro en formato JSON Lines (un objeto JSON por
        línea), útil para revisar consultas reales y armar datos de evaluación.
        Un error al escribir no interrumpe el chat.
        """
        if Config.INTERACCIONES_PATH is None:
            return

        registro = {
            "fecha": datetime.now().isoformat(timespec="seconds"),
            "sesion": user_id,
            "pregunta": message,
            "respuesta": result["llm_answer"],
            "bases": [{"titulo": s["title"], "score": s["score"]} for s in result["sources"]],
            "configuracion": {
                "numero_bases": top_k_sources or Config.RAG_TOP_K_ARCHIVOS,
                "similitud_minima": threshold if threshold is not None else Config.RAG_SCORE_THRESHOLD,
            },
            "segundos": round(segundos, 1),
        }
        try:
            Config.INTERACCIONES_PATH.parent.mkdir(parents=True, exist_ok=True)
            with self._lock_registro, open(Config.INTERACCIONES_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(registro, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"No se pudo guardar la interacción: {e}")

    def clear_memory(self, user_id: str):
        """Limpia la memoria de un usuario (no reinicia su contador de consultas)"""
        if user_id in self.user_memories:
            self.user_memories[user_id].clear()
        self.registro_chats.nueva_conversacion(user_id)


def build_chatbot() -> ChatbotService:
    """Crea el RAG (carga el índice FAISS, o lo crea desde los PDFs) y el chatbot"""

    Config.ensure_directories()

    rag = RAGService()

    if not rag.load_index():
        print("Creando índice desde PDFs...")
        if rag.load_documents_from_pdfs() == 0:
            raise RuntimeError(f"No hay PDFs para indexar en {Config.PDFS_RAW_DIR}")

    return ChatbotService(rag)


def main():
    """Chat por consola"""
    print("=" * 70)
    print("KALISH MANTA ANALYTICS - CHAT POR CONSOLA")
    print("=" * 70 + "\n")

    chatbot = build_chatbot()

    print("Escribe 'salir' para terminar\n")
    user_id = "consola"

    while True:
        try:
            message = input("Tú: ").strip()

            if not message:
                continue

            if message.lower() in ['salir', 'exit', 'quit']:
                break

            response = chatbot.handle_chat(user_id, message)
            print(f"\nAsistente: {response['answer']}\n")

        except KeyboardInterrupt:
            print("\n\nInterrumpido")
            break
        except Exception as e:
            print(f"Error: {e}\n")

    print("\nChat finalizado")


if __name__ == "__main__":
    main()
