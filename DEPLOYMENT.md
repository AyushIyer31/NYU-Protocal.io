# Deployment

## Recommended Free Setup

Use Render for both services:

- Backend: Render Python web service
- Frontend: Render static site

The repo includes `render.yaml`, so Render can create both services from the same Blueprint.

## Render Blueprint

1. Push this repo to GitHub.
2. In Render, create a new Blueprint and select the repo.
3. Render will create:
   - `protocolsnerd-backend`
   - `protocolsnerd-frontend`
4. When prompted for environment variables, set:
   - `ANTHROPIC_API_KEY` if you want Claude explanations and better query expansion.
   - `PROTOCOLS_IO_TOKEN` if you have a protocols.io token.

The frontend gets the backend URL from the backend service's `RENDER_EXTERNAL_URL` during the static build.

Backend settings used by `render.yaml`:

```bash
pip install -r requirements.txt
```

```bash
cd protocolsnerd-backend && uvicorn main:app --host 0.0.0.0 --port $PORT
```

## Netlify Frontend

If you prefer Netlify for the frontend:

1. Deploy the backend on Render first.
2. Create a Netlify site from this repo.
3. Set the Netlify environment variable:
   - `PROTOCOLSNERD_API_URL=https://your-backend.onrender.com`
4. Netlify will use `netlify.toml`:

```toml
[build]
  publish = "protocolsnerd-website"
  command = "python3 scripts/write_frontend_env.py"
```

## Local Development

Local behavior is unchanged:

```bash
python3 run.py
```

Open:

```text
http://localhost:5555/chat.html
```

The local frontend defaults to:

```text
http://localhost:8001
```

You can override it in the browser console for quick testing:

```js
localStorage.setItem("PROTOCOLSNERD_API_URL", "https://your-backend.onrender.com");
location.reload();
```
