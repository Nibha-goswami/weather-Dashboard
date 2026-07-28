FROM python:3.11-slim
WORKDIR /APP
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p database
EXPOSE 10000
CMD["gunicorn","--bind","0.0.0.0:10000","app:app"]
