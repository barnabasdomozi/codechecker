from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from ..database.config_db_model import Configuration as ORMConfiguration
from ..database.database import DBSession

app = FastAPI(title="CodeChecker Server")


def get_config_session():
    """Override this to provide the config DB session factory."""
    raise NotImplementedError(
        "config_session must be set before using readiness probe.")


@app.get("/live", response_class=PlainTextResponse)
async def liveness():
    """Handle liveness probe."""
    return PlainTextResponse(
        content="CODECHECKER_SERVER_IS_LIVE", status_code=200)


@app.get("/ready", response_class=PlainTextResponse)
async def readiness():
    """Handle readiness probe."""
    try:
        with DBSession(get_config_session()) as cfg_sess:
            cfg_sess.query(ORMConfiguration).count()
            return PlainTextResponse(
                content="CODECHECKER_SERVER_IS_READY", status_code=200)
    except Exception:
        return PlainTextResponse(
            content="CODECHECKER_SERVER_IS_NOT_READY", status_code=500)
