FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir requests

COPY bridge.py /app/bridge.py

CMD ["python", "-u", "bridge.py"]
