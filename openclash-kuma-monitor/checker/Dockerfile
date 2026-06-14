FROM python:3.12-alpine

WORKDIR /app

COPY checker/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY checker/app.py /app/app.py
COPY config /app/config

CMD ["python", "-u", "/app/app.py"]
