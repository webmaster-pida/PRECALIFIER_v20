# src/models/chat_models.py

from pydantic import BaseModel
from typing import Literal

class ChatMessage(BaseModel):
    """Representa un único mensaje en el historial de chat."""
    role: Literal["user", "model"]
    content: str
