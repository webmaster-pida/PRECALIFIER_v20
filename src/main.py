# src/main.py

import json
import asyncio
from datetime import datetime, timezone
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

# --- LÍMITES DIARIOS (VARIABLES DE ENTORNO ESPECÍFICAS PRECALIFICADOR) ---
import os
LIMIT_DEMO = int(os.getenv("LIMIT_DEMO_PRE_DAILY", "0"))
LIMIT_BASICO = int(os.getenv("LIMIT_BASICO_PRE_DAILY", "0"))
LIMIT_AVANZADO = int(os.getenv("LIMIT_AVANZADO_PRE_DAILY", "20"))
LIMIT_PREMIUM = int(os.getenv("LIMIT_PREMIUM_PRE_DAILY", "100"))

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
async def verify_active_subscription(current_user: Dict[str, Any]) -> str:
    """
    Verifica la suscripción y retorna el rol. 
    Si es VIP, retorna 'vip' para acceso ilimitado.
    """
    user_id = current_user.get("uid")
    user_email = current_user.get("email", "").strip().lower()

    # 1. Comprobar si es VIP (Lista blanca)
    raw_domains = os.getenv("ADMIN_DOMAINS", '[]')
    raw_emails = os.getenv("ADMIN_EMAILS", '[]')
    try:
        import json
        allowed_domains = [str(d).strip().lower() for d in json.loads(raw_domains)]
        allowed_emails = [str(e).strip().lower() for e in json.loads(raw_emails)]
    except:
        allowed_domains, allowed_emails = [], []

    email_domain = user_email.split("@")[-1] if "@" in user_email else ""
    if (email_domain in allowed_domains) or (user_email in allowed_emails):
        return "vip" # Bypass total de límites

    # 2. Verificar suscripción en Firestore (Stripe)
    try:
        subscriptions_ref = db.collection("customers").document(user_id).collection("subscriptions")
        query = subscriptions_ref.where("status", "in", ["active", "trialing"]).limit(1)
        results = [doc async for doc in query.stream()]

        if not results:
            return "demo" # Sin plan es DEMO

        # Retornar el rol del claim 'stripeRole'
        return current_user.get("stripeRole", "basico").lower()

    except Exception:
        return "demo"

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
    # 1. Obtener rol y determinar su límite diario
    user_role = await verify_active_subscription(current_user)

    # 2. BYPASS TOTAL PARA VIP (No cuentan para la cuota ni tienen límites)
    if user_role == "vip":
        pass 
    
    else:
        # 3. Determinar límites para usuarios normales
        limits_map = {
            "demo": LIMIT_DEMO,
            "basico": LIMIT_BASICO,
            "avanzado": LIMIT_AVANZADO,
            "premium": LIMIT_PREMIUM
        }
        daily_limit = limits_map.get(user_role, 0)

        # 4. Bloqueo si el límite es 0 (Casos DEMO y BASICO según tus variables)
        if daily_limit == 0:
            raise HTTPException(
                status_code=403, 
                detail=f"Tu plan actual ({user_role.upper()}) no permite realizar precalificaciones. Por favor, adquiere un plan superior."
            )

        # 5. Validar cuota diaria (Patrón CHATv20: Documento único por día)
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        usage_ref = db.collection("users").document(current_user["uid"]).collection("usage_precalificador").document(today_str)

        usage_doc = await usage_ref.get()
        count_today = usage_doc.to_dict().get("count", 0) if usage_doc.exists else 0

        if count_today >= daily_limit:
            raise HTTPException(
                status_code=429, 
                detail=f"Has alcanzado el límite de {daily_limit} precalificaciones diarias de tu plan {user_role.upper()}."
            )

        # 6. Incrementar uso ANTES de procesar
        await usage_ref.set({"count": count_today + 1}, merge=True)

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
