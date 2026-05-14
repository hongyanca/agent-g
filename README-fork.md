Use `docker compose` to deploy local text embeddings inference

```
services:
  tei:
    image: ghcr.io/huggingface/text-embeddings-inference:cpu-1.9
    container_name: tei-bge-m3
    restart: unless-stopped
    command:
      - --model-id
      - BAAI/bge-m3
      - --dtype
      - float32
      - --pooling
      - cls
      - --hostname
      - 0.0.0.0
      - --port
      - "80"
      - --max-concurrent-requests
      - "8"
      - --max-batch-tokens
      - "8192"
      - --auto-truncate
      - "false"
      - --max-client-batch-size
      - "8"
      - --tokenization-workers
      - "24"
    volumes:
      - ./models:/data
    ports:
      - "7180:80"
    healthcheck:
      test: ["CMD-SHELL", "wget -q --spider http://127.0.0.1:80/health || exit 1"]
      interval: 30s
      timeout: 10s
      retries: 10
      start_period: 180s
```

`.env`

```
LLM_PROVIDER=openai
LLM_API_URL=http://192.168.0.35:5001/v1
LLM_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxx
LLM_MODEL_ID=local-llm

EMBEDDING_API_URL=https://api.siliconflow.cn/v1/embeddings
```

