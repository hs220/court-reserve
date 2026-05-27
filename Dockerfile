FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV TZ=America/Los_Angeles

CMD ["python", "main.py", "book-next"]
