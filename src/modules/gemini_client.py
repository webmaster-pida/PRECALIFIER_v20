# src/modules/gemini_client.py

import vertexai
import asyncio 
import re 
import random 
import google.cloud.aiplatform as aiplatform
from google.api_core.exceptions import ResourceExhausted, ServiceUnavailable, Aborted, InternalServerError
from vertexai.generative_models import (
    GenerativeModel, 
    Content, 
    Part, 
    GenerationConfig, 
    Tool, 
    HarmCategory, 
    HarmBlockThreshold
)
from typing import List, AsyncGenerator, Set
from src.config import settings, log
from src.models.chat_models import ChatMessage

# --- INICIALIZACIÓN ---
try:
    vertexai.init(project=settings.GOOGLE_CLOUD_PROJECT, location=settings.GOOGLE_CLOUD_LOCATION)

    generation_config = GenerationConfig(
        max_output_tokens=settings.MAX_OUTPUT_TOKENS,
        temperature=settings.TEMPERATURE,
        top_p=settings.TOP_P,
    )

    safety_settings = {
        HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
    }

    model = GenerativeModel(settings.GEMINI_MODEL)
    log.info(f"Cliente de Vertex AI inicializado y modelo '{settings.GEMINI_MODEL}' cargado.")

except Exception as e:
    log.critical(f"No se pudo inicializar Vertex AI o cargar el modelo: {e}", exc_info=True)
    model = None

# --- UTILS ---

def prepare_history_for_vertex(history: List[ChatMessage]) -> List[Content]:
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
    
    if not model:
        log.error("El modelo Gemini no está disponible.")
        yield "Error: El modelo de IA no está configurado correctamente."
        return

    # RETRY LOGIC
    MAX_RETRIES = 3
    BASE_DELAY = 2 

    full_prompt = f"{system_prompt}\n\n---\n\n{prompt}"

    for attempt in range(MAX_RETRIES + 1):
        try:
            chat = model.start_chat(history=history, response_validation=False)
            
            response_stream = await chat.send_message_async(
                full_prompt, 
                stream=True, 
                generation_config=generation_config,
                safety_settings=safety_settings
            )

            text_buffer = "" 

            async for chunk in response_stream:
                # Se eliminó la captura automática de grounding_metadata de Google
                # para evitar enlaces de redirección que causan errores de CORS.

                if chunk.text:
                    text_buffer += chunk.text
                    
                    # --- CLEANING LOGIC ---
                    def is_url_trusted(url_to_check):
                        clean_check = url_to_check.lower().strip().rstrip('/')
                        for t_url in trusted_urls:
                            clean_trust = t_url.lower().strip().rstrip('/')
                            if clean_check == clean_trust or clean_check.startswith(clean_trust):
                                return True
                        return False

                    # 1. Links Markdown
                    md_pattern = r'\[([^\]]+)\]\s*\(\s*(https?://[^\s\)]+)\s*\)'
                    def replace_markdown_link(match):
                        return match.group(0) if is_url_trusted(match.group(2)) else match.group(1)
                    text_buffer = re.sub(md_pattern, replace_markdown_link, text_buffer)

                    # 2. URLs Sueltas
                    raw_pattern = r'(?<!\()(https?://[^\s\)]+)' 
                    def replace_raw_url(match):
                        return match.group(0) if is_url_trusted(match.group(0)) else ""
                    text_buffer = re.sub(raw_pattern, replace_raw_url, text_buffer)

                    # 3. Limpieza mejorada de Artifacts de Citas: [1], (2), [3, 4], (5, 15, 16)
                    text_buffer = re.sub(r'\s?[\[\(]\s*\d+(?:\s*,\s*\d+)*\s*[\]\)]', '', text_buffer)

                    # 4. REPARACIÓN DE MARKDOWN ROTO
                    text_buffer = text_buffer.replace(">**", "**")
                    text_buffer = text_buffer.replace(" <", " \"")
                    text_buffer = text_buffer.replace("> ", "\" ")
                    text_buffer = re.sub(r'\*\*\s*$', '', text_buffer, flags=re.MULTILINE)

                    # 5. AGGRESSIVE STRUCTURE CLEANING
                    text_buffer = re.sub(r'(?m)^\s*[\-\*•>]\s*$', '', text_buffer)
                    text_buffer = re.sub(r'(?m)^\s*>\s*>\s*$', '', text_buffer)
                    text_buffer = re.sub(r'\n\s*\n\s*\n', '\n\n', text_buffer)

                    # 6. BUFFERING
                    if len(text_buffer) < 400: 
                        if any(text_buffer.strip().endswith(c) for c in ['[', '(', '*', '-', '>', '•']):
                            continue
                        yield text_buffer
                        text_buffer = ""
                    else:
                        yield text_buffer
                        text_buffer = ""

            if text_buffer:
                text_buffer = re.sub(r'(?m)^\s*[\-\*•>]\s*$', '', text_buffer)
                yield text_buffer

            # Se eliminó la generación del footer "Fuentes Consultadas" basado en metadatos de Google.
            
            return 

        except (ResourceExhausted, ServiceUnavailable, Aborted, InternalServerError) as e:
            log.warning(f"Vertex AI Error ({type(e).__name__}): {e} - Reintentando...")
            if attempt < MAX_RETRIES:
                wait_time = (BASE_DELAY * (2 ** attempt)) + random.uniform(0, 1)
                await asyncio.sleep(wait_time)
                continue
            else:
                log.error("Agotados reintentos Vertex AI.")
                yield f"Error: El sistema está saturado. Intente de nuevo más tarde."
                return

        except Exception as e:
            log.error(f"Error Gemini: {e}", exc_info=True)
            yield "Error inesperado en la generación."
            return
