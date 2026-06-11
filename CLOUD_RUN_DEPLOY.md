# SyncSentry Cloud Run deployment

This project is an MCP server. Deploy it to the existing Google Cloud project `mcp-workspace-492705` (display name SyncSentry) as a Cloud Run service named `syncsentry-agent`.

Required secret-backed environment variables:

- `CLOD_API_KEY` -> Secret Manager secret `gemini_api_key` currently holds the Gemini key placeholder/name used by the setup flow. If you use CLōD instead, create a `clod_api_key` secret and map that instead.
- `GREPTILE_API_KEY` -> create/map if Greptile code search is required.
- `GREPTILE_GITHUB_TOKEN` -> create/map if Greptile code search is required.
- `ALLSCALE_API_KEY` and `ALLSCALE_API_SECRET` -> existing Fivetran secret placeholders are not compatible with this codebase; create AllScale secrets if billing is required.

Minimal deploy command after secret values are real:

```bash
gcloud run deploy syncsentry-agent \
  --project=mcp-workspace-492705 \
  --source=. \
  --region=us-central1 \
  --allow-unauthenticated \
  --set-env-vars=MCP_TRANSPORT=sse,SSE_HOST=0.0.0.0,LOG_LEVEL=info,BGA_ENABLED=false,DATABASE_URL=sqlite:////tmp/syncsentry.db \
  --set-secrets=CLOD_API_KEY=gemini_api_key:latest
```

Use `--no-allow-unauthenticated` instead if this MCP endpoint should remain private.
