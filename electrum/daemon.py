#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2015 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
import asyncio
import ast
import os
import time
import traceback
import sys
import threading
from typing import Dict, Optional, Tuple
import aiohttp
from aiohttp import web
from base64 import b64decode
from collections import defaultdict

import jsonrpcclient
import jsonrpcserver
from jsonrpcserver import response
from jsonrpcclient.clients.aiohttp_client import AiohttpClient

from .network import Network
from .util import (json_decode, to_bytes, to_string, profiler, standardize_path, constant_time_compare)
from .util import PR_PAID, PR_EXPIRED, get_request_status
from .wallet import Wallet, Abstract_Wallet
from .storage import WalletStorage
from .commands import known_commands, Commands
from .simple_config import SimpleConfig
from .exchange_rate import FxThread
from .logging import get_logger, Logger


_logger = get_logger(__name__)

class DaemonNotRunning(Exception):
    pass

def get_lockfile(config: SimpleConfig):
    return os.path.join(config.path, 'daemon')


def remove_lockfile(lockfile):
    os.unlink(lockfile)


def get_file_descriptor(config: SimpleConfig):
    '''Tries to create the lockfile, using O_EXCL to
    prevent races.  If it succeeds it returns the FD.
    Otherwise try and connect to the server specified in the lockfile.
    If this succeeds, the server is returned.  Otherwise remove the
    lockfile and try again.'''
    lockfile = get_lockfile(config)
    while True:
        try:
            return os.open(lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except OSError:
            pass
        try:
            request(config, 'ping')
            return None
        except DaemonNotRunning:
            # Couldn't connect; remove lockfile and try again.
            remove_lockfile(lockfile)



def request(config: SimpleConfig, endpoint, args=(), timeout=60):
    lockfile = get_lockfile(config)
    while True:
        create_time = None
        try:
            with open(lockfile) as f:
                (host, port), create_time = ast.literal_eval(f.read())
        except Exception:
            raise DaemonNotRunning()
        rpc_user, rpc_password = get_rpc_credentials(config)
        server_url = 'http://%s:%d' % (host, port)
        auth = aiohttp.BasicAuth(login=rpc_user, password=rpc_password)
        loop = asyncio.get_event_loop()
        async def request_coroutine():
            async with aiohttp.ClientSession(auth=auth, loop=loop) as session:
                server = AiohttpClient(session, server_url)
                f = getattr(server, endpoint)
                response = await f(*args)
                return response.data.result
        try:
            fut = asyncio.run_coroutine_threadsafe(request_coroutine(), loop)
            return fut.result(timeout=timeout)
        except aiohttp.client_exceptions.ClientConnectorError as e:
            _logger.info(f"failed to connect to JSON-RPC server {e}")
            if not create_time or create_time < time.time() - 1.0:
                raise DaemonNotRunning()
        # Sleep a bit and try again; it might have just been started
        time.sleep(1.0)


def get_rpc_credentials(config: SimpleConfig) -> Tuple[str, str]:
    rpc_user = config.get('rpcuser', None)
    rpc_password = config.get('rpcpassword', None)
    if rpc_user is None or rpc_password is None:
        rpc_user = 'user'
        import ecdsa, base64
        bits = 128
        nbytes = bits // 8 + (bits % 8 > 0)
        pw_int = ecdsa.util.randrange(pow(2, bits))
        pw_b64 = base64.b64encode(
            pw_int.to_bytes(nbytes, 'big'), b'-_')
        rpc_password = to_string(pw_b64, 'ascii')
        config.set_key('rpcuser', rpc_user)
        config.set_key('rpcpassword', rpc_password, save=True)
    elif rpc_password == '':
        _logger.warning('RPC authentication is disabled.')
    return rpc_user, rpc_password


class WatchTowerServer(Logger):

    def __init__(self, network):
        Logger.__init__(self)
        self.config = network.config
        self.network = network
        self.lnwatcher = network.local_watchtower
        self.app = web.Application()
        self.app.router.add_post("/", self.handle)
        self.methods = jsonrpcserver.methods.Methods()
        self.methods.add(self.get_ctn)
        self.methods.add(self.add_sweep_tx)

    async def handle(self, request):
        request = await request.text()
        self.logger.info(f'{request}')
        response = await jsonrpcserver.async_dispatch(request, methods=self.methods)
        if response.wanted:
            return web.json_response(response.deserialized(), status=response.http_status)
        else:
            return web.Response()

    async def run(self):
        host = self.config.get('watchtower_host')
        port = self.config.get('watchtower_port', 12345)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, host, port)
        await site.start()

    async def get_ctn(self, *args):
        return await self.lnwatcher.sweepstore.get_ctn(*args)

    async def add_sweep_tx(self, *args):
        return await self.lnwatcher.sweepstore.add_sweep_tx(*args)

