#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2011 thomasv@gitorious
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

import sys
import datetime
import copy
import argparse
import json
import ast
import base64
import operator
import asyncio
from functools import wraps, partial
from decimal import Decimal
from typing import Optional, TYPE_CHECKING

from .import util, ecc
from .util import bfh, bh2u, format_satoshis, json_decode, json_encode, is_hash256_str, is_hex_str, to_bytes, timestamp_to_datetime
from .util import standardize_path
from . import bitcoin
from .bitcoin import is_address,  hash_160, COIN, TYPE_ADDRESS
from .bip32 import BIP32Node
from .i18n import _
from .transaction import Transaction, multisig_script, TxOutput
from .paymentrequest import PR_PAID, PR_UNPAID, PR_UNKNOWN, PR_EXPIRED
from .synchronizer import Notifier
from .wallet import Abstract_Wallet, create_new_wallet, restore_wallet_from_text
from .address_synchronizer import TX_HEIGHT_LOCAL
from .mnemonic import Mnemonic
from .lnutil import SENT, RECEIVED
from .lnpeer import channel_id_from_funding_tx
from .plugin import run_hook
from .version import ELECTRUM_VERSION
from .simple_config import SimpleConfig


if TYPE_CHECKING:
    from .network import Network
    from .daemon import Daemon


known_commands = {}


def satoshis(amount):
    # satoshi conversion must not be performed by the parser
    return int(COIN*Decimal(amount)) if amount not in ['!', None] else amount


class Command:
    def __init__(self, func, s):
        self.name = func.__name__
        self.requires_network = 'n' in s
        self.requires_wallet = 'w' in s
        self.requires_password = 'p' in s
        self.description = func.__doc__
        self.help = self.description.split('.')[0] if self.description else None
        varnames = func.__code__.co_varnames[1:func.__code__.co_argcount]
        self.defaults = func.__defaults__
        if self.defaults:
            n = len(self.defaults)
            self.params = list(varnames[:-n])
            self.options = list(varnames[-n:])
        else:
            self.params = list(varnames)
            self.options = []
            self.defaults = []


def command(s):
    def decorator(func):
        global known_commands
        name = func.__name__
        known_commands[name] = Command(func, s)
        @wraps(func)
        def func_wrapper(*args, **kwargs):
            c = known_commands[func.__name__]
            wallet = args[0].wallet
            password = kwargs.get('password')
            if c.requires_wallet and wallet is None:
                raise Exception("wallet not loaded. Use 'electrum daemon load_wallet'")
            if c.requires_password and password is None and wallet.has_password():
                return {'error': 'Password required' }
            return func(*args, **kwargs)
        return func_wrapper
    return decorator


