"""
Construye el dataset de fine-tuning en el formato exacto que el chatbot recibe en producción.

Las conversaciones nuevas (data/Finetuning/conversaciones_rag.md) se escriben a partir de
las fichas PDF de cada base de datos. Este script les agrega el system prompt real: ejecuta
el RAG sobre la pregunta del usuario (mismo índice, umbral, catálogo y reglas que la web),
de modo que el modelo aprende a responder con las fichas que recibe y no de memoria.

Verificaciones de cada conversación nueva:
    1. Recuperación: las bases recuperadas son las esperadas (o ninguna, en saludos y
       preguntas fuera de alcance, para que esas respuestas no lleven links).
    2. Grounding: cada número, año, sigla e identificador de variable de la respuesta
       aparece en el system prompt (fichas y catálogo) o en el mensaje del usuario.
    3. Sin links: el asistente nunca escribe URLs; los links los agrega el código.
    4. Largo: la conversación cabe en MAX_TOKENS tokens, el largo de entrenamiento.

Salida (data/Finetuning/dataset_EconData.jsonl):
    Conversaciones originales (dataset_EconData_v1.jsonl) sin los links que escribía el
    asistente, más las conversaciones nuevas con su system prompt.

Formato de conversaciones_rag.md:
    ## categoria | Base1, Base2      (códigos de las bases esperadas, o un guion si ninguna)
    @usuario
    texto del usuario
    @asistente
    texto del asistente (puede tener varias líneas y markdown)

Uso, desde la raíz del repositorio y con Ollama corriendo:
    python -m src.construir_dataset_rag
"""
import io
import json
import re
import sys
import contextlib
from pathlib import Path

from src.config import Config
from src.rag_service import RAGService

DIR_DATOS = Config.BASE_DIR / "data" / "Finetuning"
RUTA_FUENTE = DIR_DATOS / "conversaciones_rag.md"
RUTA_V1 = DIR_DATOS / "dataset_EconData_v1.jsonl"
RUTA_SALIDA = DIR_DATOS / "dataset_EconData.jsonl"

# Largo máximo de una conversación en tokens: debe coincidir con MAX_SEQ_LENGTH del
# notebook de fine-tuning
MAX_TOKENS = 2560

# Siglas de uso general que pueden aparecer sin estar en las fichas
SIGLAS_PERMITIDAS = {"PDF", "OK"}


def leer_conversaciones(ruta: Path) -> list:
    """Lee conversaciones_rag.md y devuelve una lista de conversaciones"""
    conversaciones, actual, rol, lineas = [], None, None, []

    def cerrar_turno():
        if actual is not None and rol is not None:
            actual["mensajes"].append({"role": rol, "content": "\n".join(lineas).strip()})

    for numero, linea in enumerate(ruta.read_text(encoding="utf-8").splitlines(), 1):
        if linea.startswith("## "):
            cerrar_turno()
            categoria, _, esperado = linea[3:].partition("|")
            esperado = esperado.strip()
            actual = {
                "linea": numero,
                "categoria": categoria.strip(),
                "esperado": [] if esperado in ("", "-") else [e.strip() for e in esperado.split(",")],
                "mensajes": [],
            }
            conversaciones.append(actual)
            rol, lineas = None, []
        elif linea.strip() in ("@usuario", "@asistente"):
            cerrar_turno()
            rol, lineas = ("user" if linea.strip() == "@usuario" else "assistant"), []
        elif rol is not None:
            lineas.append(linea)
    cerrar_turno()
    return conversaciones


def terminos_verificables(texto: str) -> set:
    """Números, siglas e identificadores de variables que deben estar en las fichas"""
    terminos = set()
    for codigo in re.findall(r"`([^`]+)`", texto):
        terminos.add(codigo.strip())
    sin_codigo = re.sub(r"`[^`]+`", " ", texto)
    terminos |= set(re.findall(r"\b\w*_\w+\b", sin_codigo))                      # snake_case
    terminos |= set(re.findall(r"\b[A-Z][A-Z0-9]{1,}\b", sin_codigo)) - SIGLAS_PERMITIDAS  # siglas
    terminos |= set(re.findall(r"\b[a-z]+[A-Z]\w*\b", sin_codigo))                # camelCase
    terminos |= set(re.findall(r"\b[A-Z][a-z]+[A-Z]\w*\b", sin_codigo))           # PascalCase
    for numero in re.findall(r"\d[\d.,]*\d|\d", sin_codigo):
        terminos.add(numero)
    return terminos


