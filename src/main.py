# src/main.py

import json
import asyncio
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import Dict, Any, Optional
from google.cloud import firestore
from google.cloud.firestore import AsyncClient, SERVER_TIMESTAMP

from src.config import settings, log
from src.core.security import get_current_user
from src.core.prompts import PRECALIFIER_SYSTEM_PROMPT
from src.models.schemas import AnalysisRequest
from src.modules import gemini_client

# Inicializar Firestore
db = AsyncClient(project=settings.GOOGLE_CLOUD_PROJECT)

app = FastAPI(
    title="PIDA Pre-Calificador API",
    description="Microservicio para análisis preliminar de violaciones de DDHH."
)

# --- CONFIGURACIÓN CORS ---
origins = settings.ALLOWED_ORIGINS
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=r"https://pida-ai-v20--.*\.web\.app$|https://.*\.app\.github\.dev$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- MAPAS DE LÍMITES ---
PRECAL_LIMITS = {
    "basico": settings.LIMIT_BASICO_PRE_DAILY,      # 0
    "avanzado": settings.LIMIT_AVANZADO_PRE_DAILY,  # 20
    "premium": settings.LIMIT_PREMIUM_PRE_DAILY,    # 100
    "vip": -1  # Ilimitado
}

# --- FUNCIONES DE CONTROL (LÓGICA UNIFICADA) ---

def get_date_utc_minus_6() -> str:
    """Devuelve la fecha actual ajustada a UTC-6"""
    utc_now = datetime.now(timezone.utc)
    cst_now = utc_now - timedelta(hours=6)
    return cst_now.strftime('%Y-%m-%d')

async def get_user_plan_unified(current_user: Dict[str, Any]) -> str:
    """
    Determina el plan del usuario unificando lógica VIP y DB.
    Retorna: 'vip', 'basico', 'avanzado', 'premium' o 'none'.
    """
    user_id = current_user.get('uid')
    user_email = current_user.get('email', '').strip().lower()
    email_verified = current_user.get('email_verified', False)
    
    # 1. VERIFICACIÓN VIP (Variables de Entorno)
    try:
        raw_domains = settings.ADMIN_DOMAINS
        raw_emails = settings.ADMIN_EMAILS
        # Aseguramos que sean listas
        admin_domains = raw_domains if isinstance(raw_domains, list) else []
        admin_emails = raw_emails if isinstance(raw_emails, list) else []
    except:
        admin_domains, admin_emails = [], []

    email_domain = user_email.split("@")[-1] if "@" in user_email else ""
    
    # 🛡️ CAMBIO AQUÍ: Se eliminó email_verified and
    if (email_domain in admin_domains) or (user_email in admin_emails):
        log.info(f"Acceso VIP detectado para: {user_email}")
        return 'vip'

    # 2. VERIFICACIÓN FIRESTORE (Documento de Cliente)
    try:
        cust_doc = await db.collection('customers').document(user_id).get()
        if cust_doc.exists:
            data = cust_doc.to_dict()
            status = data.get('status')
            if status in ['active', 'trialing']:
                plan = data.get('plan', 'basico')
                if data.get('has_trial'): return 'basico'
                return plan.lower() if plan else 'basico'
    except Exception as e:
        log.error(f"Error consultando plan en DB: {e}")
        
    return 'none' # Sin acceso por defecto

# 🛡️ NUEVAS FUNCIONES DE CONSUMO ATÓMICO Y REEMBOLSO
async def consume_precal_credit(user_id: str, plan_key: str):
    limit_daily = PRECAL_LIMITS.get(plan_key, 0)
    if limit_daily == -1: return # VIP Ilimitado

    today = get_date_utc_minus_6()
    stats_ref = db.collection('users').document(user_id).collection('usage_stats').document(today)
    
    @firestore.async_transactional
    async def check_and_increment(transaction, ref):
        snapshot = await ref.get(transaction=transaction)
        current_count = snapshot.get('precal_count') if snapshot.exists else 0
        
        if current_count >= limit_daily:
            raise HTTPException(status_code=429, detail=f"Límite diario alcanzado para el plan {plan_key}")
        
        transaction.set(ref, {
            'precal_count': current_count + 1,
            'last_updated': firestore.SERVER_TIMESTAMP
        }, merge=True)

    transaction = db.transaction()
    await check_and_increment(transaction, stats_ref)

