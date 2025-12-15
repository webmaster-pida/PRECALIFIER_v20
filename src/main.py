# src/main.py

import json
import asyncio
from datetime import datetime
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import Dict, Any

from src.config import settings, log
from src.core.security import get_current_user
from src.core.prompts import PRECALIFIER_SYSTEM_PROMPT
from src.models.schemas import AnalysisRequest
from src.modules import gemini_client, firestore_client

# Inicializar clientes (Asumiendo que vertexai.init ya se ejecuta al importar gemini_client o similar)

app = FastAPI(
    title="PIDA Pre-Calificador API",
    description="Microservicio para análisis preliminar de violaciones de DDHH."
)

# --- CONFIGURACIÓN CORS ---
origins = settings.ALLOWED_ORIGINS
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=r"https://pida-ai-v20--.*\.web\.app$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- GENERADOR STREAMING PARA ANÁLISIS ---
async def stream_analysis_generator(request_data: AnalysisRequest, user: Dict[str, Any]):
    """
    Genera el análisis jurídico en streaming usando Gemini y guarda el resultado al finalizar.
    """
    def create_sse_event(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    try:
        yield create_sse_event({"event": "status", "message": "Analizando relato de hechos..."})
        await asyncio.sleep(0.5) # Pequeña pausa para UX

        # Preparar el prompt combinado
        geo_context = f"Contexto Geográfico: {request_data.country_code}" if request_data.country_code else "Contexto Geográfico: Universal"
        
        final_prompt = f"""
        {geo_context}
        
        RELATO DE HECHOS PROPORCIONADO POR EL USUARIO:
        --------------------------------------------------
        {request_data.facts}
        --------------------------------------------------
        
        Realiza el análisis de precalificación solicitado.
        """

        # Usamos una lista de historial vacía para encajar con gemini_client
        # gemini_client.prepare_history_for_vertex espera objetos ChatMessage.
        # Al pasar una lista vacía [], indicamos que es una sesión nueva sin memoria previa.
        
        full_response_text = ""
        
        # Llamamos al cliente de Gemini
        async for chunk in gemini_client.generate_streaming_response(
            system_prompt=PRECALIFIER_SYSTEM_PROMPT,
            prompt=final_prompt,
            history=[] # No hay historial previo, es un análisis nuevo
        ):
            yield create_sse_event({'text': chunk})
            full_response_text += chunk

        # Guardar el resultado en Firestore bajo la colección "prequalifications"
        # Usamos asyncio.create_task para que se ejecute en segundo plano y no bloquee el yield final
        if full_response_text:
            asyncio.create_task(firestore_client.save_prequalification(
                user_id=user['uid'],
                title=request_data.title,
                facts=request_data.facts,
                analysis_result=full_response_text,
                country_code=request_data.country_code
            ))
        
        yield create_sse_event({'event': 'done'})

    except Exception as e:
        log.error(f"Error crítico en precalificador: {e}", exc_info=True)
        yield f"data: {json.dumps({'error': 'Error interno al analizar el caso.'})}\n\n"

# --- ENDPOINTS ---

@app.get("/status")
def read_status():
    return {"status": "ok", "service": "Precalificador v2.0"}

@app.post("/analyze", tags=["Analysis"])
async def analyze_facts(
    analysis_request: AnalysisRequest, 
    request: Request,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Endpoint principal. Recibe los hechos y devuelve un stream con el análisis jurídico.
    """
    # Aquí podrías verificar suscripción si es necesario (ej: await verify_active_subscription(current_user))

    headers = { 
        "Content-Type": "text/event-stream", 
        "Cache-Control": "no-cache", 
        "Connection": "keep-alive", 
        "X-Accel-Buffering": "no" 
    }
    
    return StreamingResponse(
        stream_analysis_generator(analysis_request, current_user), 
        headers=headers
    )
