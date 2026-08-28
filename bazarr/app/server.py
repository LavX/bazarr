# coding=utf-8

import signal
import warnings
import logging
import errno
from literals import EXIT_INTERRUPT, EXIT_NORMAL, EXIT_PORT_ALREADY_IN_USE_ERROR
from utilities.central import restart_bazarr, stop_bazarr
from utilities.tracemalloc_dumper import install as install_tracemalloc_dumper

from waitress.server import create_server
from time import sleep

from api import api_bp
from .ui import ui_bp
from .get_args import args
from .config import settings, base_url
from .database import close_database
from .app import create_app

app = create_app()
from compat import register as register_compat  # noqa: E402
register_compat(app, base_url=base_url)
app.register_blueprint(api_bp, url_prefix=base_url.rstrip('/') + '/api')
app.register_blueprint(ui_bp, url_prefix=base_url.rstrip('/'))


class Server:
    def __init__(self):
        # Mute DeprecationWarning
        warnings.simplefilter("ignore", DeprecationWarning)
        # Mute Insecure HTTPS requests made to Sonarr and Radarr
        warnings.filterwarnings('ignore', message='Unverified HTTPS request')
        # Mute Python3 BrokenPipeError
        warnings.simplefilter("ignore", BrokenPipeError)

        self.server = None
        self.connected = False
        self.address = str(settings.general.ip)
        self.port = int(args.port) if args.port else int(settings.general.port)
        self.interrupted = False

        install_tracemalloc_dumper()

        while not self.connected:
            sleep(0.1)
            self.configure_server()

    def configure_server(self):
        try:
            # Trust X-Forwarded-* only from 127.0.0.1 (the supervisor proxy
            # in docker/supervisor.py). Direct clients that inject these
            # headers are untrusted and Waitress strips them. The compat
            # endpoint reads X-Forwarded-Host/Proto for download-link
            # construction; trusting arbitrary client values would let an
            # attacker forge stream URLs and exfiltrate the Api-Key.
            # Thread count: measured on a live 4-instance install, the old
            # inherited threads=100 accounted for 100 of 123 process threads
            # at idle for a single-user UI whose heavy work runs in background
            # schedulers and out-of-process Provider Hub workers. The busiest
            # in-process surface is the subtitle editor's stream/peaks
            # endpoints, which a browser fans out over at most a handful of
            # connections; 16 covers that with headroom, and waitress queues
            # rather than drops beyond it. Configurable (4..100) for larger
            # installs via general.web_server_threads.
            self.server = create_server(app,
                                        host=self.address,
                                        port=self.port,
                                        threads=settings.general.web_server_threads,
                                        trusted_proxy='127.0.0.1',
                                        trusted_proxy_headers={'x-forwarded-host',
                                                               'x-forwarded-proto',
                                                               'x-forwarded-for'})
            self.connected = True
        except OSError as error:
            if error.errno == errno.EADDRNOTAVAIL:
                logging.exception("BAZARR cannot bind to specified IP, trying with 0.0.0.0")
                self.address = '0.0.0.0'
                self.connected = False
                super(Server, self).__init__()
            elif error.errno == errno.EADDRINUSE:
                if self.port != '6767':
                    logging.exception("BAZARR cannot bind to specified TCP port, trying with default (6767)")
                    self.port = '6767'
                    self.connected = False
                    super(Server, self).__init__()
                else:
                    logging.exception("BAZARR cannot bind to default TCP port (6767) because it's already in use, "
                                      "exiting...")
                    self.shutdown(EXIT_PORT_ALREADY_IN_USE_ERROR)
            elif error.errno in [errno.ENOLINK, errno.EAFNOSUPPORT]:
                logging.exception("BAZARR cannot bind to IPv6 (*), trying with 0.0.0.0")
                self.address = '0.0.0.0'
                self.connected = False
                super(Server, self).__init__()
            else:
                logging.exception("BAZARR cannot start because of unhandled exception.")
                self.shutdown()

    def interrupt_handler(self, signum, frame):
        if not self.interrupted:
            # ignore user hammering Ctrl-C; we heard you the first time!
            self.interrupted = True
            status = EXIT_INTERRUPT if signum == signal.SIGINT else EXIT_NORMAL
            self.shutdown(status)

    def start(self):
        self.server.print_listen("BAZARR is started and waiting for requests on: http://{}:{}")
        signal.signal(signal.SIGINT, self.interrupt_handler)
        signal.signal(signal.SIGTERM, self.interrupt_handler)
        try:
            self.server.run()
        except (KeyboardInterrupt, SystemExit):
            self.shutdown()
        except OSError as error:
            if error.errno == 9:
                # deal with "OSError: [Errno 9] Bad file descriptor" by closing webserver again.
                self.server.close()
            else:
                pass
        except Exception:
            pass

    def close_all(self):
        print("Closing database...")
        close_database()
        if self.server:
            print("Closing webserver...")
            self.server.close()

    def shutdown(self, status=EXIT_NORMAL):
        self.close_all()
        stop_bazarr(status)

    def restart(self):
        self.close_all()
        restart_bazarr()


webserver = Server()

