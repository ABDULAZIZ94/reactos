'''
PROJECT:     ReactOS apisets generator
LICENSE:     MIT (https://spdx.org/licenses/MIT)
PURPOSE:     Create apiset forwarders based on Wine apisets
COPYRIGHT:   Copyright 2017,2018 Mark Jansen (mark.jansen@reactos.org)
'''

import os
import re
import sys
from collections import defaultdict
import subprocess


SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))

IGNORE_OPTIONS = ('-norelay', '-ret16', '-ret64', '-register', '-private',
                  '-noname', '-ordinal', '-i386', '-arch=', '-stub')

# Figure these out later
FUNCTION_BLACKLIST = [
    # api-ms-win-crt-utility-l1-1-0_stubs.c(6):
    # error C2169: '_abs64': intrinsic function, cannot be defined
    '_abs64',
    '_byteswap_uint64', '_byteswap_ulong', '_byteswap_ushort',
    '_rotl64', '_rotr64',
]

SPEC_HEADER = [
    '\n',
    '# This file is autogenerated by update.py\n',
    '\n'
]


class InvalidSpecError(Exception):
    def __init__(self, message):
        Exception.__init__(self, message)

class Arch(object):
    none = 0
    i386 = 1
    x86_64 = 2
    arm = 4
    arm64 = 8
    Any = i386 | x86_64 | arm | arm64

    FROM_STR = {
        'i386': i386,
        'x86_64': x86_64,
        'arm': arm,
        'arm64': arm64,
        'any': Any,
        'win32': i386,
        'win64': x86_64,
    }

    TO_STR = {
        i386: 'i386',
        x86_64: 'x86_64',
        arm: 'arm',
        arm64: 'arm64',
    }

    def __init__(self, initial=none):
        self._val = initial

    def add(self, text):
        self._val |= sum([Arch.FROM_STR[arch] for arch in text.split(',')])
        assert self._val != 0

    def has(self, val):
        return (self._val & val) != 0

    def to_str(self):
        arch_str = []
        for value in Arch.TO_STR:
            if value & self._val:
                arch_str.append(Arch.TO_STR[value])
        return ','.join(arch_str)

    def __len__(self):
        return bin(self._val).count("1")

    def __add__(self, other):
        return Arch(self._val | other._val) # pylint: disable=W0212

    def __sub__(self, other):
        return Arch(self._val & ~other._val) # pylint: disable=W0212

    def __gt__(self, other):
        return self._val > other._val       # pylint: disable=W0212

    def __lt__(self, other):
        return self._val < other._val       # pylint: disable=W0212

    def __eq__(self, other):
        return self._val == other._val      # pylint: disable=W0212

    def __ne__(self, other):
        return not self.__eq__(other)

ALIAS_DLL = {
    'ucrtbase': 'msvcrt',
    'kernelbase': 'kernel32',
    'shcore': 'shlwapi',
    'combase': 'ole32',

    # These modules cannot be linked against in ROS, so forward it
    'cfgmgr32': 'setupapi', # Forward everything
    'wmi': 'advapi32',      # Forward everything
}

