# Works on any container host (Railway, Fly.io, Google Cloud Run, Azure...).
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8000
EXPOSE 8000
CMD ["python", "-m", "ghostbus"]
