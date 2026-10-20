
"""
Servicio RAG (Retrieval Augmented Generation) con LangChain y FAISS.

Flujo de una consulta:
    1. Retrieval: busca en FAISS los chunks de las fichas de bases de datos más
       parecidos a la pregunta (embeddings qwen3-embedding:4b servidos por Ollama).
    2. Augmentation: agrupa los chunks por base de datos, agrega sus chunks
       vecinos y quita el texto repetido para armar el contexto del prompt.
    3. Generation: el Qwen3.5-4B fine-tuned con QLoRA responde en modo no
       thinking usando solo ese contexto.
    4. Acceso: los links de Dropbox salen de la metadata de cada PDF y se
       agregan al final de la respuesta, sin pasar por el LLM.
"""


import re
import threading
from typing import List, Dict, Optional
from pathlib import Path

from langchain_community.vectorstores import FAISS
from langchain_classic.text_splitter import RecursiveCharacterTextSplitter
from langchain_classic.schema import Document
from langchain_core.embeddings import Embeddings

import ollama
from tqdm import tqdm

from src.config import Config
from src.pdf_reader import PDFReader


# ===== PROMPT =====
# El mismo texto se usa en producción y en los datos de fine-tuning
# (src/construir_dataset_rag.py): si el modelo entrena con el formato exacto que
# recibe en la web, aprende a apoyarse en el contexto y no en lo que memorizó.

SYSTEM_PROMPT = """Eres el asistente de Kalish Manta Analytics. Guías a investigadores, desde estudiantes hasta académicos, en la búsqueda eficiente de bases de datos de economía y ciencias sociales del Perú a partir de sus propuestas o ideas de investigación.

Bases de datos disponibles en el catálogo:
{catalogo}

Reglas:
1. Usa únicamente la información de las fichas del CONTEXTO. No inventes nombres de bases, variables, periodos ni cifras.
2. Si el usuario no explicó su investigación, pídele que resuma su abstract, su hipótesis o el objetivo de su trabajo o análisis económico.
3. Si pregunta qué bases hay, nómbralas según el catálogo e invítalo a revisar la pestaña Catálogo de bases de datos.
4. Si el mensaje no trata sobre datos para investigación, indica con cortesía que no puedes responderlo, sin mencionar ninguna base.
5. No escribas links: los links de acceso se agregan automáticamente al final de la respuesta cuando hay fichas en el CONTEXTO.

{contexto}"""

CONTEXTO_CON_FICHAS = """CONTEXTO (fichas de las bases de datos relacionadas con el mensaje):
{fichas}

Menciona el nombre de cada base que uses e indica que en su link encontrará los datos y la ficha en PDF con más detalle."""

CONTEXTO_SIN_FICHAS = "CONTEXTO: ninguna ficha del catálogo está relacionada con este mensaje. No describas el contenido de ninguna base."


def formatear_contexto(databases: List[Dict]) -> str:
    """Texto de las fichas recuperadas, una sección por base de datos"""
    return "\n\n".join(f"[Base de datos: {db['title']}]\n{db['text']}" for db in databases)


def construir_system_prompt(contexto: str, catalogo: str) -> str:
    """System prompt con el catálogo completo y las fichas recuperadas (o el aviso de que no hay)"""
    bloque = CONTEXTO_CON_FICHAS.format(fichas=contexto) if contexto else CONTEXTO_SIN_FICHAS
    return SYSTEM_PROMPT.format(catalogo=catalogo, contexto=bloque)


