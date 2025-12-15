# Usa una imagen base de Python oficial y ligera.
FROM python:3.12-slim

# Establece el directorio de trabajo dentro del contenedor.
WORKDIR /app

# Actualiza pip a la última versión
RUN pip install --upgrade pip

# Copia solo el archivo de requerimientos primero para aprovechar el caché de capas de Docker.
COPY requirements.txt .

# Instala las dependencias.
RUN pip install --no-cache-dir -r requirements.txt

# Ahora, copia todo el código de tu aplicación.
COPY ./src ./src

# Expone el puerto en el que Uvicorn se ejecutará. Cloud Run espera el puerto 8080.
EXPOSE 8080

# El comando para iniciar la aplicación.
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