class Commands:

    def __init__(self, config: 'SimpleConfig',
                 wallet: Abstract_Wallet,
                 network: Optional['Network'],
                 daemon: 'Daemon' = None, callback=None):
        self.config = config
        self.wallet = wallet
        self.daemon = daemon
        self.network = network
        self._callback = callback
        self.lnworker = self.wallet.lnworker if self.wallet else None

    def _run(self, method, args, password_getter=None, **kwargs):
        """This wrapper is called from unit tests and the Qt python console."""
        cmd = known_commands[method]
        password = kwargs.get('password', None)
        if (cmd.requires_password and self.wallet.has_password()
                and password is None):
            password = password_getter()
            if password is None:
                return

        f = getattr(self, method)
        if cmd.requires_password:
            kwargs['password'] = password

        coro = f(*args, **kwargs)
        fut = asyncio.run_coroutine_threadsafe(coro, asyncio.get_event_loop())
        result = fut.result()

        if self._callback:
            self._callback()
        return result

    @command('')
    async def commands(self):
        """List of commands"""
        return ' '.join(sorted(known_commands.keys()))

    @command('n')
    async def getinfo(self):
        """ network info """
        net_params = self.network.get_parameters()
        response = {
            'path': self.network.config.path,
            'server': net_params.host,
            'blockchain_height': self.network.get_local_height(),
            'server_height': self.network.get_server_height(),
            'spv_nodes': len(self.network.get_interfaces()),
            'connected': self.network.is_connected(),
            'auto_connect': net_params.auto_connect,
            'version': ELECTRUM_VERSION,
            'default_wallet': self.config.get_wallet_path(),
            'fee_per_kb': self.config.fee_per_kb(),
        }
        return response

    @command('n')
    async def stop(self):
        """Stop daemon"""
        self.daemon.stop()
        return "Daemon stopped"

    @command('n')
    async def list_wallets(self):
        """List wallets open in daemon"""
        return [{'path':k, 'synchronized':w.is_up_to_date()} for k, w in self.daemon.wallets.items()]

    @command('n')
    async def load_wallet(self):
        """Open wallet in daemon"""
        path = self.config.get_wallet_path()
        wallet = self.daemon.load_wallet(path, self.config.get('password'))
        if wallet is not None:
            self.wallet = wallet
            run_hook('load_wallet', wallet, None)
        response = wallet is not None
        return response

    @command('n')
    async def close_wallet(self):
        """Close wallet"""
        path = self.config.get_wallet_path()
        return self.daemon.stop_wallet(path)

    @command('')
    async def create(self, passphrase=None, password=None, encrypt_file=True, seed_type=None):
        """Create a new wallet.
        If you want to be prompted for an argument, type '?' or ':' (concealed)
        """
        d = create_new_wallet(path=self.config.get_wallet_path(),
                              passphrase=passphrase,
                              password=password,
                              encrypt_file=encrypt_file,
                              seed_type=seed_type)
        return {
            'seed': d['seed'],
            'path': d['wallet'].storage.path,
            'msg': d['msg'],
        }

    @command('')
    async def restore(self, text, passphrase=None, password=None, encrypt_file=True):
        """Restore a wallet from text. Text can be a seed phrase, a master
        public key, a master private key, a list of bitcoin addresses
        or bitcoin private keys.
        If you want to be prompted for an argument, type '?' or ':' (concealed)
        """
        # TODO create a separate command that blocks until wallet is synced
        d = restore_wallet_from_text(text,
                                     path=self.config.get_wallet_path(),
                                     passphrase=passphrase,
                                     password=password,
                                     encrypt_file=encrypt_file)
        return {
            'path': d['wallet'].storage.path,
            'msg': d['msg'],
        }

    @command('wp')
    async def password(self, password=None, new_password=None):
        """Change wallet password. """
        if self.wallet.storage.is_encrypted_with_hw_device() and new_password:
            raise Exception("Can't change the password of a wallet encrypted with a hw device.")
        b = self.wallet.storage.is_encrypted()
        self.wallet.update_password(password, new_password, b)
        self.wallet.storage.write()
        return {'password':self.wallet.has_password()}

    @command('w')
    async def get(self, key):
        """Return item from wallet storage"""
        return self.wallet.storage.get(key)

    @command('')
    async def getconfig(self, key):
        """Return a configuration variable. """
        return self.config.get(key)

    @classmethod
    def _setconfig_normalize_value(cls, key, value):
        if key not in ('rpcuser', 'rpcpassword'):
            value = json_decode(value)
            try:
                value = ast.literal_eval(value)
            except:
                pass
        return value

    @command('')
    async def setconfig(self, key, value):
        """Set a configuration variable. 'value' may be a string or a Python expression."""
        value = self._setconfig_normalize_value(key, value)
        self.config.set_key(key, value)
        return True

    @command('')
    async def make_seed(self, nbits=132, language=None, seed_type=None):
        """Create a seed"""
        from .mnemonic import Mnemonic
        s = Mnemonic(language).make_seed(seed_type, num_bits=nbits)
        return s

    @command('n')
    async def getaddresshistory(self, address):
        """Return the transaction history of any address. Note: This is a
        walletless server query, results are not checked by SPV.
        """
        sh = bitcoin.address_to_scripthash(address)
        return await self.network.get_history_for_scripthash(sh)

    @command('w')
    async def listunspent(self):
        """List unspent outputs. Returns the list of unspent transaction
        outputs in your wallet."""
        l = copy.deepcopy(self.wallet.get_utxos())
        for i in l:
            v = i["value"]
            i["value"] = str(Decimal(v)/COIN) if v is not None else None
        return l

    @command('n')
    async def getaddressunspent(self, address):
        """Returns the UTXO list of any address. Note: This
        is a walletless server query, results are not checked by SPV.
        """
        sh = bitcoin.address_to_scripthash(address)
        return await self.network.listunspent_for_scripthash(sh)

    @command('')
    async def serialize(self, jsontx):
        """Create a transaction from json inputs.
        Inputs must have a redeemPubkey.
        Outputs must be a list of {'address':address, 'value':satoshi_amount}.
        """
        keypairs = {}
        inputs = jsontx.get('inputs')
        outputs = jsontx.get('outputs')
        locktime = jsontx.get('lockTime', 0)
        for txin in inputs:
            if txin.get('output'):
                prevout_hash, prevout_n = txin['output'].split(':')
                txin['prevout_n'] = int(prevout_n)
                txin['prevout_hash'] = prevout_hash
            sec = txin.get('privkey')
            if sec:
                txin_type, privkey, compressed = bitcoin.deserialize_privkey(sec)
                pubkey = ecc.ECPrivkey(privkey).get_public_key_hex(compressed=compressed)
                keypairs[pubkey] = privkey, compressed
                txin['type'] = txin_type
                txin['x_pubkeys'] = [pubkey]
                txin['signatures'] = [None]
                txin['num_sig'] = 1

        outputs = [TxOutput(TYPE_ADDRESS, x['address'], int(x['value'])) for x in outputs]
        tx = Transaction.from_io(inputs, outputs, locktime=locktime)
        tx.sign(keypairs)
        return tx.as_dict()

    @command('wp')
    async def signtransaction(self, tx, privkey=None, password=None):
        """Sign a transaction. The wallet keys will be used unless a private key is provided."""
        tx = Transaction(tx)
        if privkey:
            txin_type, privkey2, compressed = bitcoin.deserialize_privkey(privkey)
            pubkey = ecc.ECPrivkey(privkey2).get_public_key_bytes(compressed=compressed).hex()
            tx.sign({pubkey:(privkey2, compressed)})
        else:
            self.wallet.sign_transaction(tx, password)
        return tx.as_dict()

    @command('')
    async def deserialize(self, tx):
        """Deserialize a serialized transaction"""
        tx = Transaction(tx)
        return tx.deserialize(force_full_parse=True)

    @command('n')
    async def broadcast(self, tx):
        """Broadcast a transaction to the network. """
        tx = Transaction(tx)
        await self.network.broadcast_transaction(tx)
        return tx.txid()

    @command('')
    async def createmultisig(self, num, pubkeys):
        """Create multisig address"""
        assert isinstance(pubkeys, list), (type(num), type(pubkeys))
        redeem_script = multisig_script(pubkeys, num)
        address = bitcoin.hash160_to_p2sh(hash_160(bfh(redeem_script)))
        return {'address':address, 'redeemScript':redeem_script}

    @command('w')
    async def freeze(self, address):
        """Freeze address. Freeze the funds at one of your wallet\'s addresses"""
        return self.wallet.set_frozen_state_of_addresses([address], True)

    @command('w')
    async def unfreeze(self, address):
        """Unfreeze address. Unfreeze the funds at one of your wallet\'s address"""
        return self.wallet.set_frozen_state_of_addresses([address], False)

    @command('wp')
    async def getprivatekeys(self, address, password=None):
        """Get private keys of addresses. You may pass a single wallet address, or a list of wallet addresses."""
        if isinstance(address, str):
            address = address.strip()
        if is_address(address):
            return self.wallet.export_private_key(address, password)[0]
        domain = address
        return [self.wallet.export_private_key(address, password)[0] for address in domain]

    @command('w')
    async def ismine(self, address):
        """Check if address is in wallet. Return true if and only address is in wallet"""
        return self.wallet.is_mine(address)

    @command('')
    async def dumpprivkeys(self):
        """Deprecated."""
        return "This command is deprecated. Use a pipe instead: 'electrum listaddresses | electrum getprivatekeys - '"

    @command('')
    async def validateaddress(self, address):
        """Check that an address is valid. """
        return is_address(address)

    @command('w')
    async def getpubkeys(self, address):
        """Return the public keys for a wallet address. """
        return self.wallet.get_public_keys(address)

    @command('w')
    async def getbalance(self):
        """Return the balance of your wallet. """
        c, u, x = self.wallet.get_balance()
        l = self.lnworker.get_balance() if self.lnworker else None
        out = {"confirmed": str(Decimal(c)/COIN)}
        if u:
            out["unconfirmed"] = str(Decimal(u)/COIN)
        if x:
            out["unmatured"] = str(Decimal(x)/COIN)
        if l:
            out["lightning"] = str(Decimal(l)/COIN)
        return out

    @command('n')
    async def getaddressbalance(self, address):
        """Return the balance of any address. Note: This is a walletless
        server query, results are not checked by SPV.
        """
        sh = bitcoin.address_to_scripthash(address)
        out = await self.network.get_balance_for_scripthash(sh)
        out["confirmed"] =  str(Decimal(out["confirmed"])/COIN)
        out["unconfirmed"] =  str(Decimal(out["unconfirmed"])/COIN)
        return out

    @command('n')
    async def getmerkle(self, txid, height):
        """Get Merkle branch of a transaction included in a block. Electrum
        uses this to verify transactions (Simple Payment Verification)."""
        return await self.network.get_merkle_for_transaction(txid, int(height))

    @command('n')
    async def getservers(self):
        """Return the list of available servers"""
        return self.network.get_servers()

    @command('')
    async def version(self):
        """Return the version of Electrum."""
        from .version import ELECTRUM_VERSION
        return ELECTRUM_VERSION

    @command('w')
    async def getmpk(self):
        """Get master public key. Return your wallet\'s master public key"""
        return self.wallet.get_master_public_key()

    @command('wp')
    async def getmasterprivate(self, password=None):
        """Get master private key. Return your wallet\'s master private key"""
        return str(self.wallet.keystore.get_master_private_key(password))

    @command('')
    async def convert_xkey(self, xkey, xtype):
        """Convert xtype of a master key. e.g. xpub -> ypub"""
        try:
            node = BIP32Node.from_xkey(xkey)
        except:
            raise Exception('xkey should be a master public/private key')
        return node._replace(xtype=xtype).to_xkey()

    @command('wp')
    async def getseed(self, password=None):
        """Get seed phrase. Print the generation seed of your wallet."""
        s = self.wallet.get_seed(password)
        return s

    @command('wp')
    async def importprivkey(self, privkey, password=None):
        """Import a private key."""
        if not self.wallet.can_import_privkey():
            return "Error: This type of wallet cannot import private keys. Try to create a new wallet with that key."
        try:
            addr = self.wallet.import_private_key(privkey, password)
            out = "Keypair imported: " + addr
        except Exception as e:
            out = "Error: " + repr(e)
        return out

    def _resolver(self, x):
        if x is None:
            return None
        out = self.wallet.contacts.resolve(x)
        if out.get('type') == 'openalias' and self.nocheck is False and out.get('validated') is False:
            raise Exception('cannot verify alias', x)
        return out['address']

    @command('n')
    async def sweep(self, privkey, destination, fee=None, nocheck=False, imax=100):
        """Sweep private keys. Returns a transaction that spends UTXOs from
        privkey to a destination address. The transaction is not
        broadcasted."""
        from .wallet import sweep
        tx_fee = satoshis(fee)
        privkeys = privkey.split()
        self.nocheck = nocheck
        #dest = self._resolver(destination)
        tx = sweep(privkeys, self.network, self.config, destination, tx_fee, imax)
        return tx.as_dict() if tx else None

    @command('wp')
    async def signmessage(self, address, message, password=None):
        """Sign a message with a key. Use quotes if your message contains
        whitespaces"""
        sig = self.wallet.sign_message(address, message, password)
        return base64.b64encode(sig).decode('ascii')

    @command('')
    async def verifymessage(self, address, signature, message):
        """Verify a signature."""
        sig = base64.b64decode(signature)
        message = util.to_bytes(message)
        return ecc.verify_message_with_address(address, sig, message)

    def _mktx(self, outputs, *, fee=None, feerate=None, change_addr=None, domain=None,
              nocheck=False, unsigned=False, rbf=None, password=None, locktime=None):
        if fee is not None and feerate is not None:
            raise Exception("Cannot specify both 'fee' and 'feerate' at the same time!")
        self.nocheck = nocheck
        change_addr = self._resolver(change_addr)
        domain = None if domain is None else map(self._resolver, domain)
        final_outputs = []
        for address, amount in outputs:
            address = self._resolver(address)
            amount = satoshis(amount)
            final_outputs.append(TxOutput(TYPE_ADDRESS, address, amount))

        coins = self.wallet.get_spendable_coins(domain, self.config)
        if feerate is not None:
            fee_per_kb = 1000 * Decimal(feerate)
            fee_estimator = partial(SimpleConfig.estimate_fee_for_feerate, fee_per_kb)
        else:
            fee_estimator = fee
        tx = self.wallet.make_unsigned_transaction(coins, final_outputs, self.config, fee_estimator, change_addr)
        if locktime is not None:
            tx.locktime = locktime
        if rbf is None:
            rbf = self.config.get('use_rbf', True)
        if rbf:
            tx.set_rbf(True)
        if not unsigned:
            self.wallet.sign_transaction(tx, password)
        return tx

    @command('wp')
    async def payto(self, destination, amount, fee=None, feerate=None, from_addr=None, change_addr=None,
                    nocheck=False, unsigned=False, rbf=None, password=None, locktime=None):
        """Create a transaction. """
        tx_fee = satoshis(fee)
        domain = from_addr.split(',') if from_addr else None
        tx = self._mktx([(destination, amount)],
                        fee=tx_fee,
                        feerate=feerate,
                        change_addr=change_addr,
                        domain=domain,
                        nocheck=nocheck,
                        unsigned=unsigned,
                        rbf=rbf,
                        password=password,
                        locktime=locktime)
        return tx.as_dict()

    @command('wp')
    async def paytomany(self, outputs, fee=None, feerate=None, from_addr=None, change_addr=None,
                        nocheck=False, unsigned=False, rbf=None, password=None, locktime=None):
        """Create a multi-output transaction. """
        tx_fee = satoshis(fee)
        domain = from_addr.split(',') if from_addr else None
        tx = self._mktx(outputs,
                        fee=tx_fee,
                        feerate=feerate,
                        change_addr=change_addr,
                        domain=domain,
                        nocheck=nocheck,
                        unsigned=unsigned,
                        rbf=rbf,
                        password=password,
                        locktime=locktime)
        return tx.as_dict()

    @command('w')
    async def onchain_history(self, year=None, show_addresses=False, show_fiat=False, show_fees=False):
        """Wallet onchain history. Returns the transaction history of your wallet."""
        kwargs = {
            'show_addresses': show_addresses,
            'show_fees': show_fees,
        }
        if year:
            import time
            start_date = datetime.datetime(year, 1, 1)
            end_date = datetime.datetime(year+1, 1, 1)
            kwargs['from_timestamp'] = time.mktime(start_date.timetuple())
            kwargs['to_timestamp'] = time.mktime(end_date.timetuple())
        if show_fiat:
            from .exchange_rate import FxThread
            fx = FxThread(self.config, None)
            kwargs['fx'] = fx
        return json_encode(self.wallet.get_detailed_history(**kwargs))

    @command('w')
    async def lightning_history(self, show_fiat=False):
        """ lightning history """
        lightning_history = self.wallet.lnworker.get_history() if self.wallet.lnworker else []
        return json_encode(lightning_history)

    @command('w')
    async def setlabel(self, key, label):
        """Assign a label to an item. Item may be a bitcoin address or a
        transaction ID"""
        self.wallet.set_label(key, label)

    @command('w')
    async def listcontacts(self):
        """Show your list of contacts"""
        return self.wallet.contacts

    @command('w')
    async def getalias(self, key):
        """Retrieve alias. Lookup in your list of contacts, and for an OpenAlias DNS record."""
        return self.wallet.contacts.resolve(key)

    @command('w')
    async def searchcontacts(self, query):
        """Search through contacts, return matching entries. """
        results = {}
        for key, value in self.wallet.contacts.items():
            if query.lower() in key.lower():
                results[key] = value
        return results

    @command('w')
    async def listaddresses(self, receiving=False, change=False, labels=False, frozen=False, unused=False, funded=False, balance=False):
        """List wallet addresses. Returns the list of all addresses in your wallet. Use optional arguments to filter the results."""
        out = []
        for addr in self.wallet.get_addresses():
            if frozen and not self.wallet.is_frozen_address(addr):
                continue
            if receiving and self.wallet.is_change(addr):
                continue
            if change and not self.wallet.is_change(addr):
                continue
            if unused and self.wallet.is_used(addr):
                continue
            if funded and self.wallet.is_empty(addr):
                continue
            item = addr
            if labels or balance:
                item = (item,)
            if balance:
                item += (format_satoshis(sum(self.wallet.get_addr_balance(addr))),)
            if labels:
                item += (repr(self.wallet.labels.get(addr, '')),)
            out.append(item)
        return out

    @command('n')
    async def gettransaction(self, txid):
        """Retrieve a transaction. """
        tx = None
        if self.wallet:
            tx = self.wallet.db.get_transaction(txid)
        if tx is None:
            raw = await self.network.get_transaction(txid)
            if raw:
                tx = Transaction(raw)
            else:
                raise Exception("Unknown transaction")
        return tx.as_dict()

    @command('')
    async def encrypt(self, pubkey, message) -> str:
        """Encrypt a message with a public key. Use quotes if the message contains whitespaces."""
        if not is_hex_str(pubkey):
            raise Exception(f"pubkey must be a hex string instead of {repr(pubkey)}")
        try:
            message = to_bytes(message)
        except TypeError:
            raise Exception(f"message must be a string-like object instead of {repr(message)}")
        public_key = ecc.ECPubkey(bfh(pubkey))
        encrypted = public_key.encrypt_message(message)
        return encrypted.decode('utf-8')

    @command('wp')
    async def decrypt(self, pubkey, encrypted, password=None) -> str:
        """Decrypt a message encrypted with a public key."""
        if not is_hex_str(pubkey):
            raise Exception(f"pubkey must be a hex string instead of {repr(pubkey)}")
        if not isinstance(encrypted, (str, bytes, bytearray)):
            raise Exception(f"encrypted must be a string-like object instead of {repr(encrypted)}")
        decrypted = self.wallet.decrypt_message(pubkey, encrypted, password)
        return decrypted.decode('utf-8')

    def _format_request(self, out):
        from .util import get_request_status
        out['amount_BTC'] = format_satoshis(out.get('amount'))
        out['status'] = get_request_status(out)
        return out

    @command('w')
    async def getrequest(self, key):
        """Return a payment request"""
        r = self.wallet.get_payment_request(key, self.config)
        if not r:
            raise Exception("Request not found")
        return self._format_request(r)

    #@command('w')
    #async def ackrequest(self, serialized):
    #    """<Not implemented>"""
    #    pass

    @command('w')
    async def listrequests(self, pending=False, expired=False, paid=False):
        """List the payment requests you made."""
        out = self.wallet.get_sorted_requests(self.config)
        if pending:
            f = PR_UNPAID
        elif expired:
            f = PR_EXPIRED
        elif paid:
            f = PR_PAID
        else:
            f = None
        if f is not None:
            out = list(filter(lambda x: x.get('status')==f, out))
        return list(map(self._format_request, out))

    @command('w')
    async def createnewaddress(self):
        """Create a new receiving address, beyond the gap limit of the wallet"""
        return self.wallet.create_new_address(False)

    @command('w')
    async def getunusedaddress(self):
        """Returns the first unused address of the wallet, or None if all addresses are used.
        An address is considered as used if it has received a transaction, or if it is used in a payment request."""
        return self.wallet.get_unused_address()

    @command('w')
    async def addrequest(self, amount, memo='', expiration=None, force=False):
        """Create a payment request, using the first unused address of the wallet.
        The address will be considered as used after this operation.
        If no payment is received, the address will be considered as unused if the payment request is deleted from the wallet."""
        addr = self.wallet.get_unused_address()
        if addr is None:
            if force:
                addr = self.wallet.create_new_address(False)
            else:
                return False
        amount = satoshis(amount)
        expiration = int(expiration) if expiration else None
        req = self.wallet.make_payment_request(addr, amount, memo, expiration)
        self.wallet.add_payment_request(req, self.config)
        out = self.wallet.get_payment_request(addr, self.config)
        return self._format_request(out)

    @command('w')
    async def addtransaction(self, tx):
        """ Add a transaction to the wallet history """
        tx = Transaction(tx)
        if not self.wallet.add_transaction(tx.txid(), tx):
            return False
        self.wallet.storage.write()
        return tx.txid()

    @command('wp')
    async def signrequest(self, address, password=None):
        "Sign payment request with an OpenAlias"
        alias = self.config.get('alias')
        if not alias:
            raise Exception('No alias in your configuration')
        alias_addr = self.wallet.contacts.resolve(alias)['address']
        self.wallet.sign_payment_request(address, alias, alias_addr, password)

    @command('w')
    async def rmrequest(self, address):
        """Remove a payment request"""
        return self.wallet.remove_payment_request(address, self.config)

    @command('w')
    async def clearrequests(self):
        """Remove all payment requests"""
        for k in list(self.wallet.receive_requests.keys()):
            self.wallet.remove_payment_request(k, self.config)

    @command('n')
    async def notify(self, address: str, URL: str):
        """Watch an address. Every time the address changes, a http POST is sent to the URL."""
        if not hasattr(self, "_notifier"):
            self._notifier = Notifier(self.network)
        await self._notifier.start_watching_queue.put((address, URL))
        return True

    @command('wn')
    async def is_synchronized(self):
        """ return wallet synchronization status """
        return self.wallet.is_up_to_date()

    @command('n')
    async def getfeerate(self, fee_method=None, fee_level=None):
        """Return current suggested fee rate (in sat/kvByte), according to config
        settings or supplied parameters.
        """
        if fee_method is None:
            dyn, mempool = None, None
        elif fee_method.lower() == 'static':
            dyn, mempool = False, False
        elif fee_method.lower() == 'eta':
            dyn, mempool = True, False
        elif fee_method.lower() == 'mempool':
            dyn, mempool = True, True
        else:
            raise Exception('Invalid fee estimation method: {}'.format(fee_method))
        if fee_level is not None:
            fee_level = Decimal(fee_level)
        return self.config.fee_per_kb(dyn=dyn, mempool=mempool, fee_level=fee_level)

    @command('w')
    async def removelocaltx(self, txid):
        """Remove a 'local' transaction from the wallet, and its dependent
        transactions.
        """
        if not is_hash256_str(txid):
            raise Exception(f"{repr(txid)} is not a txid")
        height = self.wallet.get_tx_height(txid).height
        to_delete = {txid}
        if height != TX_HEIGHT_LOCAL:
            raise Exception(f'Only local transactions can be removed. '
                            f'This tx has height: {height} != {TX_HEIGHT_LOCAL}')
        to_delete |= self.wallet.get_depending_transactions(txid)
        for tx_hash in to_delete:
            self.wallet.remove_transaction(tx_hash)
        self.wallet.storage.write()

    @command('wn')
    async def get_tx_status(self, txid):
        """Returns some information regarding the tx. For now, only confirmations.
        The transaction must be related to the wallet.
        """
        if not is_hash256_str(txid):
            raise Exception(f"{repr(txid)} is not a txid")
        if not self.wallet.db.get_transaction(txid):
            raise Exception("Transaction not in wallet.")
        return {
            "confirmations": self.wallet.get_tx_height(txid).conf,
        }

    @command('')
    async def help(self):
        # for the python console
        return sorted(known_commands.keys())

    # lightning network commands
    @command('wn')
    async def add_peer(self, connection_string, timeout=20):
        await self.lnworker.add_peer(connection_string)
        return True

    @command('wpn')
    async def open_channel(self, connection_string, amount, channel_push=0, password=None):
        chan = await self.lnworker._open_channel_coroutine(connection_string, satoshis(amount), satoshis(channel_push), password)
        return chan.funding_outpoint.to_str()

    @command('wn')
    async def lnpay(self, invoice, attempts=1, timeout=10):
        return await self.lnworker._pay(invoice, attempts=attempts)

    @command('wn')
    async def addinvoice(self, requested_amount, message, expiration=3600):
        # using requested_amount because it is documented in param_descriptions
        payment_hash = await self.lnworker._add_invoice_coro(satoshis(requested_amount), message, expiration)
        invoice, direction, is_paid = self.lnworker.invoices[bh2u(payment_hash)]
        return invoice

    @command('w')
    async def nodeid(self):
        listen_addr = self.config.get('lightning_listen')
        return bh2u(self.lnworker.node_keypair.pubkey) + (('@' + listen_addr) if listen_addr else '')

    @command('w')
    async def list_channels(self):
        return list(self.lnworker.list_channels())

    @command('wn')
    async def dumpgraph(self):
        return list(map(bh2u, self.lnworker.channel_db.nodes.keys()))

    @command('n')
    async def inject_fees(self, fees):
        import ast
        self.network.config.fee_estimates = ast.literal_eval(fees)
        self.network.notify('fee')

    @command('n')
    async def clear_ln_blacklist(self):
        self.network.path_finder.blacklist.clear()

    @command('w')
    async def lightning_invoices(self):
        from .util import pr_tooltips
        out = []
        for payment_hash, (preimage, invoice, is_received, timestamp) in self.lnworker.invoices.items():
            status = self.lnworker.get_invoice_status(payment_hash)
            item = {
                'date':timestamp_to_datetime(timestamp),
                'direction': 'received' if is_received else 'sent',
                'payment_hash':payment_hash,
                'invoice':invoice,
                'preimage':preimage,
                'status':pr_tooltips[status]
            }
            out.append(item)
        return out

    @command('w')
    async def lightning_history(self):
        return self.lnworker.get_history()

    @command('wn')
    async def close_channel(self, channel_point, force=False):
        txid, index = channel_point.split(':')
        chan_id, _ = channel_id_from_funding_tx(txid, int(index))
        coro = self.lnworker.force_close_channel(chan_id) if force else self.lnworker.close_channel(chan_id)
        return await coro

    @command('wn')
    async def get_channel_ctx(self, channel_point):
        """ return the current commitment transaction of a channel """
        txid, index = channel_point.split(':')
        chan_id, _ = channel_id_from_funding_tx(txid, int(index))
        chan = self.lnworker.channels[chan_id]
        tx = chan.force_close_tx()
        return tx.as_dict()

