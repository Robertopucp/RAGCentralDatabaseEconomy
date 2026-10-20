"""
Lector de PDFs para el sistema RAG.

Cada base de datos del catálogo tiene un PDF de descripción (ficha técnica) con
sus variables, cobertura, periodos y, al final, el link de Dropbox de acceso.
Este módulo extrae el texto limpio para indexarlo y el link para la metadata.
"""
from pathlib import Path
from typing import List, Dict
import re
import unicodedata


# Patrón del texto "Link de Dropbox: https://www.dropbox.com/...&dl=0" al final de
# cada PDF. Al extraer el texto el link viene partido en varias líneas, por eso la
# búsqueda abarca saltos de línea hasta el parámetro final "dl=<n>".
DROPBOX_TEXT_PATTERN = re.compile(
    r"link de dropbox\s*:?\s*(https?://(?:www\.)?dropbox\.com.*?dl=\d)",
    re.IGNORECASE | re.DOTALL
)

# Nombre oficial de la base, en la línea "Nombre de base de datos: ..." de cada ficha
TITLE_PATTERN = re.compile(
    r"nombre de base de datos\s*:?\s*(.+?)\s*resumen\b",
    re.IGNORECASE | re.DOTALL
)

# Sección final de cada ficha con la fuente y los links. Se excluye del texto
# indexado: el link de Dropbox va en la metadata y las URLs en el contexto inducen
# al LLM a escribir links en la respuesta.
SOURCE_SECTION_PATTERN = re.compile(r"\bfuente y acceso\b.*", re.IGNORECASE | re.DOTALL)


