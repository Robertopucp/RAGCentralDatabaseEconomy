"""
Kalish Manta Analytics: interfaz web del chatbot con Streamlit.

Acceso a bases de datos para investigación. El usuario describe su investigación,
el RAG recupera las fichas de las bases de datos más relacionadas y el Qwen3.5-4B
fine-tuned las explica. Cada respuesta incluye los links de Dropbox de acceso.

Estructura de la página:
    Barra lateral: configuración de la búsqueda y pipeline del sistema.
    Pestaña Consulta: campo de texto, botón y respuestas de la conversación.
    Pestaña Catálogo: títulos de todas las bases de datos, agrupados por tema.

Cada interacción se guarda en el registro JSON Lines definido en INTERACCIONES_PATH.

Uso, desde la raíz del repositorio y con Ollama corriendo:
    streamlit run app.py
    La web queda disponible en http://localhost:8501
"""
import uuid

import streamlit as st

from src.config import Config
from src.llm_service import build_chatbot


st.set_page_config(
    page_title="Kalish Manta Analytics",
    page_icon="📊",
    layout="wide",
)

# Estilos mínimos: la paleta de azules claros está en .streamlit/config.toml;
# aquí solo se ajustan tamaños, espaciado y el estilo del título y subtítulo
st.markdown("""
<style>
.block-container { padding-top: 3rem; max-width: 1100px; }
#MainMenu, footer { visibility: hidden; }

.kma-title {
    font-size: 2.6rem;
    font-weight: 700;
    color: #163A5F;
    line-height: 1.2;
    margin-bottom: 0.6rem;
}
.kma-subtitle {
    font-style: italic;
    font-size: 1.05rem;
    color: #3A4A5C;
    margin-bottom: 0.4rem;
}
.kma-links-title {
    margin-top: 0.6rem;
    font-size: 0.8rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: #2B7BBE;
}
</style>
""", unsafe_allow_html=True)


@st.cache_resource(show_spinner="Cargando las bases de datos y el modelo de lenguaje "
                                "(la primera vez tarda 1-2 minutos)...")
def get_chatbot():
    """Un solo chatbot (y una sola copia del LLM en la GPU) para todos los usuarios"""
    return build_chatbot()


def to_markdown(text: str) -> str:
    """Escapa el signo $ para que Streamlit no lo interprete como fórmula LaTeX"""
    return text.replace("$", r"\$")


def render_links(sources: list):
    """Links de Dropbox de acceso a las bases de datos de la respuesta. Solo aparecen
    cuando el RAG recuperó fichas para responder."""
    links = [s for s in sources if s.get("dropbox_link")]
    if not links:
        return
    st.markdown('<div class="kma-links-title">Acceso a las bases de datos</div>',
                unsafe_allow_html=True)
    st.markdown("\n".join(
        f"- 🔗 [{s.get('title') or s['dataset']}]({s['dropbox_link']})" for s in links
    ))


chatbot = get_chatbot()

# Streamlit vuelve a ejecutar este script en cada interacción; session_state
# conserva los datos de cada pestaña del navegador, que es un usuario con su
# propia memoria de conversación
if "user_id" not in st.session_state:
    st.session_state.user_id = str(uuid.uuid4())
if "messages" not in st.session_state:
    st.session_state.messages = []


# ===== BARRA LATERAL: CONFIGURACIÓN Y PIPELINE =====

