"""
Prueba rápida de que Ollama responde y de que los embeddings vienen normalizados.

La norma debe ser 1.0: rag_service.search() convierte la distancia de FAISS en
similitud coseno asumiendo vectores de norma 1.

Uso, desde la carpeta src:
    python testinglib.py
"""
import numpy as np

import ollama
from config import Config

v=ollama.embed(model= Config.EMBEDDING_MODEL, input=['hola']).embeddings[0]

print(np.linalg.norm(v))
