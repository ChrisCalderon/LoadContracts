import rpctools
import rlp
import os
import stat
import re
import sys
import sha3
import time
import traceback
import socket
from .preprocessors import SimplePreProcessor

class CompilerError(Exception): pass


class Compiler(object):

    gas = '0x47e7c4'
    http_pattern = re.compile('('
                              '(?P<ip>^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'
                              '|'
                              '(?P<host>^\w+))'
                              ':(?P<port>\d{1,5}$)')
    ethaddr_pattern = re.compile('^0x[0-9a-f]{40}$')
    
    def __init__(self,
                 sources=('src',),
                 recursive=True,
                 blocktime=12.0,
                 build='build',
                 rpc_address=None,
                 chdir=None,
                 creator=None,
                 controller=None,
                 registry=None):
        '''Create a Compiler object for group of Serpent contracts.

        Arguments:
        sources -- A list of paths to search for Serpent contracts.
        recursive -- Search the sources recursively for contracts.
        blocktime -- Amount of time in seconds to wait before sending each
                     contract.
        build -- Name of the directory to write build data to.
        rpcAddress -- The address of an Ethereum node to connect to. Only paths
                      to Unix domain sockets and host:port strings are
                      acceptable.
        chdir -- Relative paths in the "sources" argument are relative to this
                 path.
        creator -- Ethereum address to use for creating the contracts.
        controller -- The contract name of the access controller.
        registry -- The contract name or address of the registry being used.

        If creator is None, the the coinbase of the Ethereum node used for RPC
        is used for contract creation.

        If controller and registry are both False, then all "import foo as bar"
        statements are replaced with address macros and serpent signatures.

        If registry is True and the rpc client is connected to the live
        Ethereum network, then the live version of the registry contract found
        at https://github.com/ChrisCalderon/Registry is used to lookup the
        Dapp's contracts' addresses. If it is not on the live network, then
        a the registry contract is added to the Dapp and used similarly.

        If creator is an address, then a json file containing the transactions
        necessary to populate your registry are dumped to a file called
        registry_txs.json in the build directory. If it is the name of a
        contract in the Dapp, then that contract has registry initialization
        transactions added to it's init function.
        '''
        if controller == False and registry == False:
            self.preprocessor = SimplePreProcessor
        else: # TODO: write two more preprocessors.
            pass
        
        ## Check whether or not to use HTTP or IPC for RPC.
        m = Compiler.http_pattern.match(rpc_address)
        if m:
            d = m.groupdict()
            ip = d['ip']
            addr = ip if ip else d['host'], int(d['port'])
            self.rpc_address = addr
            self.RpcClass = rpctools.HTTPRPCClient
        elif Compiler.is_uds_addr(rpc_address): 
            self.rpc_address = rpc_address
            self.RpcClass = rpctools.IPCRPCClient
        else:
            raise CompilerError('Invalid rpc address: {}'.format(rpc_address))
        self.rpc_client = None

        ## Check whether or not creator is a valid address.
        ## If not, set it up to be retrieved later.
        if ethaddress.match(creator):
            self.creator_address = creator # for contract creation/registry
            self.raw_creator_address = creator[2:].decode('hex') # for generating addresses
        else:
            self.creator_address = None
            self.raw_creator_address = None

        self.sources = sources
        self.recursive = recursive
        self.build = build
        self.chdir = chdir
        self.blocktime = blocktime

        self.contract_info = []
        self.shortcuts = {}

    def get_creator_address(self):
        creator = self.rpc_client.eth_coinbase()['result']
        self.creator_address = creator
        self.raw_creator_address = creator[2:].decode('hex')

    def normalize_paths(self):
        '''Makes all paths in sources point to absolute paths.'''
        # This is called at the start of the compiling process to make sure all the
        # paths exist and to make them easier to work with.
        chdir = self.chdir
        if chdir and os.path.isdir(chdir):
            chdir = self.chdir = os.path.realpath(chdir)
        elif chdir:
            raise CompilerError('chdir path does not exist: {}'.format(repr(chdir)))
        else:
            chdir = self.chdir = os.getcwd()

        sources = []
        for src_dir in self.sources:
            if not src_dir.startswith('/'):
                sources.append(os.path.join(chdir, src_dir))
            else:
                sources.append(src_dir)

        if all(map(os.path.isdir, sources)):
            self.sources = map(os.path.realpath, sources)
        else:
            bad_paths = ', '.join(map(repr,
                                      filter(lambda p: not os.path.isdir(p),
                                             sources)))
            raise CompilerError('These source paths aren\'t directories: {}'.format(bad_paths))

    @staticmethod
    def is_uds_addr(path):
        '''True if path is the location of an existing Unix Domain Socket.'''
        return os.path.isfile(path) and stat.S_ISSOCK(os.stat(path).st_mode)

    @staticmethod
    def gas_estimate(code):
        '''Estimates the gas cost of putting Serpent code on the blockchain.'''
        return Tester(code).gas_cost

    def add_source_path(self, dirname, basename):
        if basename.endswith('.se'):
            path = os.path.join(dirname, basename)
            if os.path.isfile(path):
                c_info = {'path': path}
                self.contract_info.append(c_info)

    def get_source_paths(self):
        '''Finds the paths in the supplied source directories that are Serpent contracts.'''
        for src_dir in self.sources:
            if self.recursive:
                for directory, subdirs, files in os.walk(src_dir):
                    for f in files:
                        self.add_source_path(directory, f)
            else:
                for path in os.path.listdir(src_dir):
                    self.add_source_path(src_dir, path)

    def assign_addresses(self):
        '''Computed the address for each source file.'''
        tx_nonce = self.rpc_client.eth_getTransactionCount(self.creator_address)['result']
        for i, c_info in enumerate(self.contract_info):
            seed_data = rlp.encode([self.raw_creator_address, tx_nonce + i])
            address = '0x' + sha3.sha3_256(seed_data).digest()[12:].encode('hex')
            c_info['address'] = address

    def populate_shortcuts(self):
        for c_info in self.contract_info:
            shortcut = os.path.basename(c_info['path'])[:-3]
            self.shortcuts[shortcut] = c_info

    def preprocess_with_macros(self):
        '''Default strategy for dealing with intra-Dapp contract access.'''
        for c_info in self.contract_info:
            with open(c_info['path']) as f:
                new_code = []
                for line in f:
                    m = Compiler.import_pattern.match(line)
                    if m:
                        d = m.groupdict()
                        shortcut = d['module']
                        macro = d['name']
                        address = self.shortcuts[shortcut]['address']
                        new_line = 'macro {}: {}\n'.format(macro, address)
                        new_code.append(new_line)
                    else:
                        new_code.append(line)
                c_info['new_code'] = ''.join(new_code)
        
    def preprocess_with_controller_address(self):
        pass

    def preprocess_with_controller_contract(self):
        pass