def eval_bool(x: str) -> bool:
    if x == 'false': return False
    if x == 'true': return True
    try:
        return bool(ast.literal_eval(x))
    except:
        return bool(x)

param_descriptions = {
    'privkey': 'Private key. Type \'?\' to get a prompt.',
    'destination': 'Bitcoin address, contact or alias',
    'address': 'Bitcoin address',
    'seed': 'Seed phrase',
    'txid': 'Transaction ID',
    'pos': 'Position',
    'height': 'Block height',
    'tx': 'Serialized transaction (hexadecimal)',
    'key': 'Variable name',
    'pubkey': 'Public key',
    'message': 'Clear text message. Use quotes if it contains spaces.',
    'encrypted': 'Encrypted message',
    'amount': 'Amount to be sent (in BTC). Type \'!\' to send the maximum available.',
    'requested_amount': 'Requested amount (in BTC).',
    'outputs': 'list of ["address", amount]',
    'redeem_script': 'redeem script (hexadecimal)',
}

command_options = {
    'password':    ("-W", "Password"),
    'new_password':(None, "New Password"),
    'encrypt_file':(None, "Whether the file on disk should be encrypted with the provided password"),
    'receiving':   (None, "Show only receiving addresses"),
    'change':      (None, "Show only change addresses"),
    'frozen':      (None, "Show only frozen addresses"),
    'unused':      (None, "Show only unused addresses"),
    'funded':      (None, "Show only funded addresses"),
    'balance':     ("-b", "Show the balances of listed addresses"),
    'labels':      ("-l", "Show the labels of listed addresses"),
    'nocheck':     (None, "Do not verify aliases"),
    'imax':        (None, "Maximum number of inputs"),
    'fee':         ("-f", "Transaction fee (absolute, in BTC)"),
    'feerate':     (None, "Transaction fee rate (in sat/byte)"),
    'from_addr':   ("-F", "Source address (must be a wallet address; use sweep to spend from non-wallet address)."),
    'change_addr': ("-c", "Change address. Default is a spare address, or the source address if it's not in the wallet"),
    'nbits':       (None, "Number of bits of entropy"),
    'seed_type':   (None, "The type of seed to create, e.g. 'standard' or 'segwit'"),
    'language':    ("-L", "Default language for wordlist"),
    'passphrase':  (None, "Seed extension"),
    'privkey':     (None, "Private key. Set to '?' to get a prompt."),
    'unsigned':    ("-u", "Do not sign transaction"),
    'rbf':         (None, "Replace-by-fee transaction"),
    'locktime':    (None, "Set locktime block number"),
    'domain':      ("-D", "List of addresses"),
    'memo':        ("-m", "Description of the request"),
    'expiration':  (None, "Time in seconds"),
    'attempts':    (None, "Number of payment attempts"),
    'timeout':     (None, "Timeout in seconds"),
    'force':       (None, "Create new address beyond gap limit, if no more addresses are available."),
    'pending':     (None, "Show only pending requests."),
    'channel_push':(None, 'Push initial amount (in BTC)'),
    'expired':     (None, "Show only expired requests."),
    'paid':        (None, "Show only paid requests."),
    'show_addresses': (None, "Show input and output addresses"),
    'show_fiat':   (None, "Show fiat value of transactions"),
    'show_fees':   (None, "Show miner fees paid by transactions"),
    'year':        (None, "Show history for a given year"),
    'fee_method':  (None, "Fee estimation method to use"),
    'fee_level':   (None, "Float between 0.0 and 1.0, representing fee slider position"),
    'from_height': (None, "Only show transactions that confirmed after given block height"),
    'to_height':   (None, "Only show transactions that confirmed before given block height"),
}


