FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir requests

COPY reaper.py /app/reaper.py

CMD ["python", "-u", "reaper.py"]
