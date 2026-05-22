from asyncio import Queue
import json
import os
import pathlib
import shutil
import signal
import socket
import ssl
import sys
from sqlalchemy.orm import sessionmaker
from typing import Annotated, Optional
import uvicorn

from fastapi import APIRouter, Cookie, Depends, FastAPI, Header, Request, Response, status
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.engine.url import make_url
from sqlalchemy.sql.expression import func
from thrift.protocol import TJSONProtocol
from thrift.transport import TTransport
from thrift.Thrift import TApplicationException
from thrift.Thrift import TMessageType

from codechecker_api_shared.ttypes import DBStatus
from codechecker_api.Authentication_v6 import \
    codeCheckerAuthentication as AuthAPI_v6
from codechecker_api.Configuration_v6 import \
    configurationService as ConfigAPI_v6
from codechecker_api.codeCheckerDBAccess_v6 import \
    codeCheckerDBAccess as ReportAPI_v6
from codechecker_api.ProductManagement_v6 import \
    codeCheckerProductService as ProductAPI_v6
from codechecker_api.ServerInfo_v6 import \
    serverInfoService as ServerInfoAPI_v6
from codechecker_api.codeCheckerServersideTasks_v6 import \
    codeCheckerServersideTaskService as TaskAPI_v6

from codechecker_common import util
from codechecker_common.compatibility.multiprocessing import \
    Pool, Process, Queue, Value, cpu_count, SyncManager
from codechecker_common.logger import get_logger, signal_log, LOG_CONFIG

from codechecker_web.shared import database_status
from codechecker_web.shared.version import get_version_str

from .. import instance_manager, permissions, routing, session_manager
from ..api.authentication import ThriftAuthHandler as AuthHandler_v6
from ..api.config_handler import ThriftConfigHandler as ConfigHandler_v6
from ..api.product_server import ThriftProductHandler as ProductHandler_v6
from ..api.report_server import ThriftRequestHandler as ReportHandler_v6
from ..api.server_info_handler import \
    ThriftServerInfoHandler as ServerInfoHandler_v6
from ..api.tasks import ThriftTaskHandler as TaskHandler_v6
from ..database.config_db_model import Product as ORMProduct, \
    Configuration as ORMConfiguration
from ..database.database import DBSession
from ..database.run_db_model import Run

from ..database.config_db_model import Product as ORMProduct, \
    Configuration as ORMConfiguration
from ..database.database import DBSession
from ..database.run_db_model import Run
from ..product import Product
from ..task_executors.main import executor as background_task_executor
from ..task_executors.task_manager import \
    TaskManager as BackgroundTaskManager

def get_config_session():
    """Override this to provide the config DB session factory."""
    raise NotImplementedError(
        "config_session must be set before using readiness probe.")

LOG = get_logger('server')

