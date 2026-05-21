"""
'CodeChecker fastapi' command to start the FastAPI-based server.
"""

import uvicorn

from codechecker_common import arg
from codechecker_server.fastapi.main import app


def get_argparser_ctor_args():
    return {
        'prog': 'CodeChecker fastapi',
        'formatter_class': arg.RawDescriptionDefaultHelpFormatter,
        'description': "Start the CodeChecker FastAPI server.",
        'help': "Start the CodeChecker FastAPI server."
    }


def add_arguments_to_parser(parser):
    parser.add_argument('-l', '--listen-address',
                        type=str,
                        dest="listen_address",
                        default="localhost",
                        required=False,
                        help="The IP address or hostname of the server on "
                             "which it should listen for connections. "
                             "(default: localhost)")

    parser.add_argument('-p', '--port',
                        type=int,
                        dest="port",
                        default=8001,
                        required=False,
                        help="The port which will be used as listen port for "
                             "the server. (default: 8001)")

    parser.set_defaults(func=main)


def main(args):
    uvicorn.run(app, host=args.listen_address, port=args.port)