class QWEN3Embeddings(Embeddings):
    """
    Embeddings de qwen3-embedding:4b a través de Ollama.

    Implementa la interfaz Embeddings de LangChain para usarse con FAISS, tanto
    al indexar los PDFs como al buscar con la pregunta del usuario.
    """

    def __init__(self, model:str=None, batch_size:int=10):

        self.model = model or Config.EMBEDDING_MODEL
        self.batch_size = batch_size

        # num_gpu=0 corre el modelo de embeddings en CPU: en una GPU de 8GB no caben
        # a la vez el LLM y el modelo de embeddings, y compartirla vuelve lenta la
        # generación. En CPU una consulta tarda menos de medio segundo.
        self.options = {}
        if Config.EMBEDDING_NUM_GPU is not None:
            self.options["num_gpu"] = Config.EMBEDDING_NUM_GPU

        print(f"Embeddings Model: {self.model}"
              f", dimension={Config.EMBEDDING_DIM}, batch={batch_size}")

    def _embed(self, texts: List[str]) -> List[List[float]]:

        """Genera los embeddings por lotes para no saturar a Ollama"""

        vectors: List[List[float]]=[]

        for i in tqdm(range(0, len(texts), self.batch_size), disable=len(texts) <= self.batch_size):

            batch = texts[i:i + self.batch_size]
            response = ollama.embed(model=self.model, input=batch, options=self.options)
            vectors.extend(response.embeddings)

        return vectors

    def embed_documents(self, texts:List[str]) -> List[List[float]]:

        """Embeddings de los chunks al indexar (sin instrucción)"""

        return self._embed(texts)

    def embed_query(self, text:str) -> List[float]:

        """
        Embedding de una consulta. Qwen3-Embedding está entrenado para recibir las
        consultas con una instrucción de la tarea y los documentos sin ella; esto
        separa mejor las consultas relevantes de los saludos o temas ajenos.
        """

        return self._embed([f"Instruct: {Config.EMBEDDING_QUERY_INSTRUCTION}\nQuery: {text}"])[0]



class QwenFineTunedLLM:
    """
    LLM Qwen3.5-4B fine-tuned con QLoRA (modelo mergeado en Config.LLM_MODEL_PATH)

    Se carga con transformers puro (sin Unsloth ni PEFT) y genera siempre en
    modo no thinking, igual que en el entrenamiento.
    """

    def __init__(self, model_path: Path = None):

        # Imports locales: indexar PDFs no necesita cargar torch ni transformers
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

        model_path = Path(model_path or Config.LLM_MODEL_PATH)

        if not model_path.exists():
            raise FileNotFoundError(
                f"No existe el modelo fine-tuned en {model_path}. "
                "Ejecuta notebooks/2.Finetuning_LLM_QLora.ipynb para generarlo."
            )

        print(f"  Cargando LLM fine-tuned: {model_path}")

        # Cuantización NF4 en 4 bits para que el modelo entre en una GPU de 8GB
        quantization_config = None
        if Config.LLM_LOAD_IN_4BIT:
            quantization_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.bfloat16,
            )

        self.tokenizer = AutoTokenizer.from_pretrained(str(model_path))
        self.model = AutoModelForCausalLM.from_pretrained(
            str(model_path),
            quantization_config=quantization_config,
            dtype=torch.bfloat16,
            device_map="cuda",
        )
        self.model.eval()

        # La web atiende a varios usuarios a la vez y la GPU procesa una
        # generación por vez: las demás consultas esperan su turno
        self._lock = threading.Lock()

    def generate(self, messages: List[Dict]) -> str:

        """Genera la respuesta del asistente para una lista de mensajes de chat"""

        import torch

        # La plantilla guardada ya fuerza el modo no thinking. enable_thinking=False
        # se pasa igual por si se carga un modelo con la plantilla original de Qwen3.5
        prompt = self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True,
            enable_thinking=False,
        )
        inputs = self.tokenizer(prompt, return_tensors="pt").to(self.model.device)

        with self._lock, torch.no_grad():
            output = self.model.generate(
                **inputs,
                max_new_tokens=Config.LLM_MAX_NEW_TOKENS,
                do_sample=True,
                temperature=Config.LLM_TEMPERATURE,
                top_p=Config.LLM_TOP_P,
                repetition_penalty=Config.LLM_REPETITION_PENALTY,
                pad_token_id=self.tokenizer.eos_token_id,
            )

        # Se decodifican solo los tokens nuevos, sin el prompt
        answer = self.tokenizer.decode(
            output[0][inputs.input_ids.shape[1]:], skip_special_tokens=True
        )

        # Por seguridad se descarta cualquier bloque <think> que pudiera aparecer
        answer = re.sub(r"<think>.*?</think>", "", answer, flags=re.DOTALL)
        return answer.strip()



