# CASO Comply API Service

FastAPI service for PDF accessibility analysis and automated remediation.

## Local Development

```bash
./run.sh
# API available at http://localhost:8787
# Swagger docs at http://localhost:8787/docs
```

## Deploy to Render.com

### Option A: Docker Deploy (recommended)

1. Push the `api-service/` directory to its own GitHub repo (or a subdirectory with Render's root directory setting):

   ```bash
   # From api-service/
   git init
   git add .
   git commit -m "Initial commit"
   git remote add origin https://github.com/YOUR_ORG/caso-comply-api.git
   git push -u origin main
   ```

2. In Render Dashboard:
   - Click **New > Web Service**
   - Connect your GitHub repo
   - Render auto-detects the Dockerfile
   - Set **Port** to `8787`
   - Click **Create Web Service**

3. Or use the blueprint: click **New > Blueprint** and point to this repo. Render reads `render.yaml` automatically.

### Option B: Manual Docker Deploy

```bash
# Build and test locally
docker build -t caso-comply-api .
docker run -p 8787:8787 caso-comply-api

# Verify
curl http://localhost:8787/health
```

## After Deployment

1. Copy the Render service URL (e.g., `https://caso-comply-api.onrender.com`)
2. Set it in the Next.js app:
   - **Local dev:** Add to `.env.local`:
     ```
     NEXT_PUBLIC_CASO_API_URL=https://caso-comply-api.onrender.com
     ```
   - **Vercel:** Add the same env var in Project Settings > Environment Variables

## Endpoints

| Method | Path                     | Description                          |
|--------|--------------------------|--------------------------------------|
| GET    | `/`                      | Service info                         |
| GET    | `/health`                | Health check                         |
| POST   | `/api/analyze`           | Upload PDF for accessibility analysis|
| POST   | `/api/remediate`         | Upload PDF for auto-remediation      |
| GET    | `/api/download/{file_id}`| Download remediated PDF              |
