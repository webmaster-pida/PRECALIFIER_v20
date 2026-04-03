# src/main.py

import json
import asyncio
import re
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

# 👇 NUEVAS IMPORTACIONES HÍBRIDAS
from src.modules import gemini_client, perplexity_client, rag_client

# Inicializar Firestore
db = AsyncClient(project=settings.GOOGLE_CLOUD_PROJECT)

app = FastAPI(
    title="PIDA Pre-Calificador API",
    description="Microservicio de análisis jurídico con Perplexity + RAG."
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
    "basico": settings.LIMIT_BASICO_PRE_DAILY,
    "avanzado": settings.LIMIT_AVANZADO_PRE_DAILY,
    "premium": settings.LIMIT_PREMIUM_PRE_DAILY,
    "vip": -1
}

# --- FUNCIONES DE CONTROL ---

def get_date_utc_minus_6() -> str:
    utc_now = datetime.now(timezone.utc)
    cst_now = utc_now - timedelta(hours=6)
    return cst_now.strftime('%Y-%m-%d')

async def get_user_plan_unified(current_user: Dict[str, Any]) -> str:
    user_id = current_user.get('uid')
    user_email = current_user.get('email', '').strip().lower()
    admin_domains = settings.ADMIN_DOMAINS
    admin_emails = settings.ADMIN_EMAILS
    email_domain = user_email.split("@")[-1] if "@" in user_email else ""
    
    if (email_domain in admin_domains) or (user_email in admin_emails):
        return 'vip'

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
    return 'none'

async def consume_precal_credit(user_id: str, plan_key: str):
    limit_daily = PRECAL_LIMITS.get(plan_key, 0)
    if limit_daily == -1: return 

    today = get_date_utc_minus_6()
    stats_ref = db.collection('users').document(user_id).collection('usage_stats').document(today)
    
    @firestore.async_transactional
    async def check_and_increment(transaction, ref):
        snapshot = await ref.get(transaction=transaction)
        data = snapshot.to_dict() if snapshot.exists else {}
        current_count = data.get('precal_count', 0)
        
        if current_count >= limit_daily:
            raise HTTPException(status_code=429, detail=f"Límite diario alcanzado.")
        
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
            data = snapshot.to_dict() or {}
            current_count = data.get('precal_count', 0)
            if current_count > 0:
                transaction.update(ref, {'precal_count': current_count - 1, 'last_updated': firestore.SERVER_TIMESTAMP})
    try:
        transaction = db.transaction()
        await check_and_decrement(transaction, stats_ref)
    except Exception as e:
        log.error(f"Error refund: {e}")

# --- GENERADOR STREAMING HÍBRIDO ---
async def stream_analysis_generator(request_data: AnalysisRequest, user: Dict[str, Any], plan: str):
    def create_sse_event(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    try:
        yield create_sse_event({"event": "status", "message": "Investigando bases legales y jurisprudencia..."})
        
        # Búsqueda optimizada para Precalificador
        search_query = f"Delitos y violaciones de DDHH en {request_data.country_code or 'Derecho Internacional'} para este caso: {request_data.facts[:300]}"
        
        # Ejecución paralela
        rag_task = rag_client.search_internal_documents(request_data.facts)
        perp_task = perplexity_client.get_perplexity_research(search_query)
        
        rag_context, web_context = await asyncio.gather(rag_task, perp_task)
        
        # Recolección de URLs para limpieza
        combined_context = f"{rag_context}\n{web_context}"
        trusted_urls_set = set(re.findall(r'https?://[^\s\)\],>]+', combined_context))
        
        yield create_sse_event({"event": "status", "message": "Sintetizando pre-análisis jurídico..."})

        geo_context = f"Contexto Geográfico: {request_data.country_code or 'Universal'}"
        
        final_prompt = f"""
{geo_context}

[CONTEXTO INTERNO DE JURISPRUDENCIA (RAG)]
{rag_context}

[INVESTIGACIÓN WEB RECIENTE (Perplexity)]
{web_context}

RELATO DE HECHOS DEL USUARIO:
{request_data.facts}

Analiza los hechos usando el contexto proporcionado.
"""
        
        full_response_text = ""
        
        async for chunk in gemini_client.generate_streaming_response(
            system_prompt=PRECALIFIER_SYSTEM_PROMPT,
            prompt=final_prompt,
            history=[],
            trusted_urls=trusted_urls_set
        ):
            yield create_sse_event({'text': chunk})
            full_response_text += chunk

        if full_response_text:
            user_id = user['uid']
            await db.collection('users').document(user_id).collection('prequalifications').add({
                "title": request_data.title or "Sin título",
                "facts": request_data.facts,
                "country_code": request_data.country_code,
                "analysis": full_response_text,
                "created_at": SERVER_TIMESTAMP,
                "plan_at_time": plan
            })
        
        yield create_sse_event({'event': 'done'})

    except Exception as e:
        log.error(f"Error crítico precalificador: {e}", exc_info=True)
        yield f"data: {json.dumps({'error': 'Error interno en el análisis.'})}\n\n"

# --- ENDPOINTS ---

@app.get("/status")
def read_status():
    return {"status": "ok", "service": "Precalificador v2.0 (Hybrid Engine)"}

@app.post("/analyze", tags=["Analysis"])
async def analyze_facts(
    analysis_request: AnalysisRequest, 
    request: Request,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    user_id = current_user['uid']
    plan = await get_user_plan_unified(current_user)

    if plan == 'none':
        raise HTTPException(status_code=403, detail="No tienes acceso activo.")

    await consume_precal_credit(user_id, plan)

    async def counted_stream_generator():
        has_error = False
        tokens_sent = False 
        try:
            async for chunk in stream_analysis_generator(analysis_request, current_user, plan):
                if '"error":' in chunk: has_error = True
                if '"text":' in chunk and not has_error: tokens_sent = True 
                yield chunk
        finally:
            if has_error or not tokens_sent:
                asyncio.create_task(refund_precal_credit(user_id))

    return StreamingResponse(counted_stream_generator(), headers={ 
        "Content-Type": "text/event-stream", 
        "Cache-Control": "no-cache", 
        "X-Accel-Buffering": "no" 
    })
