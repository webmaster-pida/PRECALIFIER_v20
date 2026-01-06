# src/main.py

import json
import asyncio
from datetime import datetime
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import Dict, Any
from google.cloud import firestore

from src.config import settings, log
from src.core.security import get_current_user
from src.core.prompts import PRECALIFIER_SYSTEM_PROMPT
from src.models.schemas import AnalysisRequest
from src.modules import gemini_client, firestore_client

# Cliente Firestore para verificación de suscripción
db = firestore.AsyncClient(project=settings.GOOGLE_CLOUD_PROJECT)

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

# --- VERIFICACIÓN DE SUSCRIPCIÓN ---
async def verify_active_subscription(current_user: Dict[str, Any]):
    """
    Verifica si el usuario es VIP o tiene una suscripción activa en Stripe.
    """
    user_id = current_user.get("uid")
    user_email = current_user.get("email", "").strip().lower()
    
    # 1. Comprobar si es VIP (Listas blancas en settings)
    allowed_domains = settings.ADMIN_DOMAINS
    allowed_emails = settings.ADMIN_EMAILS
    email_domain = user_email.split("@")[-1] if "@" in user_email else ""

    if (email_domain in allowed_domains) or (user_email in allowed_emails):
        log.info(f"Acceso VIP concedido en Precalificador: {user_email}")
        return

    # 2. Comprobar suscripción en Firestore (Stripe)
    try:
        subscriptions_ref = db.collection("customers").document(user_id).collection("subscriptions")
        query = subscriptions_ref.where("status", "in", ["active", "trialing"]).limit(1)
        results = [doc async for doc in query.stream()]
        
        if not results:
            raise HTTPException(status_code=403, detail="No tienes una suscripción activa.")
    except HTTPException as he:
        raise he
    except Exception as e:
        log.error(f"Error verificando suscripción en Precalificador: {e}")
        raise HTTPException(status_code=500, detail="Error interno verificando suscripción.")

# --- GENERADOR STREAMING PARA ANÁLISIS ---
async def stream_analysis_generator(request_data: AnalysisRequest, user: Dict[str, Any]):
    """
    Genera el análisis jurídico en streaming usando Gemini y guarda el resultado al finalizar.
    """
    def create_sse_event(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    try:
        yield create_sse_event({"event": "status", "message": "Analizando relato de hechos..."})
        await asyncio.sleep(0.5) 

        geo_context = f"Contexto Geográfico: {request_data.country_code}" if request_data.country_code else "Contexto Geográfico: Universal"
        
        final_prompt = f"""
        {geo_context}
        
        RELATO DE HECHOS PROPORCIONADO POR EL USUARIO:
        --------------------------------------------------
        {request_data.facts}
        --------------------------------------------------
        
        Realiza el análisis de precalificación solicitado.
        """
        
        full_response_text = ""
        
        async for chunk in gemini_client.generate_streaming_response(
            system_prompt=PRECALIFIER_SYSTEM_PROMPT,
            prompt=final_prompt,
            history=[] 
        ):
            yield create_sse_event({'text': chunk})
            full_response_text += chunk

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
    # Verificación obligatoria de suscripción o VIP
    await verify_active_subscription(current_user)

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
