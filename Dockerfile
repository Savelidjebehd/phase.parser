FROM python:3.11-slim

WORKDIR /app

# Устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY parser.py .

# Папка для базы данных — монтируется как volume
RUN mkdir -p /app/data

# Запуск
CMD ["python", "parser.py"]
