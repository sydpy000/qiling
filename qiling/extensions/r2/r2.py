#!/usr/bin/env python3
#
# Cross Platform and Multi Architecture Advanced Binary Emulation Framework
#

import bisect
import ctypes
import json
import libr
from dataclasses import dataclass, fields
from enum import Enum
from functools import cached_property
from typing import Dict, List, Literal, Optional, Tuple, Union
from qiling.core import Qiling
from qiling.extensions import trace
from unicorn import UC_PROT_NONE, UC_PROT_READ, UC_PROT_WRITE, UC_PROT_EXEC, UC_PROT_ALL


class R2Data:
    def __init__(self, **kwargs):
        names = set([f.name for f in fields(self)])
        for k, v in kwargs.items():
            if k in names:
                setattr(self, k, v)


@dataclass(unsafe_hash=True, init=False)
class Function(R2Data):
    name: str
    offset: int
    size: int
    signature: str


@dataclass(unsafe_hash=True, init=False)
class Section(R2Data):
    name: str
    size: int
    vsize: int
    paddr: int
    vaddr: int
    perm: int

    @staticmethod
    def perm2uc(permstr: str) -> int:
        '''convert "-rwx" to unicorn const'''
        perm = UC_PROT_NONE
        dic = {
            "r": UC_PROT_READ,
            "w": UC_PROT_WRITE,
            "x": UC_PROT_EXEC,
        }
        for ch in permstr:
            perm += dic.get(ch, 0)
        return perm

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.perm = Section.perm2uc(self.perm)


@dataclass(unsafe_hash=True, init=False)
class String(R2Data):
    string: str
    vaddr: int
    paddr: int
    size: int
    length: int
    section: str = None


@dataclass(unsafe_hash=True, init=False)
class Symbol(R2Data):
    # see https://github.com/rizinorg/rizin/blob/dev/librz/include/rz_bin.h
    SymbolType = Literal["NOTYPE", "OBJ", "FUNC", "FIELD", "IFACE", "METH", "STATIC", "SECT",
                         "FILE", "COMMON", "TLS", "NUM", "LOOS", "HIOS", "LOPROC", "HIPROC", "SPCL", "UNK"]

    SymbolBind = Literal["LOCAL", "GLOBAL", "WEAK", "NUM", "LOOS", "HIOS", "LOPROC", "HIPROC", "IMPORT", "UNKNOWN"]

    name: str
    realname: str
    bind: SymbolBind
    size: int
    type: SymbolType
    vaddr: int
    paddr: int
    is_imported: bool

@dataclass(unsafe_hash=True, init=False)
class Flag(R2Data):
    offset: int
    name: str = ''
    size: int = 0

    def __lt__(self, other):
        return self.offset < other.offset

@dataclass(unsafe_hash=True, init=False)
class Xref(R2Data):
    name: str
    fromaddr: int  # from is reserved word in Python
    refname: str
    addr: int
    type: str

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.fromaddr = kwargs["from"]

    def __lt__(self, other):
        return self.fromaddr < other.fromaddr

class R2:
    def __init__(self, ql: Qiling, baseaddr=(1 << 64) - 1, loadaddr=0):
        super().__init__()
        self.ql = ql
        self.baseaddr = baseaddr  # r2 -B [baddr]   set base address for PIE binaries
        self.loadaddr = loadaddr  # r2 -m [addr]    map file at given address

        self._r2c = libr.r_core.r_core_new()
        if ql.code:
            self._setup_code(ql.code)
        else:
            self._setup_file(ql.path)

    def _setup_code(self, code: bytes):
        path = f'malloc://{len(code)}'.encode()
        fh = libr.r_core.r_core_file_open(self._r2c, path, UC_PROT_ALL, self.loadaddr)
        libr.r_core.r_core_bin_load(self._r2c, path, self.baseaddr)
        self._cmd(f'wx {code.hex()}')
        # set architecture and bits for r2 asm
        self._cmd(f"e,asm.arch={self.ql.arch.type.name.lower().removesuffix('64')},asm.bits={self.ql.arch.bits}")

    def _setup_file(self, path: str):
        path = path.encode()
        fh = libr.r_core.r_core_file_open(self._r2c, path, UC_PROT_READ | UC_PROT_EXEC, self.loadaddr)
        libr.r_core.r_core_bin_load(self._r2c, path, self.baseaddr)

    def _cmd(self, cmd: str) -> str:
        r = libr.r_core.r_core_cmd_str(
            self._r2c, ctypes.create_string_buffer(cmd.encode("utf-8")))
        return ctypes.string_at(r).decode('utf-8')

    def _cmdj(self, cmd: str) -> Union[Dict, List[Dict]]:
        return json.loads(self._cmd(cmd))
    
    def read(self, addr: int, size: int) -> bytes:
        hexstr = self._cmd(f"p8 {size} @ {addr}")
        return bytes.fromhex(hexstr)

    @cached_property
    def sections(self) -> Dict[str, Section]:
        sec_lst = self._cmdj("iSj")
        return {dic['name']: Section(**dic) for dic in sec_lst}

    @cached_property
    def strings(self) -> Dict[str, String]:
        str_lst = self._cmdj("izzj")
        return {dic['string']: String(**dic) for dic in str_lst}

    @cached_property
    def symbols(self) -> Dict[str, Symbol]:
        sym_lst = self._cmdj("isj")
        return {dic['name']: Symbol(**dic).vaddr for dic in sym_lst}

    @cached_property
    def functions(self) -> Dict[str, Function]:
        self._cmd("aaa")
        fcn_lst = self._cmdj("aflj")
        return {dic['name']: Function(**dic) for dic in fcn_lst}

    @cached_property
    def flags(self) -> List[Flag]:
        return [Flag(**dic) for dic in self._cmdj("fj")]

    @cached_property
    def xrefs(self) -> Dict[int, Xref]:
        return {dic['from']: Xref(**dic) for dic in self._cmdj("axj")}

    def at(self, addr: int) -> Tuple[Flag, int]:
        # the most suitable flag should have address <= addr
        # bisect_right find the insertion point, right side if value exists
        idx = bisect.bisect_right(self.flags, Flag(offset=addr))
        # minus 1 to find the corresponding flag
        flag = self.flags[idx - 1]
        return flag, addr - flag.offset

    def refrom(self, addr: int) -> Optional[Xref]:
        return self.xrefs.get(addr)

    def refto(self, addr: int) -> List[Xref]:
        return [xref for xref in self.xrefs.values() if xref.addr == addr]

    def enable_trace(self, mode='full'):
        # simple map from addr to flag name, cannot resolve addresses in the middle
        self.ql.loader.symsmap = {flag.offset: flag.name for flag in self.flags}
        if mode == 'full':
            trace.enable_full_trace(self.ql)
        elif mode == 'history':
            trace.enable_history_trace(self.ql)

    @cached_property
    def binfo(self) -> Dict[str, str]:
        return self._cmdj("iIj")

    @cached_property
    def baddr(self) -> int:
        return self.binfo["baddr"]

    @cached_property
    def bintype(self) -> str:
        return self.binfo["bintype"]

    def __del__(self):
        libr.r_core.r_core_free(self._r2c)