# don't use floats because of rounding errors
from .transaction import tx_from_str
json_loads = lambda x: json.loads(x, parse_float=lambda x: str(Decimal(x)))
arg_types = {
    'num': int,
    'nbits': int,
    'imax': int,
    'year': int,
    'from_height': int,
    'to_height': int,
    'tx': tx_from_str,
    'pubkeys': json_loads,
    'jsontx': json_loads,
    'inputs': json_loads,
    'outputs': json_loads,
    'fee': lambda x: str(Decimal(x)) if x is not None else None,
    'amount': lambda x: str(Decimal(x)) if x != '!' else '!',
    'locktime': int,
    'fee_method': str,
    'fee_level': json_loads,
    'encrypt_file': eval_bool,
    'timeout': float,
}

config_variables = {

    'addrequest': {
        'ssl_privkey': 'Path to your SSL private key, needed to sign the request.',
        'ssl_chain': 'Chain of SSL certificates, needed for signed requests. Put your certificate at the top and the root CA at the end',
        'url_rewrite': 'Parameters passed to str.replace(), in order to create the r= part of bitcoin: URIs. Example: \"(\'file:///var/www/\',\'https://electrum.org/\')\"',
    },
    'listrequests':{
        'url_rewrite': 'Parameters passed to str.replace(), in order to create the r= part of bitcoin: URIs. Example: \"(\'file:///var/www/\',\'https://electrum.org/\')\"',
    }
}

