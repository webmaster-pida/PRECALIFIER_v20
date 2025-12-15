# src/models/schemas.py
from pydantic import BaseModel, Field

class AnalysisRequest(BaseModel):
    title: str = Field(..., description="Un título corto para el caso")
    facts: str = Field(..., description="El relato de los hechos ocurrido")
    country_code: str | None = Field(None, description="Contexto geográfico (ej: 'SV', 'MX')")

# Se puede reutilizar la estructura básica si se quiere guardar el historial, 
# pero para este servicio es un análisis 'one-shot' (una sola vez).