class RAGService:
    """
    Servicio de Retrieval Augmented Generation.

    Usa FAISS como vector store, qwen3-embedding:4b para los embeddings y el
    Qwen3.5-4B fine-tuned para generar las respuestas.
    """

    def __init__(self, index_dir: str = None):

        """
        Inicializa el servicio RAG

        Args:
            index_dir: Directorio del índice FAISS (None = usa Config.FAISS_INDEX_DIR)
        """
        print("Inicializando servicio RAG...")

        # Asegurar que existen los directorios
        Config.ensure_directories()

        # Directorio del índice (permite índices separados por tema)
        self.index_dir = Path(index_dir) if index_dir else Config.FAISS_INDEX_DIR


        print(f"  Cargando modelo de embeddings: {Config.EMBEDDING_MODEL}")

        self.embeddings = QWEN3Embeddings()

        # Inicializar lector de PDFs
        self.pdf_reader = PDFReader()

        # Vector store (se inicializa al cargar o crear el índice)
        self.vector_store: Optional[FAISS] = None

        # Mapa (source, chunk_id) a texto, para ampliar el contexto con chunks vecinos
        self.chunk_lookup: Dict[tuple, str] = {}

        # LLM fine-tuned (se carga una sola vez, con load_llm)
        self.llm: Optional[QwenFineTunedLLM] = None

        # Divide cada PDF en chunks, priorizando cortes por párrafo, línea y oración
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=Config.CHUNK_SIZE,
            chunk_overlap=Config.CHUNK_OVERLAP,
            length_function=len,
            separators=["\n\n", "\n", ". ", " ", ""]
        )

        print("Servicio RAG inicializado\n")

    def load_documents_from_pdfs(self, pdfs_dir: str = None,
                                 file_filter: callable = None) -> int:
        """
        Carga y procesa los PDFs para crear el índice vectorial

        Args:
            pdfs_dir: Directorio con PDFs (usa Config.PDFS_RAW_DIR por defecto)
            file_filter: Función que recibe el nombre del archivo y devuelve
                         True si debe indexarse (permite indexar solo un
                         subconjunto de bases de datos)

        Returns:
            Número de chunks indexados
        """
        pdfs_dir = pdfs_dir or str(Config.PDFS_RAW_DIR)

        print(f"Cargando documentos desde: {pdfs_dir}")

        # Leer PDFs (con filtro opcional)
        raw_documents = self.pdf_reader.read_pdf_folder(pdfs_dir,
                                                        file_filter=file_filter)

        if not raw_documents:
            print("No se encontraron documentos para procesar")
            return 0

        # Convertir a documentos de LangChain
        langchain_docs = []

        for doc in raw_documents:
            langchain_docs.append(Document(
                page_content=doc["text"],
                metadata=doc["metadata"]
            ))

        # División en chunks; cada chunk hereda la metadata de su PDF

        print(f"\nDividiendo {len(langchain_docs)} documentos en chunks...")
        chunks = self.text_splitter.split_documents(langchain_docs)
        print(f"  Total chunks generados: {len(chunks)}")

        # Posición de cada chunk dentro de su PDF, para recuperar sus vecinos
        chunk_counter: Dict[str, int] = {}
        for chunk in chunks:
            source = chunk.metadata.get("source")
            chunk.metadata["chunk_id"] = chunk_counter.get(source, 0)
            chunk_counter[source] = chunk.metadata["chunk_id"] + 1

        # Crear índice FAISS
        print("\nCreando índice vectorial FAISS...")
        self.vector_store = FAISS.from_documents(chunks, self.embeddings)
        self._build_chunk_lookup()

        # Guardar índice para uso futuro
        self._save_index()

        print(f"Índice creado con {len(chunks)} chunks\n")
        return len(chunks)

    def _save_index(self):

        """Guarda el índice FAISS en disco"""

        if not self.vector_store:

            return

        try:
            # Asegurar que el directorio del índice existe
            self.index_dir.mkdir(parents=True, exist_ok=True)

            # Verificar que el directorio existe
            if not self.index_dir.exists():
                print(f"  ERROR: No se pudo crear el directorio {self.index_dir}")
                return

            index_path = str(self.index_dir)
            print(f"  Guardando índice en: {index_path}")

            # Guardar índice
            self.vector_store.save_local(index_path)
            print(f"  Índice guardado exitosamente")

        except Exception as e:
            print(f"  ERROR al guardar índice: {e}")
            print(f"  El índice se mantendrá solo en memoria (se perderá al cerrar)")

    def load_index(self) -> bool:

        """
        Carga el índice FAISS guardado en disco

        Returns:
            True si el índice se cargó correctamente
        """

        index_path = self.index_dir

        if not index_path.exists():
            print(f"No existe índice guardado en: {index_path}")
            return False

        try:
            print(f"Loading Index from: {index_path}")
            # allow_dangerous_deserialization es necesario para leer el pickle de
            # LangChain; es seguro porque el índice lo genera este mismo proyecto
            self.vector_store = FAISS.load_local(
                str(index_path),
                self.embeddings,
                allow_dangerous_deserialization=True
            )
            self._build_chunk_lookup()
            print("Índice cargado exitosamente")
            return True

        except Exception as e:
            print(f"Error al cargar índice: {e}")
            return False

    def _build_chunk_lookup(self):

        """Indexa los chunks del vector store por (source, chunk_id)"""

        self.chunk_lookup = {}

        # InMemoryDocstore no expone un método público para listar documentos
        for doc in self.vector_store.docstore._dict.values():
            if "chunk_id" in doc.metadata:
                key = (doc.metadata.get("source"), doc.metadata["chunk_id"])
                self.chunk_lookup[key] = doc.page_content

        if not self.chunk_lookup:
            print("  Aviso: el índice no tiene chunk_id ni link de Dropbox en la "
                  "metadata; reindexa los PDFs (load_documents_from_pdfs)")

    def search(self, query: str, k: int = None, threshold: float = None) -> List[Dict]:
        """
        Busca documentos relevantes para una consulta

        Args:
            query: Texto de búsqueda
            k: Número de resultados (usa Config.RAG_TOP_K_CHUNKS por defecto)
            threshold: Umbral mínimo de similitud (0.0 a 1.0).
                       None = usa Config.RAG_SCORE_THRESHOLD.
                       0.0 = sin filtro.

        Returns:
            Lista de documentos con texto, score y metadata
        """
        if not self.vector_store:
            print("El índice no está inicializado. Carga los documentos primero.")
            return []

        k = k or Config.RAG_TOP_K_CHUNKS

        if threshold is None:
            threshold = float(Config.RAG_SCORE_THRESHOLD)

        # Búsqueda con scores
        results = self.vector_store.similarity_search_with_score(query, k=k)

        # Formatear resultados
        formatted_results = []
        for doc, score in results:
            # FAISS devuelve la distancia L2 al cuadrado (menor es mejor). Con
            # embeddings normalizados equivale a 2 menos 2 veces el coseno, así
            # que 1 menos score/2 es la similitud coseno.
            similarity = 1 - score / 2

            # Filtro por umbral mínimo (descarta resultados de baja calidad)
            if similarity < threshold:
                continue

            formatted_results.append({
                "text": doc.page_content,
                "score": similarity,
                "metadata": doc.metadata
            })

        return formatted_results


    def load_llm(self) -> QwenFineTunedLLM:

        """Carga el LLM fine-tuned una sola vez y lo reutiliza"""

        if self.llm is None:
            self.llm = QwenFineTunedLLM()
        return self.llm

    @staticmethod
    def _join_chunks(chunks: Dict[int, str], min_overlap: int = 20) -> str:
        """
        Une los chunks de un PDF en el orden del documento, sin repetir el texto
        que comparten dos chunks consecutivos (CHUNK_OVERLAP)

        Args:
            chunks: Diccionario de chunk_id a texto
            min_overlap: Solape mínimo para considerarlo repetido (evita recortar
                         coincidencias casuales de pocos caracteres)
        """
        merged, prev_id = "", None

        for chunk_id in sorted(chunks):
            text = chunks[chunk_id]

            if prev_id is not None and chunk_id == prev_id + 1:
                # Chunks consecutivos: se busca el solape más largo entre el final
                # del texto acumulado y el inicio del chunk nuevo
                max_n = min(len(merged), len(text), Config.CHUNK_OVERLAP)
                overlap = next((n for n in range(max_n, min_overlap - 1, -1)
                                if merged.endswith(text[:n])), 0)
                merged += ("" if overlap else "\n") + text[overlap:]
            else:
                # Chunks no consecutivos: "[...]" marca el salto dentro del PDF
                merged += ("\n[...]\n" if merged else "") + text

            prev_id = chunk_id

        return merged.strip()

    def _group_by_database(self, context_chunks: List[Dict], max_databases: int,
                           chunks_per_database: int, window: int) -> List[Dict]:
        """
        Agrupa los chunks recuperados por base de datos (PDF), cada una distinta y
        una sola vez, y amplía su contexto con los chunks vecinos.

        Reglas:
            Cada PDF aparece una sola vez, con sus mejores chunks_per_database chunks.
            Un chunk nunca se repite, ni dentro de un PDF ni entre PDFs.
            El solape entre chunks consecutivos se quita al unirlos.

        Args:
            context_chunks: Resultados de search()
            max_databases: Máximo de bases de datos (PDFs distintos) a devolver
            chunks_per_database: Máximo de chunks recuperados a usar por PDF
            window: Chunks vecinos a agregar a cada lado de un chunk encontrado

        Returns:
            Lista de bases de datos, de mayor a menor score, con dataset,
            dropbox_link, score y el texto ampliado
        """
        databases: Dict[str, Dict] = {}
        seen_texts = set()

        for c in sorted(context_chunks, key=lambda c: c["score"], reverse=True):
            meta = c["metadata"]
            source = meta.get("source", "desconocida")

            if source not in databases:
                if len(databases) >= max_databases:
                    continue
                # El score de la base de datos es el de su mejor chunk
                databases[source] = {
                    "source": source,
                    "dataset": meta.get("dataset", source),
                    "title": meta.get("title") or meta.get("dataset", source),
                    "dropbox_link": meta.get("dropbox_link", ""),
                    "score": c["score"],
                    "hits": 0,
                    "chunks": {}
                }
                # El primer chunk de cada ficha (título, resumen y ficha rápida) va
                # siempre: tiene lo esencial de la base aunque no sea el más parecido
                ficha = self.chunk_lookup.get((source, 0))
                if Config.RAG_INCLUIR_FICHA and ficha and ficha not in seen_texts:
                    databases[source]["chunks"][0] = ficha
                    seen_texts.add(ficha)

            db = databases[source]
            if db["hits"] >= chunks_per_database or c["text"] in seen_texts:
                continue
            db["hits"] += 1

            chunk_id = meta.get("chunk_id")

            # Índice antiguo sin chunk_id: solo se usa el chunk encontrado
            if chunk_id is None:
                db["chunks"][len(db["chunks"])] = c["text"]
                seen_texts.add(c["text"])
                continue

            for neighbor_id in range(chunk_id - window, chunk_id + window + 1):
                text = self.chunk_lookup.get((source, neighbor_id))
                if text and text not in seen_texts:
                    db["chunks"][neighbor_id] = text
                    seen_texts.add(text)

        result = []
        for db in databases.values():
            if not db["chunks"]:
                continue
            db["text"] = self._join_chunks(db["chunks"])
            del db["chunks"], db["hits"]
            result.append(db)

        return result

    def answer_question(self, question: str, history: List[Dict] = None, k: int = None,
                        threshold: float = None, top_k_sources: int = None) -> Dict:
        """
        Respuesta RAG: recupera los chunks más similares a la pregunta y genera
        la respuesta con el Qwen3.5-4B fine-tuned (modo no thinking).

        La información de cada base de datos sale de su ficha rápida y de sus
        chunks más similares a la pregunta. El acceso sale del link de Dropbox de
        la metadata del PDF, que se agrega al final de la respuesta sin pasar por
        el LLM para que no lo altere.

        Si ningún chunk supera el umbral (saludos, preguntas fuera de alcance), el
        LLM responde sin fichas y sin links, con la regla de no describir bases.

        Estructura del prompt:
            system: rol, catálogo completo, reglas y fichas recuperadas
            history: mensajes anteriores de la conversación (opcional)
            user: pregunta del usuario

        Args:
            question: Pregunta del usuario
            history: Mensajes anteriores [{"role": "user"|"assistant", "content": ...}]
            k: Chunks candidatos a evaluar (None = todos los del índice)
            threshold: Umbral de similitud para el contexto
            top_k_sources: Máximo de bases de datos distintas en el contexto y en los links

        Returns:
            Dict con answer (respuesta con links de Dropbox), llm_answer (solo la
            respuesta del LLM), sources y used_context
        """
        prompt = self.preparar_prompt(question, k=k, threshold=threshold, top_k_sources=top_k_sources)
        databases = prompt["databases"]

        messages = [{"role": "system", "content": prompt["system_prompt"]}]
        messages += [{"role": m["role"], "content": m["content"]} for m in history or []]
        messages.append({"role": "user", "content": question})

        # 3) Generation: Qwen3.5-4B fine-tuned en modo no thinking
        llm_answer = self.load_llm().generate(messages)

        # 4) Acceso: links de Dropbox de la metadata, en orden de score. Solo hay
        #    links cuando se recuperaron fichas para responder.
        answer = llm_answer
        links = [f"🔗 {db['title']}: {db['dropbox_link']}"
                 for db in databases if db["dropbox_link"]]
        if links:
            answer += "\n\nAcceso a las bases de datos:\n" + "\n".join(links)

        sources = [
            {
                "source": db["source"],
                "dataset": db["dataset"],
                "title": db["title"],
                "dropbox_link": db["dropbox_link"],
                "score": round(float(db["score"]), 4)
            }
            for db in databases
        ]

        return {
            "answer": answer,
            "llm_answer": llm_answer,
            "sources": sources,
            "used_context": prompt["used_context"]
        }

    def preparar_prompt(self, question: str, k: int = None, threshold: float = None,
                        top_k_sources: int = None) -> Dict:
        """
        Retrieval y augmentation de una consulta: recupera las fichas relacionadas y
        arma el system prompt. Lo usan answer_question y el script que construye los
        datos de fine-tuning, para que ambos tengan exactamente el mismo formato.

        Returns:
            Dict con system_prompt, databases (fichas recuperadas) y used_context
        """
        top_k_sources = top_k_sources or Config.RAG_TOP_K_ARCHIVOS

        if not self.vector_store:
            raise RuntimeError("El índice FAISS no está cargado. Indexa los PDFs primero.")

        # 1) Retrieval: se ordenan TODOS los chunks por similitud (el índice es chico).
        #    Con solo los top k, todos podrían venir del mismo PDF y el chat
        #    respondería con una sola base de datos; así se llega a PDFs distintos.
        k = k or self.vector_store.index.ntotal
        context_chunks = self.search(question, k=k, threshold=threshold)

        # Solo chunks cercanos al mejor (a menos de RAG_SCORE_MARGIN): el umbral
        # absoluto deja pasar bases poco relacionadas cuando la mejor es muy clara
        if context_chunks:
            best_score = max(c["score"] for c in context_chunks)
            context_chunks = [c for c in context_chunks
                              if c["score"] >= best_score - Config.RAG_SCORE_MARGIN]

        # 2) Augmentation: contexto ampliado, una vez por cada base de datos distinta
        databases = self._group_by_database(
            context_chunks,
            max_databases=top_k_sources,
            chunks_per_database=Config.RAG_TOP_K_CHUNKS_POR_ARCHIVO,
            window=Config.RAG_CHUNK_WINDOW
        )

        used_context = formatear_contexto(databases)

        return {
            "system_prompt": construir_system_prompt(used_context, self.catalog_text()),
            "databases": databases,
            "used_context": used_context
        }


    def get_stats(self) -> Dict:
        """Obtiene estadísticas del índice"""
        if not self.vector_store:
            return {
                "initialized": False,
                "document_count": 0
            }

        # El número de vectores del índice FAISS es el número de chunks
        try:
            doc_count = self.vector_store.index.ntotal
        except:
            doc_count = "Desconocido"

        return {
            "initialized": True,
            "document_count": doc_count,
            "embedding_provider": Config.EMBEDDING_PROVIDER,
            "embedding_model": (Config.EMBEDDING_MODEL),
            "chunk_size": Config.CHUNK_SIZE,
            "chunk_overlap": Config.CHUNK_OVERLAP,
            "top_k": Config.RAG_TOP_K_CHUNKS,
            "score_threshold": Config.RAG_SCORE_THRESHOLD
        }

    def list_databases(self) -> List[Dict]:
        """
        Catálogo de bases de datos indexadas, una entrada por PDF

        Returns:
            Lista ordenada por categoría y título, con dataset, title, category y dropbox_link
        """
        if not self.vector_store:
            return []

        databases: Dict[str, Dict] = {}
        for doc in self.vector_store.docstore._dict.values():
            meta = doc.metadata
            source = meta.get("source", "desconocida")
            databases.setdefault(source, {
                "dataset": meta.get("dataset", source),
                "title": meta.get("title") or meta.get("dataset", source),
                "category": meta.get("category", "Documento general"),
                "dropbox_link": meta.get("dropbox_link", "")
            })

        return sorted(databases.values(), key=lambda d: (d["category"], d["title"]))

    def catalog_text(self) -> str:
        """Catálogo completo como texto para el system prompt: títulos agrupados por tema"""
        grupos: Dict[str, List[str]] = {}
        for db in self.list_databases():
            grupos.setdefault(db["category"], []).append(db["title"])
        return "\n".join(f"{categoria}: {'; '.join(titulos)}." for categoria, titulos in grupos.items())

    def is_ready(self) -> bool:
        """Verifica si el servicio está listo para consultas"""
        return self.vector_store is not None
