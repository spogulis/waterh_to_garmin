FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY waterh_to_garmin.py app.py garmin_bootstrap.py ./

# Store the Garmin session on a mounted volume so it survives restarts.
ENV GARMIN_TOKENSTORE=/data \
    PORT=8000

EXPOSE 8000

CMD ["python", "app.py"]
