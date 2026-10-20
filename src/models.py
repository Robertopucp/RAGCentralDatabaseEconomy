"""
Modelos Pydantic de requests y responses de la API REST (main.py).

FastAPI los usa para validar los datos de entrada y para generar la
documentación interactiva en /docs.
"""
from pydantic import BaseModel, ConfigDict, Field
from typing import List, Dict, Any


# ===== REQUEST MODELS =====

class ChatRequest(BaseModel):
    """Request para enviar un mensaje al chatbot"""
    user_id: str = Field(..., description="ID del usuario (cada usuario tiene su propia memoria)")
    message: str = Field(..., min_length=1, max_length=2000,
                         description="Mensaje del usuario (máx. 2000 caracteres)")

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "user_id": "user123",
            "message": "Para mi tesis sobre pobreza distrital en el Perú, ¿qué base de datos me recomiendas?"
        }
    })


class SearchRequest(BaseModel):
    """Request para búsqueda RAG"""
    query: str = Field(..., min_length=1, description="Consulta de búsqueda")
    top_k: int = Field(default=5, ge=1, description="Número de chunks a devolver")

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "query": "mapa de pobreza distrital",
            "top_k": 5
        }
    })


# ===== RESPONSE MODELS =====

class Source(BaseModel):
    """Base de datos usada como contexto en la respuesta"""
    source: str = Field(..., description="PDF de descripción de la base de datos")
    dataset: str = Field(..., description="Código de la base de datos")
    title: str = Field("", description="Nombre oficial de la base de datos, según su ficha")
    dropbox_link: str = Field(..., description="Link de Dropbox de acceso a la base de datos")
    score: float = Field(..., description="Similitud con la pregunta (0 a 1)")


class ChatResponse(BaseModel):
    """Response del chatbot"""
    answer: str = Field(..., description="Respuesta para el usuario, con los links de acceso al final")
    sources: List[Source] = Field(default_factory=list, description="Bases de datos usadas")

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "answer": "Para pobreza distrital te recomiendo el Mapa de Pobreza del INEI...\n\n"
                      "Acceso a las bases de datos:\n"
                      "🔗 Pobreza: https://www.dropbox.com/scl/fo/...&dl=0",
            "sources": [{
                "source": "Pobrezadescription.pdf",
                "dataset": "Pobreza",
                "dropbox_link": "https://www.dropbox.com/scl/fo/...&dl=0",
                "score": 0.78
            }]
        }
    })


class SearchResult(BaseModel):
    """Resultado de búsqueda RAG"""
    text: str
    score: float
    metadata: Dict[str, Any]


class SearchResponse(BaseModel):
    """Response de búsqueda RAG"""
    query: str
    results: List[SearchResult]
    total: int


class HealthResponse(BaseModel):
    """Response de health check"""
    status: str
    service: str
    rag_ready: bool
    timestamp: str
