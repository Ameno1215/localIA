FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py ingest.py ./
EXPOSE 9901
CMD ["python", "server.py"]