def set_default_subparser(self, name, args=None):
    """see http://stackoverflow.com/questions/5176691/argparse-how-to-specify-a-default-subcommand"""
    subparser_found = False
    for arg in sys.argv[1:]:
        if arg in ['-h', '--help']:  # global help if no subparser
            break
    else:
        for x in self._subparsers._actions:
            if not isinstance(x, argparse._SubParsersAction):
                continue
            for sp_name in x._name_parser_map.keys():
                if sp_name in sys.argv[1:]:
                    subparser_found = True
        if not subparser_found:
            # insert default in first position, this implies no
            # global options without a sub_parsers specified
            if args is None:
                sys.argv.insert(1, name)
            else:
                args.insert(0, name)

argparse.ArgumentParser.set_default_subparser = set_default_subparser


# workaround https://bugs.python.org/issue23058
# see https://github.com/nickstenning/honcho/pull/121

def subparser_call(self, parser, namespace, values, option_string=None):
    from argparse import ArgumentError, SUPPRESS, _UNRECOGNIZED_ARGS_ATTR
    parser_name = values[0]
    arg_strings = values[1:]
    # set the parser name if requested
    if self.dest is not SUPPRESS:
        setattr(namespace, self.dest, parser_name)
    # select the parser
    try:
        parser = self._name_parser_map[parser_name]
    except KeyError:
        tup = parser_name, ', '.join(self._name_parser_map)
        msg = _('unknown parser {!r} (choices: {})').format(*tup)
        raise ArgumentError(self, msg)
    # parse all the remaining options into the namespace
    # store any unrecognized options on the object, so that the top
    # level parser can decide what to do with them
    namespace, arg_strings = parser.parse_known_args(arg_strings, namespace)
    if arg_strings:
        vars(namespace).setdefault(_UNRECOGNIZED_ARGS_ATTR, [])
        getattr(namespace, _UNRECOGNIZED_ARGS_ATTR).extend(arg_strings)

