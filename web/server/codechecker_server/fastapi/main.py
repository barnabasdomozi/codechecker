from typing import Optional

import uvicorn
from fastapi import FastAPI, Response, status
from fastapi.staticfiles import StaticFiles

from ..database.config_db_model import Configuration as ORMConfiguration
from ..database.database import DBSession

app: FastAPI

def get_config_session():
    """Override this to provide the config DB session factory."""
    raise NotImplementedError(
        "config_session must be set before using readiness probe.")


@app.get("/live")
async def liveness() -> str:
    return "CODECHECKER_SERVER_IS_LIVE"


@app.get("/ready")
async def readiness(response: Response) -> str:
    try:
        with DBSession(get_config_session()) as cfg_sess:
            cfg_sess.query(ORMConfiguration).count()
            return "CODECHECKER_SERVER_IS_READY"
    except Exception:
        response.status_code = 500
        return "CODECHECKER_SERVER_IS_NOT_READY"
    


def start_server(config_directory: str, workspace_directory: str,
                 package_data, port: int, config_sql_server,
                 listen_address: str, force_auth: bool,
                 skip_db_cleanup: bool, context, check_env,
                 machine_id: str,
                 api_handler_processes: Optional[int],
                 task_worker_processes: Optional[int]) -> int:
    global app
    app = FastAPI(title="CodeChecker Server")
    if package_data:
        app.mount("/", StaticFiles(directory=package_data['www_root'], name="static"))
    uvicorn.run(app, host=listen_address, port=port)
