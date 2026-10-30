# Dograh - Project Overview

Dograh is a voice AI platform for building and deploying conversational AI agents with telephony and WebRTC support.

## Project Structure

```
dograh/
├── api/              # Backend - FastAPI application
├── ui/               # Frontend - Next.js application
├── scripts/          # Helper scripts for local development
├── docs/             # Mintlify documentation
├── pipecat/          # Pipecat framework (git submodule)
├── docker-compose.yaml       # Production/OSS deployment
├── docker-compose-local.yaml # Local development services
```

## Tech Stack

- **Backend**: Python with FastAPI
- **Frontend**: Next.js 15 with React 19, TypeScript, Tailwind CSS
- **Database**: PostgreSQL with SQLAlchemy (async)
- **Cache/Queue**: Redis with ARQ for background tasks
- **Storage**: MinIO (S3-compatible) for audio files

## Local Development

Contributor setup and service startup are documented in `docs/contribution/setup.mdx`.

## Production voice-server releases

**Always build each voice-server release FROM the image currently running in production.** Releases stack existing changes; using an older release or the repository's default base can silently overwrite live features.

- Before building, inspect `dograh-api-1` and `dograh-worker-1` and record both `.Config.Image` and the immutable `.Image` ID. Base each changed service on its actual running image. A shared candidate requires matching service image IDs.
- Ensure the Dockerfile's `FROM` resolves to that exact running image ID. If the builder needs a tag, create a unique local base tag for the inspected image ID; never hard-code a previous release tag or assume a mutable tag still identifies the running image.
- Layer only the reviewed changed files onto that base. Verify the candidate's filesystem layers retain the running image's complete layer history, and preserve the effective Compose configuration apart from the intended release changes.
- Immediately before applying, recheck the running image ID. If it changed while preparing the release, rebuild from the new running image before deploying. Never deploy a candidate prepared against an older live base.

## Environment Configuration

- `api/.env` - Backend environment variables. Source this when running diagnostic scripts or one-off services against the dev DB (e.g. `python -m api.services.admin_utils.local_exec`).
- `api/.env.test` - Test-only environment variables. Source this when running pytest so tests hit the test DB and never the dev/prod credentials in `api/.env`.
- `ui/.env` - Frontend environment variables

Typical invocation:

```bash
# Tests
source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/...

# Diagnostics / scripts
source venv/bin/activate && set -a && source api/.env && set +a && python -m api.services.admin_utils.local_exec
```
