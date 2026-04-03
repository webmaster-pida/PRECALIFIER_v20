# src/modules/gemini_client.py

import vertexai
import asyncio 
import re 
import random 
from vertexai.generative_models import (
    GenerativeModel, 
    Content, 
    Part, 
    GenerationConfig, 
    SafetySetting, 
    HarmCategory, 
    HarmBlockThreshold
)
from typing import List, AsyncGenerator, Set
from src.config import settings, log
from src.models.chat_models import ChatMessage

# --- INICIALIZACIÓN DEL CLIENTE Y MODELO ---
try:
    vertexai.init(project=settings.GOOGLE_CLOUD_PROJECT, location=settings.GOOGLE_CLOUD_LOCATION)

    generation_config = GenerationConfig(
        max_output_tokens=settings.MAX_OUTPUT_TOKENS,
        temperature=settings.TEMPERATURE,
        top_p=settings.TOP_P,
    )

    safety_settings = [
        SafetySetting(category=HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH),
        SafetySetting(category=HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH),
        SafetySetting(category=HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH),
        SafetySetting(category=HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH),
    ]

    model = GenerativeModel(settings.GEMINI_MODEL)
    log.info(f"Cliente de Vertex AI inicializado y modelo '{settings.GEMINI_MODEL}' cargado.")

except Exception as e:
    log.critical(f"No se pudo inicializar Vertex AI o cargar el modelo: {e}", exc_info=True)
    model = None

# --- FUNCIONES AUXILIARES ---

def prepare_history_for_vertex(history: List[ChatMessage]) -> List[Content]:
    """Convierte nuestro historial de Pydantic al formato que espera la API de Gemini."""
    vertex_history = []
    for message in history:
        role = 'user' if message.role == 'user' else 'model'
        vertex_history.append(Content(role=role, parts=[Part.from_text(message.content)]))
    return vertex_history

async def generate_streaming_response(
    system_prompt: str, 
    prompt: str, 
    history: List[Content],
    trusted_urls: Set[str] = set()
) -> AsyncGenerator[str, None]:
    """
    Genera una respuesta del modelo Gemini en modo streaming con limpieza de URLs.
    """
    if not model:
        log.error("El modelo Gemini no está disponible.")
        yield "Error: El modelo de IA no está configurado correctamente."
        return

    try:
        chat = model.start_chat(history=history, response_validation=False)
        full_prompt = f"{system_prompt}\n\n---\n\n{prompt}"
        
        response_stream = await chat.send_message_async(
            full_prompt, 
            stream=True, 
            generation_config=generation_config,
            safety_settings=safety_settings
        )

        text_buffer = ""

        async for chunk in response_stream:
            try:
                if chunk.text:
                    text_buffer += chunk.text
                    
                    # --- LÓGICA DE LIMPIEZA DE URLS ---
                    def is_url_trusted(url_to_check):
                        clean_check = url_to_check.lower().strip().rstrip('/')
                        for t_url in trusted_urls:
                            clean_trust = t_url.lower().strip().rstrip('/')
                            if clean_check == clean_trust or clean_check.startswith(clean_trust):
                                return True
                        return False

                    # 1. Links Markdown (Limpieza agresiva de URLs)
                    md_pattern = r'\[([^\]]+)\]\s*\(\s*(https?://[^\s\)]+)\s*\)'
                    def replace_markdown_link(match):
                        text = match.group(1)
                        # Limpiamos signos de puntuación que Gemini suele pegar al final
                        url = match.group(2).rstrip('.,;)>') 
                        return f"[{text}]({url})" if is_url_trusted(url) else text
                    
                    text_buffer = re.sub(md_pattern, replace_markdown_link, text_buffer)

                    # 2. URLs Sueltas
                    raw_pattern = r'(?<!\()(https?://[^\s\)]+)' 
                    def replace_raw_url(match):
                        url = match.group(0).rstrip('.,;)>')
                        return url if is_url_trusted(url) else ""
                    
                    text_buffer = re.sub(raw_pattern, replace_raw_url, text_buffer)

                    # 3. Limpieza mejorada de Artifacts de Citas: [1], (2), [3, 4]
                    text_buffer = re.sub(r'\s?[\[\(]\s*\d+(?:\s*,\s*\d+)*\s*[\]\)]', '', text_buffer)

                    # 4. REPARACIÓN DE MARKDOWN ROTO
                    text_buffer = text_buffer.replace(">**", "**")
                    text_buffer = text_buffer.replace(" <", " \"")
                    text_buffer = text_buffer.replace("> ", "\" ")

                    # 5. Buffering para evitar cortes en media palabra
                    if len(text_buffer) < 300: 
                        continue
                    yield text_buffer
                    text_buffer = ""

            except (ValueError, Exception):
                yield "\n\n[Contenido bloqueado por políticas de seguridad]"
        
        if text_buffer:
            yield text_buffer

    except Exception as e:
        log.error(f"Error al generar la respuesta en streaming desde Gemini: {e}", exc_info=True)
        yield "Hubo un problema al contactar al servicio de IA."