argparse._SubParsersAction.__call__ = subparser_call


def add_network_options(parser):
    parser.add_argument("-1", "--oneserver", action="store_true", dest="oneserver", default=None, help="connect to one server only")
    parser.add_argument("-s", "--server", dest="server", default=None, help="set server host:port:protocol, where protocol is either t (tcp) or s (ssl)")
    parser.add_argument("-p", "--proxy", dest="proxy", default=None, help="set proxy [type:]host[:port], where type is socks4,socks5 or http")
    parser.add_argument("--noonion", action="store_true", dest="noonion", default=None, help="do not try to connect to onion servers")
    parser.add_argument("--skipmerklecheck", action="store_true", dest="skipmerklecheck", default=False, help="Tolerate invalid merkle proofs from server")

def add_global_options(parser):
    group = parser.add_argument_group('global options')
    group.add_argument("-v", dest="verbosity", help="Set verbosity (log levels)", default='')
    group.add_argument("-V", dest="verbosity_shortcuts", help="Set verbosity (shortcut-filter list)", default='')
    group.add_argument("-D", "--dir", dest="electrum_path", help="electrum directory")
    group.add_argument("-P", "--portable", action="store_true", dest="portable", default=False, help="Use local 'electrum_data' directory")
    group.add_argument("-w", "--wallet", dest="wallet_path", help="wallet path")
    group.add_argument("--testnet", action="store_true", dest="testnet", default=False, help="Use Testnet")
    group.add_argument("--regtest", action="store_true", dest="regtest", default=False, help="Use Regtest")
    group.add_argument("--simnet", action="store_true", dest="simnet", default=False, help="Use Simnet")
    group.add_argument("--lightning", action="store_true", dest="lightning", default=False, help="Enable lightning")
    group.add_argument("--reckless", action="store_true", dest="reckless", default=False, help="Allow to enable lightning on mainnet")
    group.add_argument("-o", "--offline", action="store_true", dest="offline", default=False, help="Run offline")