async def refund_precal_credit(user_id: str):
    today = get_date_utc_minus_6()
    stats_ref = db.collection('users').document(user_id).collection('usage_stats').document(today)
    
    @firestore.async_transactional
    async def check_and_decrement(transaction, ref):
        snapshot = await ref.get(transaction=transaction)
        if snapshot.exists:
            current_count = snapshot.get('precal_count', 0)
            if current_count > 0:
                transaction.update(ref, {
                    'precal_count': current_count - 1,
                    'last_updated': firestore.SERVER_TIMESTAMP
                })
    try:
        transaction = db.transaction()
        await check_and_decrement(transaction, stats_ref)
    except Exception as e:
        log.error(f"Error procesando reembolso de precalificador para {user_id}: {e}")

# --- GENERADOR STREAMING PARA ANÁLISIS ---
async def stream_analysis_generator(request_data: AnalysisRequest, user: Dict[str, Any], plan: str):
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
        
        # Llamada al cliente de Gemini
        async for chunk in gemini_client.generate_streaming_response(
            system_prompt=PRECALIFIER_SYSTEM_PROMPT,
            prompt=final_prompt,
            history=[] 
        ):
            yield create_sse_event({'text': chunk})
            full_response_text += chunk

        # Si hay texto generado, GUARDAMOS (El incremento ya se hizo al principio)
        if full_response_text:
            user_id = user['uid']
            title_doc = request_data.title or "Sin título"
            
            await db.collection('users').document(user_id).collection('prequalifications').add({
                "title": title_doc,
                "facts": request_data.facts,
                "country_code": request_data.country_code,
                "analysis": full_response_text,
                "created_at": SERVER_TIMESTAMP,
                "plan_at_time": plan
            })
        
        yield create_sse_event({'event': 'done'})

    except Exception as e:
        log.error(f"Error crítico en precalificador: {e}", exc_info=True)
        yield f"data: {json.dumps({'error': 'Error interno al analizar el caso.'})}\n\n"

# --- ENDPOINTS ---

@app.get("/status")
def read_status():
    return {"status": "ok", "service": "Precalificador v2.0 (Security Patched)"}

@app.post("/analyze", tags=["Analysis"])
async def analyze_facts(
    analysis_request: AnalysisRequest, 
    request: Request,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    # 1. Obtener Plan Unificado
    user_id = current_user['uid']
    plan = await get_user_plan_unified(current_user)

    if plan == 'none':
        raise HTTPException(status_code=403, detail="No tienes un plan activo para usar el Precalificador.")

    limit_daily = PRECAL_LIMITS.get(plan, 0)
    if limit_daily == 0:
        raise HTTPException(status_code=403, detail=f"Tu plan {plan.capitalize()} no incluye acceso al Precalificador.")

    # 🛡️ 2. Consumo Atómico de Crédito
    await consume_precal_credit(user_id, plan)

    # 🛡️ 3. Generador Envolvente con Protección de Reembolso
    async def counted_stream_generator():
        has_error = False
        tokens_sent = False 
        
        try:
            async for chunk in stream_analysis_generator(analysis_request, current_user, plan):
                if '"error":' in chunk:
                    has_error = True
                if '"text":' in chunk and not has_error:
                    tokens_sent = True 
                yield chunk
        finally:
            if has_error or not tokens_sent:
                asyncio.create_task(refund_precal_credit(user_id))

    headers = { 
        "Content-Type": "text/event-stream", 
        "Cache-Control": "no-cache", 
        "Connection": "keep-alive", 
        "X-Accel-Buffering": "no" 
    }
    
    return StreamingResponse(counted_stream_generator(), headers=headers)
