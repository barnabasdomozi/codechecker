import json
import os
import shutil
import sys
from sqlalchemy.orm import sessionmaker
from typing import Annotated, Optional
from fastapi import APIRouter, Cookie, Depends, FastAPI, Header, Request, Response, status
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from thrift.protocol import TJSONProtocol
from thrift.transport import TTransport
from codechecker_api.ServerInfo_v6 import \
    serverInfoService as ServerInfoAPI_v6
from codechecker_api.Authentication_v6 import \
    codeCheckerAuthentication as AuthAPI_v6
from codechecker_api.Configuration_v6 import \
    configurationService as ConfigAPI_v6

from codechecker_common.logger import get_logger, signal_log, LOG_CONFIG
from .. import session_manager
from ..api.server_info_handler import \
    ThriftServerInfoHandler as ServerInfoHandler_v6
from ..api.authentication import \
    ThriftAuthHandler as AuthHandler_v6
from ..api.config_handler import ThriftConfigHandler as ConfigHandler_v6
import uvicorn

from ..database.config_db_model import Configuration as ORMConfiguration
from ..database.database import DBSession

def get_config_session():
    """Override this to provide the config DB session factory."""
    raise NotImplementedError(
        "config_session must be set before using readiness probe.")

LOG = get_logger('server')

class CodeCheckerFastAPIServer:
    def start_server(self, config_directory: str, workspace_directory: str,
                     package_data, port: int, config_sql_server,
                     listen_address: str, force_auth: bool,
                     skip_db_cleanup: bool, context, check_env,
                     machine_id: str,
                     api_handler_processes: Optional[int],
                     task_worker_processes: Optional[int]) -> int:
        self.config_directory = config_directory
        self.workspace_directory = workspace_directory
        self.www_root = package_data['www_root']
        self.doc_root = package_data['doc_root']
        self.version = package_data['version']
        self.context = context
        self.check_env = check_env

        # The root user file is DEPRECATED AND IGNORED
        root_file = os.path.join(config_directory, 'root.user')
        if os.path.exists(root_file):
            LOG.warning("The 'root.user' file:  %s"
                        " is deprecated and ignored. If you want to"
                        " setup an initial user with SUPER_USER permission,"
                        " configure the super_user field in the server_config.json"
                        " as described in the documentation."
                        " To get rid off this warning,"
                        " simply delete the root.user file.",
                        root_file)
        # Check whether configuration file exists, create an example if not.
        server_cfg_file = os.path.join(config_directory, 'server_config.json')
        if not os.path.exists(server_cfg_file):
            # For backward compatibility reason if the session_config.json file
            # exists we rename it to server_config.json.
            session_cfg_file = os.path.join(config_directory,
                                            'session_config.json')
            example_cfg_file = os.path.join(os.environ['CC_DATA_FILES_DIR'],
                                            'config', 'server_config.json')
            if os.path.exists(session_cfg_file):
                LOG.info("Renaming '%s' to '%s'. Please check the example "
                            "configuration file ('%s') or the user guide for more "
                            "information.", session_cfg_file,
                            server_cfg_file, example_cfg_file)
                os.rename(session_cfg_file, server_cfg_file)
            else:
                LOG.info("CodeChecker server's example configuration file "
                            "created at '%s'", server_cfg_file)
                shutil.copyfile(example_cfg_file, server_cfg_file)

        server_secrets_file = os.path.join(config_directory, 'server_secrets.json')

        try:
            self.manager = session_manager.SessionManager(
                server_cfg_file,
                server_secrets_file,
                force_auth,
                api_handler_processes,
                task_worker_processes)
        except IOError as ioerr:
            LOG.debug(ioerr)
            LOG.error("The server's configuration file "
                      "is missing or can not be read!")
            sys.exit(1)
        except ValueError as verr:
            LOG.error(verr)
            sys.exit(1)

        LOG.debug("Creating database engine for CONFIG DATABASE...")
        self.__engine = config_sql_server.create_engine()
        self.config_session = sessionmaker(bind=self.__engine)
        self.manager.set_database_connection(self.config_session)
        self.app = FastAPI()

        self._register_GET(package_data)
        self._register_POST(package_data)
        self.app.mount("/", StaticFiles(directory=package_data['www_root'], html=True), name="static")

        uvicorn.run(self.app, host=listen_address, port=port, log_config=json.loads(LOG_CONFIG))
        return 0

    def __getThriftProtocol(self, body: bytes):
        protocol_factory = TJSONProtocol.TJSONProtocolFactory()
        input_protocol_factory = protocol_factory
        output_protocol_factory = protocol_factory

        itrans = TTransport.TMemoryBuffer(body)
        otrans = TTransport.TMemoryBuffer()
        iprot = input_protocol_factory.getProtocol(itrans)
        oprot = output_protocol_factory.getProtocol(otrans)
        return iprot, oprot, otrans

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

        async def verifySession(request: Request,
                                header: Annotated[str | None, Header(alias="Authorization")] = None,
                                cookie: Annotated[str | None, Cookie(alias=session_manager.SESSION_COOKIE_NAME)] = None) -> Optional[session_manager._Session]:
            if not self.manager.is_enabled:
                return None

            session = None
            if header and header.startswith("Bearer "):
                token = header.split("Bearer ", 1)[1]
                session = self.manager.get_session(token)
            elif cookie:
                session = self.manager.get_session(cookie)

            if session:
                LOG.info("Session found")
                session.revalidate()
                return session
            else:
                client_host, client_port = \
                    request.client.host, request.client.port
                LOG.debug(
                    "%s:%s Invalid access, credentials not found - "
                    "session refused",
                    client_host,
                    str(client_port))


        @router.post("/ServerInfo", response_class=PlainTextResponse)
        async def handleServerInfo(request: Request, response: Response, api_major: int, api_minor: int, session: Annotated[Optional[session_manager._Session], Depends(verifySession)]) -> str:
            iprot, oprot, otrans = self.__getThriftProtocol(await request.body())

            server_info_handler = ServerInfoHandler_v6(package_data['version'])
            processor = ServerInfoAPI_v6.Processor(
                server_info_handler)

            processor.process(iprot, oprot)
            return otrans.getvalue()


        @router.post("/Authentication", response_class=PlainTextResponse)
        async def handleAuth(request: Request, response: Response, api_major: int, api_minor: int, session: Annotated[Optional[session_manager._Session], Depends(verifySession)]) -> str:
            iprot, oprot, otrans = self.__getThriftProtocol(await request.body())
            auth_handler = AuthHandler_v6(
                self.manager,
                session,
                self.config_session)
            processor = AuthAPI_v6.Processor(auth_handler)
            processor.process(iprot, oprot)
            return otrans.getvalue()

        @router.post("/Configuration", response_class=PlainTextResponse)
        async def handleConfig(request: Request, response: Response, api_major: int, api_minor: int, session: Annotated[Optional[session_manager._Session], Depends(verifySession)]) -> str:
            iprot, oprot, otrans = self.__getThriftProtocol(await request.body())
            conf_handler = ConfigHandler_v6(
                session,
                self.config_session,
                self.manager)
            processor = ConfigAPI_v6.Processor(conf_handler)
            processor.process(iprot, oprot)
            return otrans.getvalue()
        self.app.include_router(router, prefix="/v{api_major}.{api_minor}")
        pass