class SpecEntry(object):
    def __init__(self, text, spec):
        self.spec = spec
        self._ord = None
        self.callconv = None
        self.name = None
        self.arch = Arch()
        self._forwarder = None
        self.init(text)
        self.noname = False
        if self.name == '@':
            self.noname = True
            if self._forwarder:
                self.name = self._forwarder[1]

    def init(self, text):
        tokens = re.split(r'([\s\(\)#;])', text.strip())
        tokens = [token for token in tokens if token and not token.isspace()]
        idx = []
        for comment in ['#', ';']:
            if comment in tokens:
                idx.append(tokens.index(comment))
        idx = sorted(idx)
        if idx:
            tokens = tokens[:idx[0]]
        if not tokens:
            raise InvalidSpecError(text)
        self._ord = tokens[0]
        assert self._ord == '@' or self._ord.isdigit(), text
        tokens = tokens[1:]
        self.callconv = tokens.pop(0)
        self.name = tokens.pop(0)
        while self.name.startswith(IGNORE_OPTIONS):
            if self.name.startswith('-arch='):
                self.arch.add(self.name[6:])
            elif self.name == '-i386':
                self.arch.add('i386')
            self.name = tokens.pop(0)
        if not self.arch:
            self.arch = Arch(Arch.Any)
        assert not self.name.startswith('-'), text
        if not tokens:
            return
        if tokens[0] == '(':
            assert ')' in tokens, text
            arg = tokens.pop(0)
            while True:
                arg = tokens.pop(0)
                if arg == ')':
                    break
        if not tokens:
            return
        assert len(tokens) == 1, text
        self._forwarder = tokens.pop(0).split('.', 2)
        if len(self._forwarder) == 1:
            self._forwarder = ['self', self._forwarder[0]]
        assert len(self._forwarder) in (0, 2), self._forwarder
        if self._forwarder[0] in ALIAS_DLL:
            self._forwarder[0] = ALIAS_DLL[self._forwarder[0]]

    def resolve_forwarders(self, module_lookup, try_modules):
        if self._forwarder:
            assert self._forwarder[1] == self.name, '{}:{}'.format(self._forwarder[1], self.name)
        if self.noname and self.name == '@':
            return 0    # cannot search for this function
        self._forwarder = []
        self.arch = Arch()
        for module_name in try_modules:
            assert module_name in module_lookup, module_name
            module = module_lookup[module_name]
            fwd_arch = module.find_arch(self.name)
            callconv = module.find_callconv(self.name)
            if fwd_arch:
                self.arch = fwd_arch
                self._forwarder = [module_name, self.name]
                self.callconv = callconv
                return 1
        return 0

    def extra_forwarders(self, function_lookup, module_lookup):
        if self._forwarder:
            return 1
        if self.noname and self.name == '@':
            return 0    # cannot search for this function
        lst = function_lookup.get(self.name, None)
        if lst:
            modules = list(set([func.spec.name for func in lst]))
            if len(modules) > 1:
                mod = None
                arch = Arch()
                for module in modules:
                    mod_arch = module_lookup[module].find_arch(self.name)
                    if mod is None or mod_arch > arch:
                        mod = module
                        arch = mod_arch
                modules = [mod]
            mod = modules[0]
            self._forwarder = [mod, self.name]
            mod = module_lookup[mod]
            self.arch = mod.find_arch(self.name)
            self.callconv = mod.find_callconv(self.name)
            return 1
        return 0

    def forwarder_module(self):
        if self._forwarder:
            return self._forwarder[0]

    def forwarder(self):
        if self._forwarder:
            return 1
        return 0

    def write(self, spec_file):
        name = self.name
        opts = ''
        estimate_size = 0
        if self.noname:
            opts = '{} -noname'.format(opts)
        if self.name == '@':
            assert self._ord != '@'
            name = 'Ordinal' + self._ord
        if not self._forwarder:
            spec_file.write('{} stub{} {}\n'.format(self._ord, opts, name))
            estimate_size += 0x1000
        else:
            assert self.arch != Arch(), self.name
            args = '()'
            callconv = 'stdcall'
            fwd = '.'.join(self._forwarder)
            name = self.name if not self.noname else '@'
            arch = self.arch
            if self.callconv == 'extern':
                args = ''
                callconv = 'extern'
                if arch.has(Arch.x86_64):
                    fwd = '{}.__imp_{}'.format(*self._forwarder)
                    self.arch = arch - Arch(Arch.x86_64)
                    estimate_size += self.write(spec_file)
                    self.arch = arch
                    arch = Arch(Arch.x86_64)
                else:
                    fwd = '{}._imp__{}'.format(*self._forwarder)
            if arch != Arch(Arch.Any):
                opts = '{} -arch={}'.format(opts, arch.to_str())
            spec_file.write('{ord} {cc}{opts} {name}{args} {fwd}\n'.format(ord=self._ord,
                                                                           cc=callconv,
                                                                           opts=opts,
                                                                           name=name,
                                                                           args=args,
                                                                           fwd=fwd))
            estimate_size += 0x100
        return estimate_size



class SpecFile(object):
    def __init__(self, fullpath, name):
        self._path = fullpath
        self.name = name
        self._entries = []
        self._functions = defaultdict(list)
        self._estimate_size = 0

    def parse(self):
        with open(self._path, 'rb') as specfile:
            for line in specfile.readlines():
                if line:
                    try:
                        entry = SpecEntry(line, self)
                        self._entries.append(entry)
                        self._functions[entry.name].append(entry)
                    except InvalidSpecError:
                        pass
        return (sum([entry.forwarder() for entry in self._entries]), len(self._entries))

    def add_functions(self, function_lookup):
        for entry in self._entries:
            function_lookup[entry.name].append(entry)

    def find(self, name):
        return self._functions.get(name, None)

    def find_arch(self, name):
        functions = self.find(name)
        arch = Arch()
        if functions:
            for func in functions:
                arch += func.arch
        return arch

    def find_callconv(self, name):
        functions = self.find(name)
        callconv = None
        if functions:
            for func in functions:
                if not callconv:
                    callconv = func.callconv
                elif callconv != func.callconv:
                    assert callconv != 'extern', 'Cannot have data/function with same name'
                    callconv = func.callconv
        return callconv

    def resolve_forwarders(self, module_lookup):
        modules = self.forwarder_modules()
        total = 0
        for entry in self._entries:
            total += entry.resolve_forwarders(module_lookup, modules)
        return (total, len(self._entries))

    def extra_forwarders(self, function_lookup, module_lookup):
        total = 0
        for entry in self._entries:
            total += entry.extra_forwarders(function_lookup, module_lookup)
        return (total, len(self._entries))

    def forwarder_modules(self):
        modules = defaultdict(int)
        for entry in self._entries:
            module = entry.forwarder_module()
            if module:
                modules[module] += 1
        return sorted(modules, key=modules.get, reverse=True)

    def write(self, spec_file):
        written = set(FUNCTION_BLACKLIST)
        self._estimate_size = 0
        for entry in self._entries:
            if entry.name not in written:
                self._estimate_size += entry.write(spec_file)
                written.add(entry.name)

    def write_cmake(self, cmakelists, baseaddress):
        seen = set()
        # ntdll and kernel32 are linked against everything, self = internal,
        # we cannot link cfgmgr32 and wmi?
        ignore = ['ntdll', 'kernel32', 'self', 'cfgmgr32', 'wmi']
        forwarders = self.forwarder_modules()
        fwd_strings = [x for x in forwarders if not (x in seen or x in ignore or seen.add(x))]
        fwd_strings = ' '.join(fwd_strings)
        name = self.name
        baseaddress = '0x{:8x}'.format(baseaddress)
        cmakelists.write('add_apiset({} {} {})\n'.format(name, baseaddress, fwd_strings))
        return self._estimate_size



