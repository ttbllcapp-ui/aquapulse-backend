# AquaPulse Backend

FastAPI + MongoDB backend for the AquaPulse iOS app.

## Quick Start (Render)

1. Push this folder to a GitHub repo (`aquapulse-backend`)
2. In Render: New Web Service → connect repo
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn server:app --host 0.0.0.0 --port $PORT`
5. Add the env vars listed in `.env.example`
6. Deploy → done

See BACKEND-DEPLOY-RENDER.md for full step-by-step.

## Endpoints

- `GET  /api/` — health check
- `POST /api/auth/google` — Google sign-in
- `POST /api/auth/apple` — Apple sign-in
- `DELETE /api/auth/me` — account deletion (Apple required)
- `POST /api/chat` — AquaCoach AI (gpt-4o + user context)
- `GET  /api/chat/history` — chat history
- `DELETE /api/chat/history` — clear chats
- `POST /api/family/create` — create family group
- `POST /api/family/join` — join with 6-char code
- `GET  /api/family/me` — current family + leaderboard
- `POST /api/family/progress` — sync today's hydration
- `POST /api/family/leave` — leave family

## Contact

info@ttbinternationalllc.com
