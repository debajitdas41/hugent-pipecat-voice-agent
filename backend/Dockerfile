FROM python:3.12-slim

# 1️⃣ Environment
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 2️⃣ System deps
RUN apt-get update && apt-get install -y \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 3️⃣ Workdir
WORKDIR /app

# 4️⃣ Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5️⃣ Copy source
COPY . .

# 6️⃣ Default command (overridden by compose)
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
