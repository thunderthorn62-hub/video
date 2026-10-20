# VidNova — compact Render project

## Deploy
1. Create a new GitHub repository and upload `main.py`, `requirements.txt`, `render.yaml`, and this README.
2. In Render select **New → Blueprint**, connect the repository, and apply `render.yaml`.
3. Wait for the web service and PostgreSQL database to deploy. Test `/health`.
4. For manual setup, set `DATABASE_URL`, a long random `SECRET_KEY`, and optionally `MAX_FILE_MB=500`.

## Optional Google login
Email/password login works without Google. To enable Google login, set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`; add `https://YOUR-SERVICE.onrender.com/auth/google/callback` to authorized redirect URIs in Google Cloud OAuth settings.

## Limits and notes
- 10 successful downloads per account per day (UTC, PostgreSQL time); failed jobs do not count.
- File limit defaults to 500 MB (`MAX_FILE_MB`).
- Temporary files are stored in `/tmp/vidnova`; redeploys/restarts can remove them. Active jobs are held in memory, so this is a starter rather than a production-scale queue.
- Availability depends on the source platform. No DRM or access-control bypass is provided. Download only media you own or have permission to save.
- Keep secrets out of GitHub. The Blueprint declares a Starter web service and a Free PostgreSQL database; check Render's current plan availability/pricing before applying and adjust if needed.