def get_parser():
    # create main parser
    parser = argparse.ArgumentParser(
        epilog="Run 'electrum help <command>' to see the help for a command")
    add_global_options(parser)
    subparsers = parser.add_subparsers(dest='cmd', metavar='<command>')
    # gui
    parser_gui = subparsers.add_parser('gui', description="Run Electrum's Graphical User Interface.", help="Run GUI (default)")
    parser_gui.add_argument("url", nargs='?', default=None, help="bitcoin URI (or bip70 file)")
    parser_gui.add_argument("-g", "--gui", dest="gui", help="select graphical user interface", choices=['qt', 'kivy', 'text', 'stdio'])
    parser_gui.add_argument("-m", action="store_true", dest="hide_gui", default=False, help="hide GUI on startup")
    parser_gui.add_argument("-L", "--lang", dest="language", default=None, help="default language used in GUI")
    parser_gui.add_argument("--daemon", action="store_true", dest="daemon", default=False, help="keep daemon running after GUI is closed")
    add_network_options(parser_gui)
    add_global_options(parser_gui)
    # daemon
    parser_daemon = subparsers.add_parser('daemon', help="Run Daemon")
    parser_daemon.add_argument("-d", "--detached", action="store_true", dest="detach", default=False, help="run daemon in detached mode")
    add_network_options(parser_daemon)
    add_global_options(parser_daemon)
    # commands
    for cmdname in sorted(known_commands.keys()):
        cmd = known_commands[cmdname]
        p = subparsers.add_parser(cmdname, help=cmd.help, description=cmd.description)
        add_global_options(p)
        for optname, default in zip(cmd.options, cmd.defaults):
            a, help = command_options[optname]
            b = '--' + optname
            action = "store_true" if default is False else 'store'
            args = (a, b) if a else (b,)
            if action == 'store':
                _type = arg_types.get(optname, str)
                p.add_argument(*args, dest=optname, action=action, default=default, help=help, type=_type)
            else:
                p.add_argument(*args, dest=optname, action=action, default=default, help=help)

        for param in cmd.params:
            h = param_descriptions.get(param, '')
            _type = arg_types.get(param, str)
            p.add_argument(param, help=h, type=_type)

        cvh = config_variables.get(cmdname)
        if cvh:
            group = p.add_argument_group('configuration variables', '(set with setconfig/getconfig)')
            for k, v in cvh.items():
                group.add_argument(k, nargs='?', help=v)

    # 'gui' is the default command
    parser.set_default_subparser('gui')
    return parser
