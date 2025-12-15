# src/modules/firestore_client.py

from google.cloud import firestore
from src.config import settings, log
from src.models.chat_models import ChatMessage
from typing import List, Dict, Any
import datetime

# Inicializa el cliente de Firestore de forma asíncrona
db = firestore.AsyncClient(project=settings.GOOGLE_CLOUD_PROJECT)

async def get_conversations(user_id: str) -> List[Dict[str, Any]]:
    """Obtiene la lista de conversaciones (ID, título, fecha) para un usuario, ordenadas por fecha."""
    try:
        conversations_ref = db.collection('users').document(user_id).collection('conversations').order_by("created_at", direction=firestore.Query.DESCENDING)
        convos = []
        async for doc in conversations_ref.stream():
            data = doc.to_dict()
            convos.append({"id": doc.id, "title": data.get("title", "Sin Título"), "userId": user_id})
        return convos
    except Exception as e:
        log.error(f"Error al obtener conversaciones para el usuario {user_id}: {e}")
        return []

async def get_conversation_messages(user_id: str, convo_id: str) -> List[ChatMessage]:
    """Obtiene todos los mensajes de una conversación específica, ordenados por tiempo."""
    try:
        messages_ref = db.collection('users').document(user_id).collection('conversations').document(convo_id).collection('messages').order_by('timestamp')
        messages = []
        async for msg_doc in messages_ref.stream():
            data = msg_doc.to_dict()
            # Aseguramos que el contenido sea un string
            data['content'] = str(data.get('content', ''))
            messages.append(ChatMessage(**data))
        return messages
    except Exception as e:
        log.error(f"Error al obtener mensajes para la convo {convo_id} del usuario {user_id}: {e}")
        return []

async def add_message_to_conversation(user_id: str, convo_id: str, message: ChatMessage):
    """Añade un nuevo mensaje a una conversación, incluyendo un timestamp del servidor."""
    try:
        message_data = message.model_dump()
        message_data["timestamp"] = firestore.SERVER_TIMESTAMP
        await db.collection('users').document(user_id).collection('conversations').document(convo_id).collection('messages').add(message_data)
    except Exception as e:
        log.error(f"Error al añadir mensaje a la convo {convo_id} del usuario {user_id}: {e}")

async def create_new_conversation(user_id: str, title: str) -> Dict[str, Any]:
    """Crea una nueva conversación y devuelve su ID y título."""
    try:
        doc_ref = db.collection('users').document(user_id).collection('conversations').document()
        await doc_ref.set({
            "title": title,
            "created_at": firestore.SERVER_TIMESTAMP
        })
        return {"id": doc_ref.id, "title": title}
    except Exception as e:
        log.error(f"Error al crear nueva conversación para el usuario {user_id}: {e}")
        return {}

async def delete_conversation(user_id: str, convo_id: str):
    """Elimina una conversación y todos sus mensajes de forma recursiva."""
    try:
        convo_ref = db.collection('users').document(user_id).collection('conversations').document(convo_id)
        # Firestore no tiene un 'delete recursivo' nativo en el SDK de servidor, 
        # así que eliminamos los mensajes primero.
        messages_ref = convo_ref.collection('messages')
        async for msg_doc in messages_ref.stream():
            await msg_doc.reference.delete()
        
        await convo_ref.delete()
        log.info(f"Conversación {convo_id} del usuario {user_id} eliminada correctamente.")
    except Exception as e:
        log.error(f"Error al eliminar la conversación {convo_id} del usuario {user_id}: {e}")

async def update_conversation_title(user_id: str, convo_id: str, new_title: str):
    """Actualiza el título de una conversación específica."""
    try:
        convo_ref = db.collection('users').document(user_id).collection('conversations').document(convo_id)
        await convo_ref.update({"title": new_title})
        log.info(f"Título de la conversación {convo_id} actualizado a '{new_title}'.")
    except Exception as e:
        log.error(f"Error al actualizar el título de la convo {convo_id}: {e}")
