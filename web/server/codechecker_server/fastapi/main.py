from typing import Optional
from fastapi import FastAPI, Response, status
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from ..database.config_db_model import Configuration as ORMConfiguration
from ..database.database import DBSession

def get_config_session():
    """Override this to provide the config DB session factory."""
    raise NotImplementedError(
        "config_session must be set before using readiness probe.")

class CodeCheckerFastAPIServer:
    app = FastAPI()

    def start_server(self, config_directory: str, workspace_directory: str,
                     package_data, port: int, config_sql_server,
                     listen_address: str, force_auth: bool,
                     skip_db_cleanup: bool, context, check_env,
                     machine_id: str,
                     api_handler_processes: Optional[int],
                     task_worker_processes: Optional[int]) -> int:
        self.app.mount("/", StaticFiles(directory=package_data['www_root'], html=True), name="static")

        uvicorn.run(self.app, host="localhost", port=8001)
        return 0

    @app.get("/live", response_class=PlainTextResponse)
    async def liveness() -> str:
        return "CODECHECKER_SERVER_IS_LIVE"

    @app.get("/ready", response_class=PlainTextResponse)
    async def readiness(response: Response) -> str:
        try:
            with DBSession(get_config_session()) as cfg_sess:
                cfg_sess.query(ORMConfiguration).count()
                return "CODECHECKER_SERVER_IS_READY"
        except Exception:
            response.status_code = 500
            return "CODECHECKER_SERVER_IS_NOT_READY"
