"""
API REST con FastAPI del chatbot de Kalish Manta Analytics.

Es opcional: la web (app.py, Streamlit) no la necesita. Sirve para integrar el
chatbot en otras aplicaciones. No se debe ejecutar a la vez que la web, porque
cada una carga su propia copia del LLM y en 8GB de VRAM no caben dos.

Uso, desde la raíz del repositorio y con Ollama corriendo:
    python -m src.main
    Documentación interactiva en http://127.0.0.1:8000/docs

Endpoints:
    POST   /chat             Enviar un mensaje al chatbot
    DELETE /chat/{user_id}   Borrar la memoria de un usuario
    POST   /search           Búsqueda semántica directa en el índice
    POST   /rag/reindex      Reindexar los PDFs
    GET    /rag/stats        Estadísticas del índice
"""
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from src.config import Config
from src.models import (
    ChatRequest, SearchRequest,
    ChatResponse, SearchResponse, SearchResult,
    HealthResponse
)
from src.llm_service import LimiteChatsError, build_chatbot

# ===== INICIALIZACIÓN =====
# El chatbot (índice FAISS y LLM) se carga una sola vez al iniciar el servidor

print("\n" + "=" * 60)
print("INICIALIZANDO SERVICIOS")
print("=" * 60 + "\n")

chatbot_service = build_chatbot()
rag_service = chatbot_service.rag

app = FastAPI(
    title="Kalish Manta Analytics - API",
    description="""
    Acceso a bases de datos para investigación.

    Asistente con RAG (Retrieval Augmented Generation) sobre las fichas de
    descripción de las bases de datos, con un Qwen3.5-4B fine-tuned (QLoRA).
    Cada respuesta incluye el link de Dropbox de acceso a las bases de datos.
    """,
    version="1.0.0"
)

# CORS abierto para que cualquier frontend pueda consumir la API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===== ENDPOINTS GENERALES =====

@app.get("/", tags=["General"])
def api_info():
    """Información de la API"""
    return {
        "title": "Kalish Manta Analytics - API",
        "version": "1.0.0",
        "endpoints": {
            "chat": "POST /chat",
            "search": "POST /search",
            "health": "GET /health",
            "docs": "GET /docs"
        },
        "rag_ready": rag_service.is_ready()
    }


@app.get("/health", response_model=HealthResponse, tags=["General"])
def health_check():
    """Verifica el estado del servicio"""
    return HealthResponse(
        status="healthy",
        service="kalish-manta-analytics-api",
        rag_ready=rag_service.is_ready(),
        timestamp=datetime.now().isoformat()
    )


# ===== ENDPOINTS DE CHAT =====

@app.post("/chat", response_model=ChatResponse, tags=["Chat"])
def chat(data: ChatRequest):
    """
    Envía un mensaje al chatbot

    Responde con la información de las bases de datos más relacionadas con el
    mensaje y sus links de acceso.
    """
    try:
        response = chatbot_service.handle_chat(data.user_id, data.message)
        return ChatResponse(answer=response["answer"], sources=response["sources"])

    except LimiteChatsError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al procesar mensaje: {e}")


@app.delete("/chat/{user_id}", tags=["Chat"])
def clear_chat_history(user_id: str):
    """Limpia la memoria de conversación de un usuario"""
    chatbot_service.clear_memory(user_id)
    return {"message": f"Historial de {user_id} eliminado"}


# ===== ENDPOINTS DE BÚSQUEDA RAG =====

@app.post("/search", response_model=SearchResponse, tags=["RAG"])
def search_documents(data: SearchRequest):
    """Búsqueda semántica en las fichas de las bases de datos (FAISS)"""
    results = rag_service.search(data.query, k=data.top_k)

    return SearchResponse(
        query=data.query,
        results=[SearchResult(**r) for r in results],
        total=len(results)
    )


@app.post("/rag/reindex", tags=["RAG"])
def reindex_documents():
    """Reindexa todos los PDFs"""
    count = rag_service.load_documents_from_pdfs()

    return {
        "message": "Reindexación completada",
        "chunks_indexed": count,
        "pdfs_dir": str(Config.PDFS_RAW_DIR)
    }


@app.get("/rag/stats", tags=["RAG"])
def get_rag_stats():
    """Obtiene estadísticas del índice RAG"""
    return rag_service.get_stats()


# ===== MAIN =====

if __name__ == "__main__":
    import uvicorn

    print("\n" + "=" * 60)
    print("INICIANDO SERVIDOR")
    print("=" * 60)
    Config.print_config()

    uvicorn.run(
        app,
        host=Config.SERVER_HOST,
        port=Config.SERVER_PORT
    )
