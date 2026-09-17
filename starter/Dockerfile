FROM public.ecr.aws/docker/library/python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv package manager
RUN pip install --no-cache-dir uv

# Copy project definition and source code
COPY pyproject.toml uv.lock* README.md ./
COPY . .

# Install dependencies into container
RUN uv sync --frozen || uv sync

EXPOSE 8080

CMD ["python", "main.py"]