class PDFReader:
    """Lee y extrae texto de archivos PDF"""

    def __init__(self):
        """Inicializa el lector de PDF"""

        try:
            from pypdf import PdfReader
            self.PdfReader = PdfReader
        except ImportError:
            raise ImportError(
                "pypdf is not installed. Install it with pip install pypdf"
            )

    def extract_text_from_pdf(self, pdf_path: str) -> str:

        """
        Extrae y limpia el texto de un archivo PDF

        Args:
            pdf_path: Ruta al archivo PDF

        Returns:
            Texto extraído del PDF
        """

        try:
            reader = self.PdfReader(pdf_path)
            text = "\n".join(page.extract_text() or "" for page in reader.pages)

            # El link de Dropbox se guarda en la metadata (ver extract_dropbox_link);
            # la sección de fuente y links se excluye del texto indexado
            text = SOURCE_SECTION_PATTERN.sub("", text)
            text = DROPBOX_TEXT_PATTERN.sub("", text)

            # Texto original (mayúsculas, tildes y puntuación): el LLM lo lee mejor
            # y los embeddings de Qwen3 no necesitan normalización
            text = self.clean_text(text, normalizar=False)
            return text

        except Exception as e:
            print(f"Error al leer {pdf_path}: {e}")
            return ""

    def extract_dropbox_link(self, pdf_path: str) -> str:

        """
        Extrae el link de Dropbox de acceso a la base de datos

        Primero lo busca en los hipervínculos del PDF, donde el link está completo.
        Si no hay hipervínculo, lo arma desde el texto "Link de Dropbox: ..."
        uniendo las líneas en que viene partido.

        Args:
            pdf_path: Ruta al archivo PDF

        Returns:
            Link de Dropbox, o "" si el PDF no tiene
        """

        try:
            reader = self.PdfReader(pdf_path)

            # Se recorre desde la última página porque el link siempre va al final
            for page in reversed(reader.pages):
                for annot in page.get("/Annots") or []:
                    action = annot.get_object().get("/A")
                    uri = action.get_object().get("/URI") if action else None
                    if uri and "dropbox.com" in str(uri):
                        return str(uri).strip()

            # Respaldo: link escrito como texto, sin hipervínculo
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            match = DROPBOX_TEXT_PATTERN.search(text)
            if match:
                return re.sub(r"\s+", "", match.group(1))

        except Exception as e:
            print(f"Error al extraer link de Dropbox de {pdf_path}: {e}")

        return ""


    def clean_text(self, text: str, normalizar: bool=True) -> str:
        """
        Limpia el texto extraído del PDF.

        Args:
            text: Texto a limpiar
            normalizar: si es True, pasa a minúsculas, quita tildes y
                        elimina caracteres que no sean letras, números o "_"

        Returns:
            Texto limpio
        """

        text = unicodedata.normalize("NFC",
                                     text)

        if normalizar:

            text = text.lower()

            # Quita las tildes y conserva la virgulilla de la ñ (U+0303)
            text = unicodedata.normalize("NFD", text)
            text = "".join(
                c for c in text
                if unicodedata.category(c) != "Mn" or c == "̃"
            )
            text = unicodedata.normalize("NFC", text)

            text = re.sub(r"[^a-z0-9ñ_ \n]"," ", text)


        # Eliminar múltiples espacios
        text = re.sub(r'[ \t]+', ' ', text)

        # Eliminar múltiples saltos de línea (mantener máximo 2)
        text = re.sub(r'\n{3,}', '\n\n', text)

        # Eliminar espacios al inicio y final de líneas
        lines = [line.strip() for line in text.split('\n')]
        text = '\n'.join(lines)

        # Eliminar espacios al inicio y final
        text = text.strip()

        return text

    def extract_title(self, pdf_path: str) -> str:

        """
        Extrae el nombre oficial de la base de datos (línea "Nombre de base de datos")

        Args:
            pdf_path: Ruta al archivo PDF

        Returns:
            Título de la base de datos, o "" si la ficha no lo tiene
        """

        try:
            reader = self.PdfReader(pdf_path)
            text = reader.pages[0].extract_text() or ""
            match = TITLE_PATTERN.search(text)
            if match:
                title = re.sub(r"\s+", " ", match.group(1)).strip()
                # Une rangos de años partidos por el salto de línea ("1995- 2013")
                return re.sub(r"(\d)\s*-\s+(\d)", r"\1-\2", title)
        except Exception as e:
            print(f"Error al extraer el título de {pdf_path}: {e}")

        return ""



    def read_pdf_folder(self, folder_path: str, recursive: bool = True,
                        file_filter: callable = None) -> List[Dict]:
        """
        Lee todos los PDFs de descripción de una carpeta

        Args:
            folder_path: Ruta a la carpeta con PDFs
            recursive: Si True, busca en subcarpetas también
            file_filter: Función que recibe el nombre del archivo y devuelve
                         True si debe procesarse (None = procesar todos)

        Returns:
            Lista de diccionarios con texto y metadata de cada PDF
        """
        folder = Path(folder_path)

        if not folder.exists():
            raise FileNotFoundError(f"La carpeta {folder_path} no existe")

        # Solo se indexan las fichas de descripción ("*description*.pdf"); las
        # carpetas de cada base también pueden tener otros PDFs que no aplican
        if recursive:
            pdf_files = list(folder.rglob("*description*.pdf"))
        else:
            pdf_files = list(folder.glob("*.pdf"))

        # Aplicar filtro opcional (indexar solo ciertos archivos)
        if file_filter:
            pdf_files = [f for f in pdf_files if file_filter(f.name)]

        if not pdf_files:
            print(f"No se encontraron archivos PDF en {folder_path}")
            return []

        print(f"Encontrados {len(pdf_files)} archivos PDF")

        documents = []

        for pdf_file in pdf_files:
            print(f"  Leyendo: {pdf_file.name}...", end=" ")

            text = self.extract_text_from_pdf(str(pdf_file))

            if text:
                dropbox_link = self.extract_dropbox_link(str(pdf_file))
                if not dropbox_link:
                    print("(sin link de Dropbox)", end=" ")

                # Esta metadata se copia a cada chunk del PDF al indexar
                documents.append({
                    "text": text,
                    "metadata": {
                        "source": pdf_file.name,
                        # Nombre de la base de datos: el archivo sin "description.pdf"
                        "dataset": re.sub(r"description$", "", pdf_file.stem, flags=re.IGNORECASE),
                        # Nombre oficial de la base, para el catálogo y los links
                        "title": self.extract_title(str(pdf_file)),
                        "dropbox_link": dropbox_link,
                        "filepath": str(pdf_file),
                        "size_bytes": pdf_file.stat().st_size,
                        "type": "pdf",
                        "category": self._detect_category(pdf_file.name)
                    }
                })
                print(f"OK ({len(text):,} caracteres)")
            else:
                print("Non Text")

        print(f"\nTotal documentos procesados: {len(documents)}")

        return documents


    # Categorías según el prefijo del nombre del archivo: Educacion, Empresarial,
    # Pobreza, RemoteData, SeguridadCiudadana, Politica, EconomiaIlegal y Shapefile

    def _detect_category(self, filename: str) -> str:
        """
        Detecta la categoría del documento basándose en el nombre

        Args:
            filename: Nombre del archivo

        Returns:
            Categoría detectada
        """
        filename_lower = filename.lower()

        if "educacion" in filename_lower:
            return "Educación"
        elif "empresarial" in filename_lower:
            return "Empresas y productividad"
        elif "pobreza" in filename_lower:
            return "Pobreza"
        elif "remotedata" in filename_lower:
            return "Teledetección: actividad económica y geografía"
        elif "seguridadciudadana" in filename_lower:
            return "Seguridad ciudadana"
        elif "economiailegal" in filename_lower:
            return "Economías ilícitas y medio ambiente"
        elif "politica" in filename_lower:
            return "Política y corrupción"
        elif "shapefile" in filename_lower:
            return "Cartografía, clima e infraestructura (shapefiles)"
        else:
            return "Documento general"



    def get_pdf_info(self, pdf_path: str) -> Dict:
        """
        Obtiene información detallada de un PDF

        Args:
            pdf_path: Ruta al PDF

        Returns:
            Diccionario con información del PDF
        """
        try:
            reader = self.PdfReader(pdf_path)

            info = {
                "filepath": pdf_path,
                "num_pages": len(reader.pages),
                "metadata": {}
            }

            # Extraer metadata del PDF si existe
            if reader.metadata:
                info["metadata"] = {
                    "title": reader.metadata.title if reader.metadata.title else None,
                    "author": reader.metadata.author if reader.metadata.author else None,
                    "subject": reader.metadata.subject if reader.metadata.subject else None,
                    "creator": reader.metadata.creator if reader.metadata.creator else None,
                }

            return info

        except Exception as e:
            return {"error": str(e), "filepath": pdf_path}