def generate_specnames(dll_dir):
    win32 = os.path.join(dll_dir, 'win32')
    for dirname in os.listdir(win32):
        fullpath = os.path.join(win32, dirname, dirname + '.spec')
        if not os.path.isfile(fullpath):
            if '.' in dirname:
                fullpath = os.path.join(win32, dirname, dirname.rsplit('.', 1)[0] + '.spec')
                if not os.path.isfile(fullpath):
                    continue
            else:
                continue
        yield (fullpath, dirname)
    # Special cases
    yield (os.path.join(dll_dir, 'ntdll', 'def', 'ntdll.spec'), 'ntdll')
    yield (os.path.join(dll_dir, 'appcompat', 'apphelp', 'apphelp.spec'), 'apphelp')
    yield (os.path.join(dll_dir, '..', 'win32ss', 'user', 'user32', 'user32.spec'), 'user32')
    yield (os.path.join(dll_dir, '..', 'win32ss', 'gdi', 'gdi32', 'gdi32.spec'), 'gdi32')

def run(wineroot):
    wine_apisets = []
    ros_modules = []

    module_lookup = {}
    function_lookup = defaultdict(list)

    version = subprocess.check_output(["git", "describe"], cwd=wineroot).strip()

    print 'Reading Wine apisets for', version
    wine_apiset_path = os.path.join(wineroot, 'dlls')
    for dirname in os.listdir(wine_apiset_path):
        if not dirname.startswith('api-'):
            continue
        if not os.path.isdir(os.path.join(wine_apiset_path, dirname)):
            continue
        fullpath = os.path.join(wine_apiset_path, dirname, dirname + '.spec')
        spec = SpecFile(fullpath, dirname)
        wine_apisets.append(spec)

    print 'Parsing Wine apisets,',
    total = (0, 0)
    for apiset in wine_apisets:
        total = tuple(map(sum, zip(apiset.parse(), total)))
    print 'found', total[0], '/', total[1], 'forwarders'

    print 'Reading ReactOS modules'
    for fullpath, dllname in generate_specnames(os.path.dirname(SCRIPT_DIR)):
        spec = SpecFile(fullpath, dllname)
        ros_modules.append(spec)

    print 'Parsing ReactOS modules'
    for module in ros_modules:
        module.parse()
        assert module.name not in module_lookup, module.name
        module_lookup[module.name] = module
        module.add_functions(function_lookup)

    print 'First pass, resolving forwarders,',
    total = (0, 0)
    for apiset in wine_apisets:
        total = tuple(map(sum, zip(apiset.resolve_forwarders(module_lookup), total)))
    print 'found', total[0], '/', total[1], 'forwarders'

    print 'Second pass, searching extra forwarders,',
    total = (0, 0)
    for apiset in wine_apisets:
        total = tuple(map(sum, zip(apiset.extra_forwarders(function_lookup, module_lookup), total)))
    print 'found', total[0], '/', total[1], 'forwarders'

    print 'Writing apisets'
    for apiset in wine_apisets:
        with open(os.path.join(SCRIPT_DIR, apiset.name + '.spec'), 'wb') as out_spec:
            out_spec.writelines(SPEC_HEADER)
            apiset.write(out_spec)

    print 'Writing CMakeLists.txt'
    with open(os.path.join(SCRIPT_DIR, 'CMakeLists.txt.in'), 'rb') as template:
        data = template.read()
        data = data.replace('%WINE_GIT_VERSION%', version)
    baseaddress = 0x60000000
    with open(os.path.join(SCRIPT_DIR, 'CMakeLists.txt'), 'wb') as cmakelists:
        cmakelists.write(data)
        for apiset in wine_apisets:
            baseaddress += apiset.write_cmake(cmakelists, baseaddress)
            baseaddress += (0x10000 - baseaddress) % 0x10000
    print 'Done'

def main(paths):
    for path in paths:
        if path:
            run(path)
            return
    print 'No path specified,'
    print 'either pass it as argument, or set the environment variable "WINE_SRC_ROOT"'

if __name__ == '__main__':
    main(sys.argv[1:] + [os.environ.get('WINE_SRC_ROOT')])