# Code that needs to be rewritten

# def broadcast_code(rpc_client, evm, creator_address, gas):
#     '''Sends compiled code to the network, and returns the address.'''
#     tx = {'from':creator_address, 'data':evm, 'gas':gas}
#     result = rpc_client.eth_sendTransaction(tx)

#     if 'error' in result:
#         code = result['error']['code']
#         message = result['error']['message']
#         if code == -32603 and message == 'Exceeds block gas limit':
#             if cost_estimate(code) < rpc.MAXGAS:
#                 time.sleep(BLOCKTIME)
#                 return broadcast_code(evm, code, fullname)
#             else:
#                 print '%s costs too much to compile!' % fullname
#         else:
#                 print 'UNKNOWN ERROR'
#                 print json.dumps(result, indent=4, sort_keys=True)
#                 print 'ABORTING'
#                 print 'code:'
#                 print code
#                 print 'DB:'
#                 print json.dumps(DB, indent=4, sort_keys=True)
#                 dump = open('load_contracts_FATAL_dump.json', 'w')
#                 print>>dump, json.dumps(DB, indent=4, sort_keys=True)
#                 sys.exit(1)
                
#     txhash = result['result']
#                 tries = 0
#     while tries < TRIES:
#                 time.sleep(BLOCKTIME)
#                 receipt = RPC.eth_getTransactionReceipt(txhash)["result"]
#         if receipt is not None:
#                 check = RPC.eth_getCode(receipt["contractAddress"])['result']
#             if check != '0x' and check[2:] in evm:
#                 return receipt["contractAddress"]
#                 tries += 1
#                 user_input = raw_input("broadcast failed after %d tries! Try again? [Y/n]" % tries)
#     if user_input in 'Yy':
#         return broadcast_code(evm, code, fullname)
#                 print 'ABORTING'
#                 print json.dumps(DB, indent=4, sort_keys=True)
#                 sys.exit(1)

            
# def optimize_deps(deps, contract_nodes):
#                 '''When a contract is specified for recompiling with -c, this is called
#     to filter the compile order of the contracts so that only the specified
#     contract, and every contract dependent on it, are recompiled.'''
#                 new_deps = [CONTRACT]

#     for i in range(deps.index(CONTRACT) + 1, len(deps)):
#                 node = deps[i]
#         for new_dep in new_deps:
#             if new_dep in contract_nodes[node]:
#                 new_deps.append(node)
#                 break

#     return new_deps
