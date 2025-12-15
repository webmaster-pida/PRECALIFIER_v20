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

# --- EL PORTERO ---
async def get_current_user(request: Request):
    """
    Dependencia para verificar el token de Firebase ID.
    Usa listas de acceso pre-procesadas desde settings.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Falta la cabecera de autenticación o tiene un formato incorrecto.",
        )
    
    token = auth_header.split("Bearer ")[1]
    
    try:
        # 1. Verificar firma
        decoded_token = auth.verify_id_token(token)
        
        # 2. Obtener datos
        email = decoded_token.get("email", "").lower()
        domain = email.split("@")[1] if "@" in email else ""
        
        # 3. Listas de acceso (YA SON LISTAS LIMPIAS GRACIAS A CONFIG.PY)
        allowed_domains = settings.ADMIN_DOMAINS
        allowed_emails = settings.ADMIN_EMAILS

        # 4. APLICAR LÓGICA DE SEGURIDAD
        has_restrictions = bool(allowed_domains or allowed_emails)
        
        if has_restrictions:
            is_domain_authorized = domain in allowed_domains
            is_email_authorized = email in allowed_emails
            
            if not (is_domain_authorized or is_email_authorized):
                log.warning(f"⛔ ACCESO DENEGADO: {email}. Dominio '{domain}' no autorizado.")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No tienes autorización para acceder a esta plataforma."
                )

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
    except HTTPException as he:
        raise he
    except Exception as e:
        log.error(f"Error inesperado en autenticación: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error interno de seguridad.",
        )
