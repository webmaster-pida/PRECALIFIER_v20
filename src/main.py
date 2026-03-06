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
    
    # 🛡️ PROTECCIÓN: Exigir email_verified
    if email_verified and ((email_domain in admin_domains) or (user_email in admin_emails)):
        log.info(f"Acceso VIP detectado para: {user_email}")
        return 'vip'

    # 2. VERIFICACIÓN FIRESTORE (Documento de Cliente)
    # Leemos customers/{uid} igual que el Analizador y el Chat
    try:
        cust_doc = await db.collection('customers').document(user_id).get()
        if cust_doc.exists:
            data = cust_doc.to_dict()
            status = data.get('status')
            # Aceptamos active o trialing
            if status in ['active', 'trialing']:
                plan = data.get('plan', 'basico')
                # Normalizamos nombres de plan y Trial
                if data.get('has_trial'): return 'basico'
                return plan.lower() if plan else 'basico'
    except Exception as e:
        log.error(f"Error consultando plan en DB: {e}")
        
    return 'none' # Sin acceso por defecto

async def check_precalifier_limits(user_id: str, plan_key: str):
    """
    Verifica si el usuario puede realizar precalificaciones hoy.
    """
    if plan_key == 'none':
        raise HTTPException(status_code=403, detail="No tienes un plan activo para usar el Precalificador.")

    limit_daily = PRECAL_LIMITS.get(plan_key, 0)
    
    # Si el límite es 0 (ej: Plan Básico según tu config), bloqueamos.
    if limit_daily == 0:
         raise HTTPException(
            status_code=403, 
            detail=f"Tu plan {plan_key.capitalize()} no incluye acceso al Precalificador."
        )

    if limit_daily == -1: return # VIP Ilimitado

    today = get_date_utc_minus_6()
    stats_ref = db.collection('users').document(user_id).collection('usage_stats').document(today)
    doc = await stats_ref.get()
    
    current_count = 0
    if doc.exists:
        current_count = doc.to_dict().get('precal_count', 0)
        
    if current_count >= limit_daily:
        raise HTTPException(
            status_code=429,
            detail=f"Límite diario alcanzado para el plan {plan_key}"
        )

async def increment_precalifier_count(user_id: str):
    """Incrementa el contador de uso de forma atómica"""
    today = get_date_utc_minus_6()
    stats_ref = db.collection('users').document(user_id).collection('usage_stats').document(today)
    await stats_ref.set({
        'precal_count': firestore.Increment(1),
        'last_updated': SERVER_TIMESTAMP
    }, merge=True)

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

        # Si hay texto generado, GUARDAMOS e INCREMENTAMOS
        if full_response_text:
            # 1. Guardar Historial (Directo en DB, sin usar módulo externo, espejo al Analizador)
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
            
            # 2. Incrementar Uso (Solo tras éxito)
            await increment_precalifier_count(user_id)
        
        yield create_sse_event({'event': 'done'})

    except Exception as e:
        log.error(f"Error crítico en precalificador: {e}", exc_info=True)
        yield f"data: {json.dumps({'error': 'Error interno al analizar el caso.'})}\n\n"

# --- ENDPOINTS ---

@app.get("/status")
def read_status():
    return {"status": "ok", "service": "Precalificador v2.0 (Unified Logic)"}

@app.post("/analyze", tags=["Analysis"])
async def analyze_facts(
    analysis_request: AnalysisRequest, 
    request: Request,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Endpoint principal. Recibe los hechos y devuelve un stream con el análisis jurídico.
    """
    # 1. Obtener Plan Unificado
    user_id = current_user['uid']
    plan = await get_user_plan_unified(current_user)

    # 2. Verificar Límites (Lanza 403 o 429 si no cumple)
    await check_precalifier_limits(user_id, plan)

    headers = { 
        "Content-Type": "text/event-stream", 
        "Cache-Control": "no-cache", 
        "Connection": "keep-alive", 
        "X-Accel-Buffering": "no" 
    }
    
    # 3. Iniciar Stream (pasamos el plan para registro histórico)
    return StreamingResponse(
        stream_analysis_generator(analysis_request, current_user, plan), 
        headers=headers
    )
