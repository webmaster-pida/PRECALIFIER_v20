# src/core/security.py

import json
import firebase_admin
from firebase_admin import credentials, auth
from fastapi import Request, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from src.config import settings, log

# --- Inicialización de Firebase Admin ---
try:
    cred = credentials.ApplicationDefault()
    firebase_admin.initialize_app(cred)
except ValueError:
    pass

# Esquema para documentación
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# --- EL PORTERO (SÓLO AUTENTICACIÓN) ---
async def get_current_user(request: Request):
    """
    Dependencia para verificar el token de Firebase ID.
    Ya no bloquea por dominio/email aquí para permitir el acceso a 
    usuarios que no son VIP pero tienen suscripción de Stripe.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Falta la cabecera de autenticación o tiene un formato incorrecto.",
        )
    
    token = auth_header.split("Bearer ")[1]
    
    try:
        # 1. Verificar firma y validez del token
        decoded_token = auth.verify_id_token(token)
        
        # 2. Retornamos el token decodificado
        return decoded_token

    except auth.ExpiredIdTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="El token de sesión ha expirado.",
        )
    except auth.InvalidIdTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"El token es inválido: {e}",
        )
    except Exception as e:
        log.error(f"Error inesperado en autenticación: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error interno de seguridad.",
        )