class HttpServer(Logger):

    def __init__(self, daemon):
        Logger.__init__(self)
        self.daemon = daemon
        self.config = daemon.config
        self.pending = defaultdict(asyncio.Event)
        self.daemon.network.register_callback(self.on_payment, ['payment_received'])

    async def on_payment(self, evt, *args):
        print(evt, args)
        #await self.pending[key].set()

    async def run(self):
        from aiohttp import helpers
        app = web.Application()
        #app.on_response_prepare.append(http_server.on_response_prepare)
        app.add_routes([web.post('/api/create_invoice', self.create_request)])
        app.add_routes([web.get('/api/get_invoice', self.get_request)])
        app.add_routes([web.get('/api/get_status', self.get_status)])
        app.add_routes([web.static('/electrum', 'electrum/www')])
        runner = web.AppRunner(app)
        await runner.setup()
        host = self.config.get('http_host', 'localhost')
        port = int(self.config.get('http_port'))
        site = web.TCPSite(runner, port=port, host=host)
        await site.start()

    async def create_request(self, request):
        params = await request.post()
        wallet = self.daemon.wallet
        if 'amount_sat' not in params or not params['amount_sat'].isdigit():
            raise web.HTTPUnsupportedMediaType()
        amount = int(params['amount_sat'])
        message = params['message'] or "donation"
        payment_hash = await wallet.lnworker._add_invoice_coro(amount, message, 3600)
        key = payment_hash.hex()
        raise web.HTTPFound('/electrum/index.html?id=' + key)

    async def get_request(self, r):
        key = r.query_string
        request = self.daemon.wallet.get_request(key)
        return web.json_response(request)

    async def get_status(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        key = request.query_string
        info = self.daemon.wallet.get_request(key)
        if not info:
            await ws.send_str('unknown invoice')
            await ws.close()
            return ws
        if info.get('status') == PR_PAID:
            await ws.send_str(f'already paid')
            await ws.close()
            return ws
        if info.get('status') == PR_EXPIRED:
            await ws.send_str(f'invoice expired')
            await ws.close()
            return ws
        while True:
            try:
                await asyncio.wait_for(self.pending[key].wait(), 1)
                break
            except asyncio.TimeoutError:
                # send data on the websocket, to keep it alive
                await ws.send_str('waiting')
        await ws.send_str('paid')
        await ws.close()
        return ws


class AuthenticationError(Exception):
    pass

class Daemon(Logger):

    @profiler
    def __init__(self, config: SimpleConfig, fd=None, *, listen_jsonrpc=True):
        Logger.__init__(self)
        self.running = False
        self.running_lock = threading.Lock()
        self.config = config
        if fd is None and listen_jsonrpc:
            fd = get_file_descriptor(config)
            if fd is None:
                raise Exception('failed to lock daemon; already running?')
        self.asyncio_loop = asyncio.get_event_loop()
        if config.get('offline'):
            self.network = None
        else:
            self.network = Network(config)
        self.fx = FxThread(config, self.network)
        self.gui_object = None
        # path -> wallet;   make sure path is standardized.
        self.wallets = {}  # type: Dict[str, Abstract_Wallet]
        jobs = [self.fx.run]
        # Setup JSONRPC server
        if listen_jsonrpc:
            jobs.append(self.start_jsonrpc(config, fd))
        # request server
        if self.config.get('http_port'):
            self.http_server = HttpServer(self)
            jobs.append(self.http_server.run())
        # server-side watchtower
        self.watchtower = WatchTowerServer(self.network) if self.config.get('watchtower_host') else None
        if self.watchtower:
            jobs.append(self.watchtower.run)
        if self.network:
            self.network.start(jobs)

    def authenticate(self, headers):
        if self.rpc_password == '':
            # RPC authentication is disabled
            return
        auth_string = headers.get('Authorization', None)
        if auth_string is None:
            raise AuthenticationError('CredentialsMissing')
        basic, _, encoded = auth_string.partition(' ')
        if basic != 'Basic':
            raise AuthenticationError('UnsupportedType')
        encoded = to_bytes(encoded, 'utf8')
        credentials = to_string(b64decode(encoded), 'utf8')
        username, _, password = credentials.partition(':')
        if not (constant_time_compare(username, self.rpc_user)
                and constant_time_compare(password, self.rpc_password)):
            time.sleep(0.050)
            raise AuthenticationError('Invalid Credentials')

    async def handle(self, request):
        try:
            self.authenticate(request.headers)
        except AuthenticationError:
            return web.Response(text='Forbidden', status=403)
        request = await request.text()
        #self.logger.info(f'handling request: {request}')
        response = await jsonrpcserver.async_dispatch(request, methods=self.methods)
        if isinstance(response, jsonrpcserver.response.ExceptionResponse):
            self.logger.error(f"error handling request: {request}", exc_info=response.exc)
        if response.wanted:
            return web.json_response(response.deserialized(), status=response.http_status)
        else:
            return web.Response()

    async def start_jsonrpc(self, config: SimpleConfig, fd):
        self.app = web.Application()
        self.app.router.add_post("/", self.handle)
        self.rpc_user, self.rpc_password = get_rpc_credentials(config)
        self.methods = jsonrpcserver.methods.Methods()
        self.methods.add(self.ping)
        self.methods.add(self.gui)
        self.cmd_runner = Commands(self.config, None, self.network, self)
        for cmdname in known_commands:
            self.methods.add(getattr(self.cmd_runner, cmdname))
        self.methods.add(self.run_cmdline)
        self.host = config.get('rpchost', '127.0.0.1')
        self.port = config.get('rpcport', 0)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port)
        await site.start()
        socket = site._server.sockets[0]
        os.write(fd, bytes(repr((socket.getsockname(), time.time())), 'utf8'))
        os.close(fd)

    async def ping(self):
        return True

    async def gui(self, config_options):
        config = SimpleConfig(config_options)
        if self.gui_object:
            if hasattr(self.gui_object, 'new_window'):
                config.open_last_wallet()
                path = config.get_wallet_path()
                self.gui_object.new_window(path, config.get('url'))
                response = "ok"
            else:
                response = "error: current GUI does not support multiple windows"
        else:
            response = "Error: Electrum is running in daemon mode. Please stop the daemon first."
        return response

    def load_wallet(self, path, password) -> Optional[Abstract_Wallet]:
        path = standardize_path(path)
        # wizard will be launched if we return
        if path in self.wallets:
            wallet = self.wallets[path]
            return wallet
        storage = WalletStorage(path, manual_upgrades=True)
        if not storage.file_exists():
            return
        if storage.is_encrypted():
            if not password:
                return
            storage.decrypt(password)
        if storage.requires_split():
            return
        if storage.requires_upgrade():
            return
        if storage.get_action():
            return
        wallet = Wallet(storage)
        wallet.start_network(self.network)
        self.wallets[path] = wallet
        self.wallet = wallet
        return wallet

    def add_wallet(self, wallet: Abstract_Wallet):
        path = wallet.storage.path
        path = standardize_path(path)
        self.wallets[path] = wallet

    def get_wallet(self, path):
        path = standardize_path(path)
        return self.wallets.get(path)

    def delete_wallet(self, path):
        self.stop_wallet(path)
        if os.path.exists(path):
            os.unlink(path)
            return True
        return False

    def stop_wallet(self, path) -> bool:
        """Returns True iff a wallet was found."""
        path = standardize_path(path)
        wallet = self.wallets.pop(path, None)
        if not wallet:
            return False
        wallet.stop_threads()
        return True

    async def run_cmdline(self, config_options):
        password = config_options.get('password')
        new_password = config_options.get('new_password')
        config = SimpleConfig(config_options)
        # FIXME this is ugly...
        config.fee_estimates = self.network.config.fee_estimates.copy()
        config.mempool_fees  = self.network.config.mempool_fees.copy()
        cmdname = config.get('cmd')
        cmd = known_commands[cmdname]
        if cmd.requires_wallet:
            path = config.get_wallet_path()
            path = standardize_path(path)
            wallet = self.wallets.get(path)
            if wallet is None:
                return {'error': 'Wallet "%s" is not loaded. Use "electrum load_wallet"'%os.path.basename(path) }
        else:
            wallet = None
        # arguments passed to function
        args = map(lambda x: config.get(x), cmd.params)
        # decode json arguments
        args = [json_decode(i) for i in args]
        # options
        kwargs = {}
        for x in cmd.options:
            kwargs[x] = (config_options.get(x) if x in ['password', 'new_password'] else config.get(x))
        cmd_runner = Commands(config, wallet, self.network, self)
        func = getattr(cmd_runner, cmd.name)
        try:
            result = await func(*args, **kwargs)
        except TypeError as e:
            raise Exception("Wrapping TypeError to prevent JSONRPC-Pelix from hiding traceback") from e
        return result

    def run_daemon(self):
        self.running = True
        try:
            while self.is_running():
                time.sleep(0.1)
        except KeyboardInterrupt:
            self.running = False
        self.on_stop()

    def is_running(self):
        with self.running_lock:
            return self.running

    def stop(self):
        with self.running_lock:
            self.running = False

    def on_stop(self):
        if self.gui_object:
            self.gui_object.stop()
        # stop network/wallets
        for k, wallet in self.wallets.items():
            wallet.stop_threads()
        if self.network:
            self.logger.info("shutting down network")
            self.network.stop()
        self.logger.info("stopping, removing lockfile")
        remove_lockfile(get_lockfile(self.config))

    def run_gui(self, config, plugins):
        threading.current_thread().setName('GUI')
        gui_name = config.get('gui', 'qt')
        if gui_name in ['lite', 'classic']:
            gui_name = 'qt'
        gui = __import__('electrum.gui.' + gui_name, fromlist=['electrum'])
        self.gui_object = gui.ElectrumGui(config, self, plugins)
        try:
            self.gui_object.main()
        except BaseException as e:
            self.logger.exception('')
            # app will exit now
        self.on_stop()