with st.sidebar:
    st.markdown("### ⚙️ Configuración")
    st.caption("Los cambios se aplican desde tu siguiente consulta.")

    # El máximo y el valor inicial salen de RAG_TOP_K_ARCHIVOS (.env); el slider
    # necesita un máximo mayor que el mínimo, por eso nunca baja de 2
    top_k_sources = st.slider(
        "Número de bases de datos", min_value=1,
        max_value=max(Config.RAG_TOP_K_ARCHIVOS, 2),
        value=Config.RAG_TOP_K_ARCHIVOS,
    )
    st.caption("Máximo de bases de datos que el asistente puede usar en una respuesta. "
               "Súbelo si tu investigación combina varios temas.")

    threshold = st.slider(
        "Similitud mínima", min_value=0.40, max_value=0.80,
        value=float(Config.RAG_SCORE_THRESHOLD), step=0.01,
    )
    st.caption("Qué tan relacionada con tu consulta debe estar una base para usarla. "
               "Más alto: solo bases muy relacionadas, aunque a veces no encuentre ninguna. "
               "Más bajo: más bases, pero algunas pueden estar poco relacionadas.")

    if st.button("Nueva conversación", use_container_width=True):
        chatbot.clear_memory(st.session_state.user_id)
        st.session_state.messages = []
        st.rerun()

    st.divider()

    st.markdown("**Pipeline:**")
    st.markdown(
        "1. 🔍 Búsqueda semántica en FAISS (qwen3-embedding)\n"
        "2. 📑 Ficha rápida y fragmento más relacionado de cada base\n"
        "3. 🤖 Respuesta del Qwen3.5-4B fine-tuned con QLoRA\n"
        "4. 🔗 Links de acceso a cada base de datos\n"
        "5. 💬 Memoria de la conversación"
    )

    st.divider()
    st.caption("Respuestas generadas por un modelo de lenguaje a partir de las fichas "
               "técnicas de cada base de datos. Verifica la información en su documentación.")


# ===== ENCABEZADO =====

st.markdown('<div class="kma-title">📊 Kalish Manta Analytics</div>', unsafe_allow_html=True)
st.markdown('<div class="kma-subtitle">Acceso a bases de datos para investigación: '
            'describe tu investigación → el asistente encuentra las bases de datos, '
            'te explica su contenido y te da el link de acceso</div>',
            unsafe_allow_html=True)
st.divider()

tab_consulta, tab_catalogo = st.tabs(["Consulta", "Catálogo de bases de datos"])


# ===== PESTAÑA CONSULTA =====

with tab_consulta:
    # Formulario: Enter o el botón envían la consulta y el campo se limpia
    with st.form("consulta", clear_on_submit=True, border=False):
        question = st.text_area(
            "📝 Resume el abstract de tu investigación, tu hipótesis o el objetivo de tu "
            "trabajo o análisis económico:",
            placeholder="Ej.: Mi tesis evalúa si la pobreza distrital bajó entre 2013 y 2018 "
                        "en la sierra sur del Perú.",
            max_chars=2000,
            height=100,
        )
        submitted = st.form_submit_button("🚀 Buscar bases de datos", type="primary")

    if submitted and question.strip():
        try:
            with st.spinner("Buscando en las fichas de las bases de datos..."):
                response = chatbot.handle_chat(
                    st.session_state.user_id, question,
                    top_k_sources=top_k_sources, threshold=threshold
                )
            st.session_state.messages.append({
                "question": question,
                "answer": response["llm_answer"],
                "sources": response["sources"],
            })
        except Exception as e:
            st.error("No se pudo generar la respuesta. Verifica que Ollama esté "
                     f"corriendo e intenta de nuevo.\n\nDetalle: {e}")

    # Conversación con la consulta más reciente primero, justo debajo del botón
    for msg in reversed(st.session_state.messages):
        with st.chat_message("user", avatar="🎓"):
            st.markdown(to_markdown(msg["question"]))
        with st.chat_message("assistant", avatar="📊"):
            st.markdown(to_markdown(msg["answer"]))
            render_links(msg["sources"])


# ===== PESTAÑA CATÁLOGO =====

with tab_catalogo:
    st.markdown("Bases de datos disponibles, agrupadas por tema. Pregunta por cualquiera "
                "en la pestaña **Consulta** para conocer su contenido y obtener su link de acceso.")

    # Solo títulos generales: el acceso se entrega en el chat, junto con la
    # explicación de la base
    grupos = {}
    for db in chatbot.rag.list_databases():
        grupos.setdefault(db["category"], []).append(db["title"])
    for categoria, titulos in grupos.items():
        st.markdown(f"#### {categoria}")
        st.markdown("\n".join(f"- {titulo}" for titulo in titulos))
