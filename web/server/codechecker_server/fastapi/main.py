from typing import Optional
from fastapi import APIRouter, FastAPI, Request, Response, status
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from thrift.protocol import TJSONProtocol
from thrift.transport import TTransport
from codechecker_api.ServerInfo_v6 import \
    serverInfoService as ServerInfoAPI_v6
from ..api.server_info_handler import \
    ThriftServerInfoHandler as ServerInfoHandler_v6
import uvicorn

from ..database.config_db_model import Configuration as ORMConfiguration
from ..database.database import DBSession

def get_config_session():
    """Override this to provide the config DB session factory."""
    raise NotImplementedError(
        "config_session must be set before using readiness probe.")

class CodeCheckerFastAPIServer:
    def start_server(self, config_directory: str, workspace_directory: str,
                     package_data, port: int, config_sql_server,
                     listen_address: str, force_auth: bool,
                     skip_db_cleanup: bool, context, check_env,
                     machine_id: str,
                     api_handler_processes: Optional[int],
                     task_worker_processes: Optional[int]) -> int:
        self.app = FastAPI()

        self._register_GET(package_data)
        self._register_POST(package_data)
        self.app.mount("/", StaticFiles(directory=package_data['www_root'], html=True), name="static")

        uvicorn.run(self.app, host=listen_address, port=port)
        return 0
    
    def _register_GET(self, package_data):
        @self.app.get("/live", response_class=PlainTextResponse)
        async def liveness() -> str:
            return "CODECHECKER_SERVER_IS_LIVE"

        @self.app.get("/ready", response_class=PlainTextResponse)
        async def readiness(response: Response) -> str:
            try:
                with DBSession(get_config_session()) as cfg_sess:
                    cfg_sess.query(ORMConfiguration).count()
                    return "CODECHECKER_SERVER_IS_READY"
            except Exception:
                response.status_code = 500
                return "CODECHECKER_SERVER_IS_NOT_READY"
        
    def _register_POST(self, package_data):
        router = APIRouter()

        @router.post("/ServerInfo", response_class=PlainTextResponse)
        async def handleServerInfo(request: Request, response: Response) -> str:
            protocol_factory = TJSONProtocol.TJSONProtocolFactory()
            input_protocol_factory = protocol_factory
            output_protocol_factory = protocol_factory
            
            itrans = TTransport.TMemoryBuffer(await request.body())
            iprot = input_protocol_factory.getProtocol(itrans)

            otrans = TTransport.TMemoryBuffer()
            oprot = output_protocol_factory.getProtocol(otrans)

            server_info_handler = ServerInfoHandler_v6(package_data['version'])
            processor = ServerInfoAPI_v6.Processor(
                server_info_handler)
            
            processor.process(iprot, oprot)
            return otrans.getvalue()

        self.app.include_router(router, prefix="/v{api_version}")
        pass