def limpiar_links(texto: str) -> str:
    """Quita los links que escribía el asistente en las conversaciones originales"""
    lineas = [l for l in texto.split("\n") if not re.match(r"\s*- \*\*Descarga:\*\*", l)]
    texto = "\n".join(lineas)
    texto = re.sub(r":?\s*\[🔗[^\]]*\]\([^)]*\)\.?", ".", texto)
    texto = re.sub(r"https?://\S+", "", texto)
    texto = re.sub(r"\.\.", ".", texto)
    texto = re.sub(r"\.(?=[A-ZÁÉÍÓÚ¿¡])", ". ", texto)
    return re.sub(r"\n{3,}", "\n\n", texto).strip()


def main():
    with contextlib.redirect_stdout(io.StringIO()):
        rag = RAGService()
        if not rag.load_index():
            sys.exit("No hay índice FAISS: ejecuta notebooks/1.IndexingFAISS.ipynb")

    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(str(Config.LLM_MODEL_PATH))

    conversaciones = leer_conversaciones(RUTA_FUENTE)
    errores, avisos, ejemplos, largos = [], [], [], []

    for conv in conversaciones:
        ref = f"línea {conv['linea']} ({conv['categoria']})"
        mensajes = conv["mensajes"]
        roles = [m["role"] for m in mensajes]
        if not mensajes or roles[-1] != "assistant" or any(
                roles[i] == roles[i + 1] for i in range(len(roles) - 1)) or roles[0] != "user":
            errores.append(f"{ref}: los turnos deben alternar usuario y asistente")
            continue

        # El system prompt corresponde al último mensaje del usuario, como en la web
        ultimo_usuario = [m["content"] for m in mensajes if m["role"] == "user"][-1]
        prompt = rag.preparar_prompt(ultimo_usuario)
        recuperadas = [db["dataset"] for db in prompt["databases"]]

        # 1) Recuperación
        if conv["esperado"]:
            faltan = [e for e in conv["esperado"] if e not in recuperadas]
            if faltan:
                errores.append(f"{ref}: no se recuperó {faltan}; se recuperó {recuperadas}")
            extra = [r for r in recuperadas if r not in conv["esperado"]]
            if extra:
                avisos.append(f"{ref}: también se recuperó {extra}")
        elif recuperadas:
            errores.append(f"{ref}: se esperaba ninguna ficha y se recuperó {recuperadas}")

        # 2) Grounding de la respuesta final
        respuesta = mensajes[-1]["content"]
        fuente = prompt["system_prompt"] + "\n" + "\n".join(
            m["content"] for m in mensajes if m["role"] == "user")
        sin_respaldo = sorted(t for t in terminos_verificables(respuesta) if t not in fuente)
        if sin_respaldo:
            errores.append(f"{ref}: términos que no están en las fichas: {sin_respaldo}")

        # 3) Sin links
        if any(re.search(r"https?://|dropbox\.com|🔗", m["content"]) for m in mensajes
               if m["role"] == "assistant"):
            errores.append(f"{ref}: el asistente no debe escribir links")

        conversacion = [{"role": "system", "content": prompt["system_prompt"]}] + mensajes
        texto = tokenizer.apply_chat_template(conversacion, tokenize=False)
        largo = len(tokenizer(texto)["input_ids"])
        largos.append(largo)
        # 4) Largo de entrenamiento
        if largo > MAX_TOKENS:
            errores.append(f"{ref}: {largo} tokens, más que MAX_TOKENS={MAX_TOKENS}")

        ejemplos.append({"categoria": conv["categoria"], "fuente": "rag", "conversations": conversacion})

    for aviso in avisos:
        print("Aviso:", aviso)
    for error in errores:
        print("ERROR:", error)
    if largos:
        largos.sort()
        print(f"\nConversaciones nuevas: {len(ejemplos)} | tokens: mediana {largos[len(largos) // 2]}, "
              f"máximo {largos[-1]}")
    if errores:
        sys.exit(f"\n{len(errores)} errores: corrige conversaciones_rag.md y vuelve a ejecutar")

    # Conversaciones originales sin los links que escribía el asistente
    originales = []
    for linea in RUTA_V1.read_text(encoding="utf-8").splitlines():
        fila = json.loads(linea)
        for mensaje in fila["conversations"]:
            if mensaje["role"] == "assistant":
                mensaje["content"] = limpiar_links(mensaje["content"])
        originales.append({"categoria": fila["categoria"], "fuente": "v1",
                           "conversations": fila["conversations"]})

    with open(RUTA_SALIDA, "w", encoding="utf-8", newline="\n") as f:
        for fila in originales + ejemplos:
            f.write(json.dumps(fila, ensure_ascii=False) + "\n")
    print(f"Dataset guardado en {RUTA_SALIDA}: {len(originales)} originales + {len(ejemplos)} nuevas")


if __name__ == "__main__":
    main()
