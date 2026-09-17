FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080
EXPOSE 8080

# Shell form (no brackets) so ${PORT} actually expands — Cloud Run injects
# PORT at runtime, and exec-form CMD ["uvicorn", ...] would not substitute it.
CMD uvicorn webapp:app --host 0.0.0.0 --port ${PORT:-8080}