class CodeCheckerFastAPIServer:
    def __init__(self,
                 server_address,
                 _RequestHandlerClass,
                 config_directory,
                 workspace_directory,
                 product_db_sql_server,
                 pckg_data,
                 context,
                 check_env,
                 manager: session_manager.SessionManager,
                 machine_id: str,
                 task_queue: Queue,
                 task_pipes,
                 server_shutdown_flag: Value):
        LOG.debug("Initializing HTTP server...")

        self.config_directory = config_directory
        self.workspace_directory = workspace_directory
        self.www_root = pckg_data['www_root']
        self.doc_root = pckg_data['doc_root']
        self.version = pckg_data['version']
        self.context = context
        self.check_env = check_env
        self.manager = manager
        self.address, self.port = server_address
        self.__products = {}

        # Create a database engine for the configuration database.
        LOG.debug("Creating database engine for CONFIG DATABASE...")
        self.__engine = product_db_sql_server.create_engine()
        self.config_session = sessionmaker(bind=self.__engine)
        self.manager.set_database_connection(self.config_session)

        self.__task_queue = task_queue
        self.task_manager = BackgroundTaskManager(
            task_queue, task_pipes, self.config_session, self.check_env,
            server_shutdown_flag, machine_id,
            pathlib.Path(self.context.codechecker_workspace))

        # Load the initial list of products and set up the server.
        cfg_sess = self.config_session()
        permissions.initialise_defaults('SYSTEM', {
            'config_db_session': cfg_sess
        })

        self.cfg_sess_private = cfg_sess

        products = cfg_sess.query(ORMProduct).all()
        for product in products:
            self.add_product(product)
            permissions.initialise_defaults('PRODUCT', {
                'config_db_session': cfg_sess,
                'productID': product.id
            })
        cfg_sess.commit()
        cfg_sess.close()

        ssl_key_file = os.path.join(config_directory, "key.pem")
        ssl_cert_file = os.path.join(config_directory, "cert.pem")

        self.ssl_enabled = os.path.isfile(ssl_key_file) and \
            os.path.isfile(ssl_cert_file)

        if not self.ssl_enabled:
            LOG.info("Searching for SSL key at %s, cert at %s, "
                        "not found!", ssl_key_file, ssl_cert_file)
            LOG.info("Falling back to simple, insecure HTTP.")
            ssl_key_file, ssl_cert_file = None, None
        else:
            LOG.info("Initiating SSL. Server listening on secure socket.")
            LOG.debug("Using cert file: %s", ssl_cert_file)
            LOG.debug("Using key file: %s", ssl_key_file)

        self.app = FastAPI()

        self._register_GET(pckg_data)
        self._register_POST(pckg_data)
        self.app.mount("/", StaticFiles(directory=self.www_root, html=True), name="static")

        uvicorn.run(self.app, host=server_address[0],
                    port=int(server_address[1]),
                    log_config=json.loads(LOG_CONFIG),
                    ssl_certfile=ssl_cert_file, ssl_keyfile=ssl_key_file,
                    ssl_version=ssl.PROTOCOL_TLSv1_2)

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
                with DBSession(self.config_session) as cfg_sess:
                    cfg_sess.query(ORMConfiguration).count()
                    return "CODECHECKER_SERVER_IS_READY"
            except Exception:
                response.status_code = 500
                return "CODECHECKER_SERVER_IS_NOT_READY"

    def _register_POST(self, package_data):
        router = APIRouter()

        async def checkAPICompatibility(request: Request,
                                        api_major: int,
                                        api_minor: int) -> int:
            if not routing.is_supported_version(f"v{api_major}.{api_minor}"):
                error_msg = \
                    "The API version you are using is not supported " \
                    "by this server (server API version: " \
                    f"{get_version_str()})!"

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

        @router.post("/Products", response_class=PlainTextResponse)
        async def handleProducts(request: Request, response: Response, api_major: int, api_minor: int, session: Annotated[Optional[session_manager._Session], Depends(verifySession)]) -> str:
            iprot, oprot, otrans = self.__getThriftProtocol(await request.body())
            product = None # TODO FIX THIS
            prod_handler = ProductHandler_v6(
                self,
                session,
                self.config_session,
                product,
                self.version)
            processor = ProductAPI_v6.Processor(prod_handler)
            processor.process(iprot, oprot)
            return otrans.getvalue()
        self.app.include_router(router, prefix="/v{api_major}.{api_minor}", dependencies=[Depends(checkAPICompatibility)])
        pass

    @property
    def formatted_address(self) -> str:
        return f"{str(self.address)}:{self.port}"

    def configure_keepalive(self):
        """
        Enable keepalive on the socket and some TCP keepalive configuration
        option based on the server configuration file.
        """
        if not self.manager.is_keepalive_enabled():
            return

        keepalive_is_on = self.socket.getsockopt(socket.SOL_SOCKET,
                                                 socket.SO_KEEPALIVE)
        if keepalive_is_on != 0:
            LOG.debug('Socket keepalive already on.')
        else:
            LOG.debug('Socket keepalive off, turning on.')

        ret = self.socket.setsockopt(socket.SOL_SOCKET,
                                     socket.SO_KEEPALIVE, 1)
        if ret:
            LOG.error('Failed to set socket keepalive: %s', ret)

        idle = self.manager.get_keepalive_idle()
        if idle:
            ret = self.socket.setsockopt(socket.IPPROTO_TCP,
                                         socket.TCP_KEEPIDLE, idle)
            if ret:
                LOG.error('Failed to set TCP keepalive idle: %s', ret)

        interval = self.manager.get_keepalive_interval()
        if interval:
            ret = self.socket.setsockopt(socket.IPPROTO_TCP,
                                         socket.TCP_KEEPINTVL, interval)
            if ret:
                LOG.error('Failed to set TCP keepalive interval: %s', ret)

        max_probe = self.manager.get_keepalive_max_probe()
        if max_probe:
            ret = self.socket.setsockopt(socket.IPPROTO_TCP,
                                         socket.TCP_KEEPCNT, max_probe)
            if ret:
                LOG.error('Failed to set TCP max keepalive probe: %s', ret)

    def terminate(self):
        """Terminates the server and releases associated resources."""
        try:
            self.server_close()
            self.__task_queue.close()
            self.__task_queue.join_thread()
            self.__engine.dispose()

            sys.exit(128 + signal.SIGINT)
        except Exception as ex:
            LOG.error("Failed to shut down the WEB server!")
            LOG.error(str(ex))
            sys.exit(1)

    def serve_forever_with_shutdown_handler(self):
        """
        Calls `HTTPServer.serve_forever` but handles SIGINT (2) signals
        gracefully such that the open resources are properly cleaned up.
        """
        def _handler(signum: int, _frame):
            if signum not in [signal.SIGINT]:
                signal_log(LOG, "ERROR", "Signal "
                           f"<{signal.Signals(signum).name} ({signum})> "
                           "handling attempted by "
                           "'serve_forever_with_shutdown_handler'!")
                return

            signal_log(LOG, "DEBUG", f"{os.getpid()}: Received "
                       f"{signal.Signals(signum).name} ({signum}), "
                       "performing shutdown ...")
            self.terminate()

        signal.signal(signal.SIGINT, _handler)
        return self.serve_forever()

    def add_product(self, orm_product, init_db=False):
        """
        Adds a product to the list of product databases connected to
        by the server.
        Checks the database connection for the product databases.
        """
        if orm_product.endpoint in self.__products:
            LOG.debug("This product is already configured!")
            return

        LOG.debug("Setting up product '%s'", orm_product.endpoint)

        prod = Product(orm_product.id,
                       orm_product.endpoint,
                       orm_product.display_name,
                       orm_product.connection,
                       self.context,
                       self.check_env)

        # Update the product database status.
        prod.connect()

        if prod.db_status == DBStatus.FAILED_TO_CONNECT:
            LOG.debug(
                "Failed to connect to database for product '%s'",
                orm_product.endpoint)
            return

        if prod.db_status == DBStatus.SCHEMA_MISSING and init_db:
            LOG.debug("Schema was missing in the database. Initializing new")
            prod.connect(init_db=True)

        # The "num_of_runs" column of the config database is shown on the
        # product page of the web interface. This is intentionally redundant
        # with a simple query that would count the number of runs in a product:
        # measurements have proven that this caching significantly improves
        # responsibility.
        # This field is incremented whenever a run is added to a product, and
        # decreased when run(s) are removed. However, if these numbers ever
        # diverge, the product page and the bottom right of the run page would
        # display different run counts. To help on this, the num_of_runs column
        # is updated at every server startup.
        # FIXME: Pylint emits a false positive here, and states that
        # session_factory() is not callable, because it initializes to None.
        # More on this:
        # https://github.com/Ericsson/codechecker/pull/3733#issuecomment-1235304179
        # https://github.com/PyCQA/pylint/issues/6005
        with DBSession(prod.session_factory) as session:
            orm_product.num_of_runs = \
                session.query(func.count(Run.id)).one_or_none()[0] \
                # pylint: disable=not-callable

        self.__products[prod.endpoint] = prod

    def is_database_used(self, conn):
        """
        Returns bool whether the given database is already connected to by
        the server.
        """

        # get the database name from the database connection args
        driver = \
            'pysqlite' if conn.connection.engine == 'sqlite' else 'psycopg2'

        # create a tuple of database that is going to be added for comparison
        to_add = (
            f"{conn.connection.engine}+{driver}",
            conn.connection.database,
            conn.connection.host,
            conn.connection.port)

        # create a tuple of database that is already connected for comparison
        def to_tuple(product):
            url = make_url(product.connection)
            return url.drivername, url.database, url.host, url.port
        # creates a list of currently connected databases
        current_connected_databases = list(map(
            to_tuple,
            self.cfg_sess_private.query(ORMProduct).all()))

        self.cfg_sess_private.commit()
        self.cfg_sess_private.close()

        # the config database counts as an open database as well
        cfg_url = self.__engine.url
        cfg_entry = (cfg_url.drivername, cfg_url.database, cfg_url.host,
                     cfg_url.port)
        current_connected_databases.append(cfg_entry)

        # True if found, False otherwise
        return to_add in current_connected_databases

    @property
    def num_products(self):
        """
        Returns the number of products currently mounted by the server.
        """
        return len(self.__products)

    def get_product(self, endpoint):
        """
        Get the product connection object for the given endpoint, or None.
        """
        if endpoint in self.__products:
            return self.__products.get(endpoint)

        LOG.debug("Product with the given endpoint '%s' does not exist in "
                  "the local cache. Try to get it from the database.",
                  endpoint)

        # If the product doesn't find in the cache, try to get it from the
        # database.
        with DBSession(self.config_session) as cfg_sess:
            product = cfg_sess.query(ORMProduct) \
                .filter(ORMProduct.endpoint == endpoint) \
                .limit(1).one_or_none()

            if not product:
                return None

            self.add_product(product)
            permissions.initialise_defaults('PRODUCT', {
                'config_db_session': cfg_sess,
                'productID': product.id
            })

            return self.__products.get(endpoint, None)

    def get_only_product(self):
        """
        Returns the Product object for the only product connected to by the
        server, or None, if there are 0 or >= 2 products managed.
        """
        return list(self.__products.items())[0][1] if self.num_products == 1 \
            else None

    def remove_product(self, endpoint):
        product = self.get_product(endpoint)
        if not product:
            raise ValueError(
                f"The product with the given endpoint '{endpoint}' does "
                "not exist!")

        LOG.info("Disconnecting product '%s'", endpoint)
        product.teardown()

        del self.__products[endpoint]

    def remove_products_except(self, endpoints_to_keep):
        """
        Removes EVERY product connection from the server except those
        endpoints specified in :endpoints_to_keep.
        """
        for ep in list(self.__products):
            if ep not in endpoints_to_keep:
                self.remove_product(ep)

