# src/modules/gemini_client.py

import asyncio 
import re 
import random 
from typing import List, AsyncGenerator, Set

# 👇 IMPORTACIONES DEL NUEVO SDK DE GENAI
from google import genai
from google.genai import types

from src.config import settings, log
from src.models.chat_models import ChatMessage

# --- INICIALIZACIÓN ---
try:
    # Inicializa el cliente de GenAI con vertexai=True para mantener el uso en Google Cloud
    client = genai.Client(
        vertexai=True,
        project=settings.GOOGLE_CLOUD_PROJECT, 
        location=settings.GOOGLE_CLOUD_LOCATION
    )

    # La configuración de seguridad ahora usa types.SafetySetting
    safety_settings = [
        types.SafetySetting(category='HARM_CATEGORY_HATE_SPEECH', threshold='BLOCK_NONE'),
        types.SafetySetting(category='HARM_CATEGORY_DANGEROUS_CONTENT', threshold='BLOCK_NONE'),
        types.SafetySetting(category='HARM_CATEGORY_SEXUALLY_EXPLICIT', threshold='BLOCK_NONE'),
        types.SafetySetting(category='HARM_CATEGORY_HARASSMENT', threshold='BLOCK_NONE'),
    ]

    log.info(f"Cliente GenAI inicializado y modelo '{settings.GEMINI_MODEL}' configurado.")

except Exception as e:
    log.critical(f"No se pudo inicializar GenAI: {e}", exc_info=True)
    client = None

# --- UTILS ---

def prepare_history_for_genai(history: List[ChatMessage]) -> List[types.Content]:
    """Mapea los mensajes antiguos al nuevo formato types.Content de genai"""
    genai_history = []
    for message in history:
        role = 'user' if message.role == 'user' else 'model'
        genai_history.append(
            types.Content(role=role, parts=[types.Part.from_text(text=message.content)])
        )
    return genai_history

async def generate_streaming_response(
    system_prompt: str, 
    prompt: str, 
    history: List[types.Content],
    trusted_urls: Set[str] = None
) -> AsyncGenerator[str, None]:
    
    if trusted_urls is None:
        trusted_urls = set()

    if not client:
        log.error("El cliente GenAI no está disponible.")
        yield "Error: El modelo de IA no está configurado correctamente."
        return

    # RETRY LOGIC
    MAX_RETRIES = 3
    BASE_DELAY = 2 

    # 👇 En el nuevo SDK, las instrucciones del sistema y los parámetros van en Config
    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        max_output_tokens=settings.MAX_OUTPUT_TOKENS,
        temperature=settings.TEMPERATURE,
        top_p=settings.TOP_P,
        safety_settings=safety_settings
    )

    # Preparamos el contenido (historial + prompt actual)
    contents = history.copy()
    if not contents:
        contents = prompt  # Si no hay historial, basta con enviar el string directo
    else:
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=prompt)]))

    for attempt in range(MAX_RETRIES + 1):
        try:
            # 👇 Nueva forma de llamar al modelo con transmisión asíncrona
            response_stream = await client.aio.models.generate_content_stream(
                model=settings.GEMINI_MODEL,
                contents=contents,
                config=config
            )

            text_buffer = "" 

            async for chunk in response_stream:
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
                    # ELIMINADA: text_buffer = re.sub(r'\*\*\s*$', '', text_buffer, flags=re.MULTILINE)

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
            
            return 

        except Exception as e:
            # Capturamos Exception genérico ya que las excepciones de api_core cambiaron.
            log.warning(f"GenAI Error ({type(e).__name__}): {e} - Reintentando...")
            if attempt < MAX_RETRIES:
                wait_time = (BASE_DELAY * (2 ** attempt)) + random.uniform(0, 1)
                await asyncio.sleep(wait_time)
                continue
            else:
                log.error("Agotados reintentos GenAI.")
                yield f"Error: El sistema está saturado. Intente de nuevo más tarde."
                return
