FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# outputs/ (traces + the sqlite understanding cache, HLD §9) is mounted as a
# volume by docker-compose.yml so both services -- and the host -- share the
# same cache and see each other's runs.
VOLUME ["/app/outputs"]
