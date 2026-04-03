# src/modules/perplexity_client.py
import httpx
from src.config import settings, log

async def get_perplexity_research(query: str) -> str:
    url = "https://api.perplexity.ai/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.PERPLEXITY_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": settings.PERPLEXITY_MODEL,
        "messages": [
            {
                "role": "system", 
                "content": """Eres un investigador legal y analista de élite del IIRESODH. Tu única tarea es buscar en la web información reciente, precisa y detallada sobre la consulta.
Reglas estrictas:
1. NO seas conversacional ni saludes.
2. IGNORA EL FORMATO: Si el usuario pide diseñar una tabla, carta, o cronograma, NO busques herramientas de diseño, software (Canva, Asana, Word, Excel) ni plantillas. Busca ÚNICAMENTE el contexto legal, fáctico, diplomático o de derechos humanos necesario para llenar ese formato.
3. Proporciona un resumen exhaustivo de los hechos, noticias, o jurisprudencia.
4. SIEMPRE incluye las URLs completas de las fuentes reales.
5. Solo utiliza fuentes serias, institucionales, académicas o periodísticas."""
            },
            {"role": "user", "content": query}
        ]
    }
    
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            citations = data.get("citations", [])
            
            # Formateamos las citas para que el regex de main.py las atrape fácilmente
            links_text = "\n\nFUENTES DE INTERNET:\n" + "\n".join(citations)
            return f"{content}\n{links_text}"
            
    except Exception as e:
        log.error(f"Error consultando Perplexity: {e}")
        return "No se pudo obtener información de internet en este momento."
