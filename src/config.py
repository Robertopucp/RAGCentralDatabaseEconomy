"""
Configuración central del proyecto Kalish Manta Analytics.

Todos los parámetros se leen desde src/.env, con un valor por defecto en caso de
que la variable no exista. Así se puede ajustar el modelo, el RAG o el servidor
sin tocar el código.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Carga las variables de src/.env en el entorno del proceso
load_dotenv()


class Config:
    """Configuración centralizada del proyecto"""

    # ===== RUTAS =====
    # Las rutas se arman desde la raíz del repositorio para que el proyecto
    # funcione igual desde un notebook, la web o la API
    BASE_DIR = Path(__file__).parent.parent
    PDFS_RAW_DIR = BASE_DIR / "data" / "raw"
    DATA_DIR = BASE_DIR / "data" / "vectorstore"

    # ===== LLM: Qwen3.5-4B fine-tuned (QLoRA mergeado, modo no thinking) =====
    # Carpeta generada por notebooks/2.Finetuning_LLM_QLora.ipynb (RUTA_LORA).
    # Ruta relativa a la raíz del proyecto, o absoluta.

    LLM_MODEL = os.getenv("LLM_MODEL", "output/LLMfine-tuning/LLM_Qlora")
    LLM_MODEL_PATH = BASE_DIR / LLM_MODEL

    # En 16 bits el modelo pesa unos 9GB y no entra en 8GB de VRAM, por eso se carga en 4 bits
    LLM_LOAD_IN_4BIT = os.getenv("LLM_LOAD_IN_4BIT", "true").lower() == "true"

    # Parámetros de generación, los mismos usados al evaluar el fine-tuning.
    # Temperatura baja para respuestas consistentes y fieles al contexto.
    LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.2"))
    LLM_TOP_P = float(os.getenv("LLM_TOP_P", "0.9"))
    LLM_REPETITION_PENALTY = float(os.getenv("LLM_REPETITION_PENALTY", "1.2"))

    # Tope de tokens por respuesta; el modelo se detiene antes si termina su respuesta
    LLM_MAX_NEW_TOKENS = int(os.getenv("LLM_MAX_NEW_TOKENS", "512"))

    # ===== EMBEDDINGS =====
    # Modelo de embeddings servido localmente por Ollama

    EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "ollama")
    EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "qwen3-embedding:4b")
    EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "2560"))

    # Capas del modelo de embeddings en GPU (0 = solo CPU; vacío = Ollama decide).
    # En CPU el LLM conserva toda la GPU y una consulta tarda menos de medio segundo.
    EMBEDDING_NUM_GPU = int(os.getenv("EMBEDDING_NUM_GPU")) if os.getenv("EMBEDDING_NUM_GPU") else None

    # Instrucción que se antepone a cada consulta, como recomienda Qwen3-Embedding
    EMBEDDING_QUERY_INSTRUCTION = os.getenv(
        "EMBEDDING_QUERY_INSTRUCTION",
        "Dada la consulta de un investigador, recupera las fichas de bases de datos "
        "que contienen los datos que necesita"
    )

    # ===== RAG =====
    # Tamaño y solape de los chunks, medidos en caracteres (no en tokens)
    CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "1000"))
    CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "50"))

    # Número de chunks que devuelve search() por defecto
    RAG_TOP_K_CHUNKS = int(os.getenv("RAG_TOP_K_CHUNKS", "10"))

    # Máximo de bases de datos (PDFs distintos) que entran al contexto del chat
    RAG_TOP_K_ARCHIVOS = int(os.getenv("RAG_TOP_K_ARCHIVOS","5"))

    # Máximo de chunks recuperados que se usan por cada PDF,
    # para que un solo PDF no acapare el contexto
    RAG_TOP_K_CHUNKS_POR_ARCHIVO = int(os.getenv("RAG_TOP_K_CHUNKS_POR_ARCHIVO", "2"))

    # Chunks vecinos que se agregan a cada lado de un chunk recuperado, para dar
    # más contexto de la base de datos al LLM (0 deja solo el chunk recuperado)
    RAG_CHUNK_WINDOW = int(os.getenv("RAG_CHUNK_WINDOW", "1"))

    # Incluir siempre el primer chunk de cada ficha recuperada (título, resumen y
    # ficha rápida), además de sus chunks más parecidos a la consulta
    RAG_INCLUIR_FICHA = os.getenv("RAG_INCLUIR_FICHA", "true").lower() == "true"

    # Similitud mínima para que un chunk se considere relacionado con la pregunta
    RAG_SCORE_THRESHOLD = float(os.getenv("RAG_SCORE_THRESHOLD", "0.5"))

    # Solo se usan chunks con score mayor o igual a (mejor score menos este margen),
    # para dejar fuera bases poco relacionadas cuando hay una claramente mejor
    RAG_SCORE_MARGIN = float(os.getenv("RAG_SCORE_MARGIN", "0.1"))

    # Directorio del índice FAISS
    FAISS_INDEX_DIR = DATA_DIR / "faiss_index"

    # ===== SERVIDOR (API REST de src/main.py) =====
    SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
    SERVER_PORT = int(os.getenv("SERVER_PORT", "8000"))

    # ===== REGISTRO DE INTERACCIONES =====
    # Archivo JSON Lines con cada consulta y respuesta (vacío en .env para desactivarlo).
    # Contiene mensajes de usuarios: no se sube a GitHub (.gitignore).
    _ruta_interacciones = os.getenv("INTERACCIONES_PATH", "data/interacciones/interacciones.jsonl")
    INTERACCIONES_PATH = BASE_DIR / _ruta_interacciones if _ruta_interacciones else None

    # ===== CHATS DE LA WEB (para fine-tuning) Y LÍMITES DE USO =====
    # Carpeta con una conversación completa por archivo JSON (no se sube a GitHub)
    CHATS_WEB_DIR = BASE_DIR / os.getenv("CHATS_WEB_DIR", "data/chats_web")

    # Máximo de consultas por usuario (sesión de la web), para evitar abusos
    CHAT_MAX_POR_USUARIO = int(os.getenv("CHAT_MAX_POR_USUARIO", "10"))

    # Usuarios distintos a partir de los cuales se envía un aviso por correo (una vez)
    AVISO_USUARIOS = int(os.getenv("AVISO_USUARIOS", "50"))

    # Correo del aviso. Con Gmail, SMTP_PASSWORD es una contraseña de aplicación
    # (myaccount.google.com/apppasswords), no la contraseña normal de la cuenta
    AVISO_EMAIL_DESTINO = os.getenv("AVISO_EMAIL_DESTINO", "")
    SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
    SMTP_USUARIO = os.getenv("SMTP_USUARIO", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")

    # ===== MEMORIA =====
    # Mensajes de la conversación que se envían al LLM como historial
    MEMORY_K_MESSAGES = int(os.getenv("MEMORY_K_MESSAGES", "10"))

    @classmethod
    def validate(cls) -> bool:
        """Valida que la configuración sea correcta"""
        errors = []

        if not cls.PDFS_RAW_DIR.exists():
            errors.append(f"Directorio de PDFs no existe: {cls.PDFS_RAW_DIR}")

        if errors:
            print("Errores de configuración:")
            for error in errors:
                print(f"  - {error}")
            return False

        return True

    @classmethod
    def print_config(cls):
        """Imprime la configuración actual"""
        print("\n" + "=" * 60)
        print("CONFIGURACIÓN CHAT-BOT PROJECT")
        print("=" * 60)
        print(f"LLM Model: {cls.LLM_MODEL_PATH}")
        print(f"LLM 4-bit: {cls.LLM_LOAD_IN_4BIT}")

        print("\n" + "=" * 60)
        print(f"\nEmbedding Provider: {cls.EMBEDDING_PROVIDER}")

        print(f"Embedding Model: {cls.EMBEDDING_MODEL}")

        print(f"\nRAG Top K: {cls.RAG_TOP_K_CHUNKS}")
        print(f"Chunk Size: {cls.CHUNK_SIZE}")
        print(f"Chunk Overlap: {cls.CHUNK_OVERLAP}")
        print(f"\nRAG Top K ARCHIVOS: {cls.RAG_TOP_K_ARCHIVOS}")
        print(f"\nPDFs Directory: {cls.PDFS_RAW_DIR}")
        print(f"Data Directory: {cls.DATA_DIR}")
        print("=" * 60 + "\n")

    @classmethod
    def ensure_directories(cls):
        """Asegura que los directorios necesarios existan"""
        cls.DATA_DIR.mkdir(parents=True, exist_ok=True)
        cls.FAISS_INDEX_DIR.mkdir(parents=True, exist_ok=True)
        if not cls.PDFS_RAW_DIR.exists():
            cls.PDFS_RAW_DIR.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    Config.print_config()
    print("Validando configuración...")
    if Config.validate():
        print("Configuración válida")
    else:
        print("Hay errores en la configuración")
