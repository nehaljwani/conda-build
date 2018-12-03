try:
    from collections.abc import Hashable
except ImportError:
    from collections import Hashable
import hashlib
import json
import os
from os.path import join
import sys
import threading

from six import string_types

# TODO :: Remove all use of pyldd
# Currently we verify the output of each against the other
from .pyldd import inspect_linkages as inspect_linkages_pyldd
# lief cannot handle files it doesn't know about gracefully
from .pyldd import codefile_type as codefile_type_pyldd
codefile_type = codefile_type_pyldd
have_lief = False
try:
    import lief
    have_lief = True
except:
    pass


def is_string(s):
    try:
        return isinstance(s, basestring)
    except NameError:
        return isinstance(s, str)


# Some functions can operate on either file names
# or an already loaded binary. Generally speaking
# these are to be avoided, or if not avoided they
# should be passed a binary when possible as that
# will prevent having to parse it multiple times.
def ensure_binary(file):
    if not is_string(file):
        return file
    else:
        try:
            if not os.path.exists(file):
                return []
            return lief.parse(file)
        except:
            print('WARNING: liefldd: failed to ensure_binary({})'.format(file))
    return None


def nm(filename):
    """ Return symbols from *filename* binary """
    done = False
    try:
        binary = lief.parse(filename)  # Build an abstract binary
        symbols = binary.symbols

        if len(symbols) > 0:
            for symbol in symbols:
                print(dir(symbol))
                print(symbol)
                done = True
    except:
        pass
    if not done:
        print("No symbols found")


def codefile_type_liefldd(file, skip_symlinks=True):
    binary = ensure_binary(file)
    result = None
    if binary:
        if binary.format == lief.EXE_FORMATS.PE:
            if lief.PE.DLL_CHARACTERISTICS:
                if binary.header.characteristics & lief.PE.HEADER_CHARACTERISTICS.DLL:
                    result = 'DLLfile'
                else:
                    result = 'EXEfile'
        elif binary.format == lief.EXE_FORMATS.MACHO:
            result = 'machofile'
        elif binary.format == lief.EXE_FORMATS.ELF:
            result = 'elffile'
    return result


if have_lief:
    codefile_type = codefile_type_liefldd


def _trim_sysroot(sysroot):
    while sysroot.endswith('/') or sysroot.endswith('\\'):
        sysroot = sysroot[:-1]
    return sysroot


def get_libraries(file):
    result = []
    binary = ensure_binary(file)
    if binary:
        if binary.format == lief.EXE_FORMATS.PE:
            result = binary.libraries
        else:
            # LIEF returns LC_ID_DYLIB name @rpath/libbz2.dylib in binary.libraries. Strip that.
            binary_name = None
            if binary.format == lief.EXE_FORMATS.MACHO and binary.has_rpath:
                binary_name = [command.name for command in binary.commands
                               if command.command == lief.MachO.LOAD_COMMAND_TYPES.ID_DYLIB]
                binary_name = binary_name[0] if len(binary_name) else None
            result = [l if isinstance(l, string_types) else l.name for l in binary.libraries]
            if binary.format == lief.EXE_FORMATS.MACHO:
                result = [from_os_varnames(binary, l) for l in result
                          if not (binary_name and l.endswith(binary_name))]
    return result


def get_rpaths(file, exe_dirname, envroot, windows_root=''):
    binary = ensure_binary(file)
    rpaths = []
    if binary:
        if binary.format == lief.EXE_FORMATS.PE:
            if not exe_dirname:
                # log = get_logger(__name__)
                # log.warn("Windows library file at %s could not be inspected for rpath.  If it's a standalone "
                #          "thing from a 3rd party, this is safe to ignore." % file)
                return []
            # To allow the unix-y rpath code to work we consider
            # exes as having rpaths of env + CONDA_WINDOWS_PATHS
            # and consider DLLs as having no rpaths.
            # .. scratch that, we don't pass exes in as the root
            # entries so we just need rpaths for all files and
            # not to apply them transitively.
            # https://docs.microsoft.com/en-us/windows/desktop/dlls/dynamic-link-library-search-order
            if exe_dirname:
                rpaths.append(exe_dirname.replace('\\', '/'))
            if windows_root:
                rpaths.append('/'.join((windows_root, "System32")))
                rpaths.append(windows_root)
            if envroot:
                # and not lief.PE.HEADER_CHARACTERISTICS.DLL in binary.header.characteristics_list:
                rpaths.extend(list(_get_path_dirs(envroot)))
# This only returns the first entry.
#        elif binary.format == lief.EXE_FORMATS.MACHO and binary.has_rpath:
#            rpaths = binary.rpath.path.split(':')
        elif binary.format == lief.EXE_FORMATS.MACHO and binary.has_rpath:
            rpaths.extend([command.path.rstrip('/') for command in binary.commands
                      if command.command == lief.MachO.LOAD_COMMAND_TYPES.RPATH])
        elif binary.format == lief.EXE_FORMATS.ELF:
            if binary.type == lief.ELF.ELF_CLASS.CLASS32 or binary.type == lief.ELF.ELF_CLASS.CLASS64:
                dynamic_entries = binary.dynamic_entries
                # runpath takes precedence over rpath on GNU/Linux.
                rpaths_colons = [e.runpath for e in dynamic_entries if e.tag == lief.ELF.DYNAMIC_TAGS.RUNPATH]
                if not len(rpaths_colons):
                    rpaths_colons = [e.rpath for e in dynamic_entries if e.tag == lief.ELF.DYNAMIC_TAGS.RPATH]
                for rpaths_colon in rpaths_colons:
                    rpaths.extend(rpaths_colon.split(':'))
    return [from_os_varnames(binary, rpath) for rpath in rpaths]


def get_runpaths(file):
    binary = ensure_binary(file)
    rpaths = []
    if binary:
        if (binary.format == lief.EXE_FORMATS.ELF and  # noqa
            (binary.type == lief.ELF.ELF_CLASS.CLASS32 or binary.type == lief.ELF.ELF_CLASS.CLASS64)):
            dynamic_entries = binary.dynamic_entries
            rpaths_colons = [e.runpath for e in dynamic_entries if e.tag == lief.ELF.DYNAMIC_TAGS.RUNPATH]
            for rpaths_colon in rpaths_colons:
                rpaths.extend(rpaths_colon.split(':'))
    return [from_os_varnames(binary, rpath) for rpath in rpaths]


# TODO :: Consider memoizing instead of repeatedly scanning
# TODO :: libc.so/libSystem.dylib when inspect_linkages(recurse=True)
def _inspect_linkages_this(filename, sysroot='', arch='native'):
    '''

    :param filename:
    :param sysroot:
    :param arch:
    :return:
    '''

    if not os.path.exists(filename):
        return None, [], []
    sysroot = _trim_sysroot(sysroot)
    try:
        binary = lief.parse(filename)
        # Future lief has this:
        # json_data = json.loads(lief.to_json_from_abstract(binary))
        json_data = json.loads(lief.to_json(binary))
        if json_data:
            return filename, json_data['imported_libraries'], json_data['imported_libraries']
    except:
        print('WARNING: liefldd: failed _inspect_linkages_this({})'.format(filename))

    return None, [], []


def to_os_varnames(binary, input_):
    """Don't make these functions - they are methods to match the API for elffiles."""
    if binary.format == lief.EXE_FORMATS.MACHO:
        return input_.replace('$SELFDIR', '@loader_path')     \
                     .replace('$EXEDIR', '@executable_path')  \
                     .replace('$RPATH', '@rpath')
    elif binary.format == lief.EXE_FORMATS.ELF:
        if binary.ehdr.sz_ptr == 8:
            libdir = '/lib64'
        else:
            libdir = '/lib'
        return input.replace('$SELFDIR', '$ORIGIN') \
            .replace(libdir, '$LIB')


def from_os_varnames(binary, input_):
    """Don't make these functions - they are methods to match the API for elffiles."""
    if binary.format == lief.EXE_FORMATS.MACHO:
        return input_.replace('@loader_path', '$SELFDIR')     \
                     .replace('@executable_path', '$EXEDIR')  \
                     .replace('@rpath', '$RPATH')
    elif binary.format == lief.EXE_FORMATS.ELF:
        if binary.type == lief.ELF.ELF_CLASS.CLASS64:
            libdir = '/lib64'
        else:
            libdir = '/lib'
        return input_.replace('$ORIGIN', '$SELFDIR')  \
            .replace('$LIB', libdir)
    elif binary.format == lief.EXE_FORMATS.PE:
        return input_


# TODO :: Use conda's version of this (or move the constant strings into constants.py
def _get_path_dirs(prefix):
    yield join(prefix, 'Library', 'mingw-w64', 'bin')
    yield join(prefix, 'Library', 'usr', 'bin')
    yield join(prefix, 'Library', 'bin')
    yield join(prefix, 'Scripts')
    yield join(prefix, 'bin')


def get_uniqueness_key(file):
    binary = ensure_binary(file)
    if binary.format == lief.EXE_FORMATS.MACHO:
        return binary.name
    elif (binary.format == lief.EXE_FORMATS.ELF
         and  # noqa
         (binary.type == lief.ELF.ELF_CLASS.CLASS32 or binary.type == lief.ELF.ELF_CLASS.CLASS64)):
        dynamic_entries = binary.dynamic_entries
        result = [e.name for e in dynamic_entries if e.tag == lief.ELF.DYNAMIC_TAGS.SONAME]
        if result:
            return result[0]
        return binary.name
    return binary.name


def _get_resolved_location(codefile,
                           unresolved,
                           exedir,
                           selfdir,
                           rpaths_transitive,
                           LD_LIBRARY_PATH='',
                           default_paths=[],
                           sysroot='',
                           resolved_rpath=None):
    '''
       From `man ld.so`

       When resolving shared object dependencies, the dynamic linker first inspects each dependency
       string to see if it contains a slash (this can occur if a shared object pathname containing
       slashes was specified at link time).  If a slash is found, then the dependency string is
       interpreted as a (relative or absolute) pathname, and the shared object is loaded using that
       pathname.

       If a shared object dependency does not contain a slash, then it is searched for in the
       following order:

       o Using the directories specified in the DT_RPATH dynamic section attribute of the binary
         if present and DT_RUNPATH attribute does not exist.  Use of DT_RPATH is deprecated.

       o Using the environment variable LD_LIBRARY_PATH (unless the executable is being run in
         secure-execution mode; see below).  in which case it is ignored.

       o Using the directories specified in the DT_RUNPATH dynamic section attribute of the
         binary if present. Such directories are searched only to find those objects required
         by DT_NEEDED (direct dependencies) entries and do not apply to those objects' children,
         which must themselves have their own DT_RUNPATH entries. This is unlike DT_RPATH,
         which is applied to searches for all children in the dependency tree.

       o From the cache file /etc/ld.so.cache, which contains a compiled list of candidate
         shared objects previously found in the augmented library path. If, however, the binary
         was linked with the -z nodeflib linker option, shared objects in the default paths are
         skipped. Shared objects installed in hardware capability directories (see below) are
         preferred to other shared objects.

       o In the default path /lib, and then /usr/lib. (On some 64-bit architectures, the default
         paths for 64-bit shared objects are /lib64, and then /usr/lib64.)  If the binary was
         linked with the -z nodeflib linker option, this step is skipped.

       Returns a tuple of resolved location, rpath_used, in_sysroot
    '''
    rpath_result = None
    found = False
    ld_library_paths = [] if not LD_LIBRARY_PATH else LD_LIBRARY_PATH.split(':')
    if unresolved.startswith('$RPATH'):
        these_rpaths = [resolved_rpath] if resolved_rpath else \
                        rpaths_transitive + \
                        ld_library_paths + \
                        [dp.replace('$SYSROOT/', sysroot) for dp in default_paths]
        for rpath in these_rpaths:
            resolved = unresolved.replace('$RPATH', rpath) \
                                 .replace('$SELFDIR', selfdir) \
                                 .replace('$EXEDIR', exedir)
            exists = os.path.exists(resolved)
            exists_sysroot = exists and sysroot and resolved.startswith(sysroot)
            if resolved_rpath or exists or exists_sysroot:
                rpath_result = rpath
                found = True
                break
        if not found:
            # Return the so name so that it can be warned about as missing.
            return unresolved, None, False
    elif any(a in unresolved for a in ('$SELFDIR', '$EXEDIR')):
        resolved = unresolved.replace('$SELFDIR', selfdir) \
                             .replace('$EXEDIR', exedir)
        exists = os.path.exists(resolved)
        exists_sysroot = exists and sysroot and resolved.startswith(sysroot)
    else:
        if unresolved.startswith('/'):
            return unresolved, None, False
        else:
            return os.path.join(selfdir, unresolved), None, False

    return resolved, rpath_result, exists_sysroot


# TODO :: Consider returning a tree structure or a dict when recurse is True?
def inspect_linkages_lief(filename, resolve_filenames=True, recurse=True,
                          sysroot='', envroot='', arch='native'):
    # Already seen is partly about implementing single SONAME
    # rules and its appropriateness on macOS is TBD!
    already_seen = set()
    exedir = os.path.dirname(filename)
    binary = lief.parse(filename)
    todo = [[filename, binary]]

    default_paths = []
    if binary.format == lief.EXE_FORMATS.ELF:
        default_paths = ['$SYSROOT/lib', '$SYSROOT/usr/lib']
        if binary.type == lief.ELF.ELF_CLASS.CLASS64:
            default_paths.extend(['$SYSROOT/lib64', '$SYSROOT/usr/lib64'])
    elif binary.format == lief.EXE_FORMATS.MACHO:
        default_paths = ['$SYSROOT/usr/lib']
    elif binary.format == lief.EXE_FORMATS.PE:
        # We do not include C:\Windows nor C:\Windows\System32 in this list. They are added in
        # get_rpaths() instead since we need to carefully control the order.
        default_paths = ['$SYSROOT/System32/Wbem', '$SYSROOT/System32/WindowsPowerShell/v1.0']
    results = set()
    rpaths_by_binary = dict()
    parents_by_filename = dict({filename: None})
    while todo:
        for element in todo:
            todo.pop(0)
            filename2 = element[0]
            binary = element[1]
            uniqueness_key = get_uniqueness_key(binary)
            if uniqueness_key not in already_seen:
                parent_exe_dirname = None
                if binary.format == lief.EXE_FORMATS.PE:
                    tmp_filename = filename2
                    while tmp_filename:
                        if not parent_exe_dirname and codefile_type(tmp_filename) == 'EXEfile':
                            parent_exe_dirname = os.path.dirname(tmp_filename)
                        tmp_filename = parents_by_filename[tmp_filename]
                else:
                    parent_exe_dirname = exedir
                rpaths_by_binary[filename2] = get_rpaths(binary,
                                                         parent_exe_dirname,
                                                         envroot.replace(os.sep, '/'),
                                                         sysroot)
                tmp_filename = filename2
                rpaths_transitive = []
                if binary.format == lief.EXE_FORMATS.PE:
                    rpaths_transitive = rpaths_by_binary[tmp_filename]
                else:
                    while tmp_filename:
                        rpaths_transitive[:0] = rpaths_by_binary[tmp_filename]
                        tmp_filename = parents_by_filename[tmp_filename]
                libraries = get_libraries(binary)
                if filename2 in libraries:  # Happens on macOS, leading to cycles.
                    libraries.remove(filename2)
                # RPATH is implicit everywhere except macOS, make it explicit to simplify things.
                these_orig = [('$RPATH/' + lib if not lib.startswith('/') and not lib.startswith('$') and  # noqa
                               binary.format != lief.EXE_FORMATS.MACHO else lib)
                              for lib in libraries]
                for orig in these_orig:
                    resolved = _get_resolved_location(binary,
                                                      orig,
                                                      exedir,
                                                      exedir,
                                                      rpaths_transitive=rpaths_transitive,
                                                      default_paths=default_paths,
                                                      sysroot=sysroot)
                    if resolve_filenames:
                        results.add(resolved[0])
                        parents_by_filename[resolved[0]] = filename2
                    else:
                        results.add(orig)
                    if recurse:
                        if os.path.exists(resolved[0]):
                            todo.append([resolved[0], lief.parse(resolved[0])])
                already_seen.add(get_uniqueness_key(binary))
    return results


def get_linkages(filename, resolve_filenames=True, recurse=True,
                 sysroot='', envroot='', arch='native'):
    # When we switch to lief, want to ensure these results do not change.
    # if have_lief:
    if codefile_type(filename) not in ('DLLfile', 'EXEfile'):
        result_pyldd = inspect_linkages_pyldd(filename, resolve_filenames=resolve_filenames, recurse=recurse,
                                              sysroot=sysroot, arch=arch)
    if not have_lief:
        return result_pyldd
    if codefile_type(filename) not in ('DLLfile', 'EXEfile'):
        result_pyldd = inspect_linkages_pyldd(filename, resolve_filenames=resolve_filenames, recurse=recurse,
                                              sysroot=sysroot, arch=arch)
        # We do not support Windows yet with pyldd.
        if set(result_lief) != set(result_pyldd):
            print("WARNING: Disagreement in get_linkages(filename={}, resolve_filenames={}, recurse={}, sysroot={}, envroot={}, arch={}):\n lief: {}\npyldd: {}\n  (using lief)".
                  format(filename, resolve_filenames, recurse, sysroot, envroot, arch, result_lief, result_pyldd))
    return result_lief


def get_imports(file, arch='native'):
    binary = ensure_binary(file)
    return [str(i) for i in binary.imported_functions]


'''
    * The n_type field really contains three fields:
    *	unsigned char N_STAB:3,
    *		      N_PEXT:1,
    *		      N_TYPE:3,
    *		      N_EXT:1;
'''
import ctypes
c_uint8 = ctypes.c_uint8
c_uint32 = ctypes.c_uint32

# This is likely all utter rubbish!
# http://mirror.informatimago.com/next/developer.apple.com/documentation/DeveloperTools/Conceptual/MachORuntime/8rt_file_format/chapter_10_section_33.html

class n_type_bits( ctypes.LittleEndianStructure ):
    '''
    ("N_STAB",     c_uint8, 3 ),
    ("N_PEXT",     c_uint8, 1 ),
    ("N_TYPE",     c_uint8, 3 ),
    ("N_EXT",      c_uint8, 1 ),
    '''
    _fields_ = [
                ("N_EXT",      c_uint8, 1 ),
                ("N_TYPE",     c_uint8, 3 ),
                ("N_PEXT",     c_uint8, 1 ),
                ("N_STAB",     c_uint8, 3 ),
            ]

class n_type( ctypes.Union ):
    _anonymous_ = ("bit",)
    _fields_ = [
                ("bit",    n_type_bits ),
                ("asByte", c_uint8     )
            ]


class n_stab_bits( ctypes.LittleEndianStructure ):
    '''
    ("N_VALUE",    c_uint32, 8 ),
    ("N_DESC",     c_uint32, 8 ),
    ("N_SEXT",     c_uint32, 8 ),
    ("N_TYPE",     c_uint32, 8 ),
    '''
    _fields_ = [
                ("N_TYPE",     c_uint32, 8 ),
                ("N_SECT",     c_uint32, 8 ),
                ("N_DESC",     c_uint32, 8 ),
                ("N_VALUE",    c_uint32, 8 ),
            ]

class n_stab( ctypes.Union ):
    _anonymous_ = ("bit",)
    _fields_ = [
                ("bit",      n_stab_bits ),
                ("asUint32", c_uint32    )
            ]


def get_static_lib_exports(file):
    # References:
    # https://github.com/bminor/binutils-gdb/tree/master/bfd/archive.c
    # https://en.wikipedia.org/wiki/Ar_(Unix)
    # https://web.archive.org/web/20100314154747/http://www.microsoft.com/whdc/system/platform/firmware/PECOFF.mspx
    results = []
    with open(file, 'rb') as f:
        print("Archive file {}".format(file))
        index = 0
        content = f.read()
        fsize = len(content)
        signature, = struct.unpack('<8s', content[index:8])
        if signature != b'!<arch>\n':
            print("ERROR: {} is not an archive".format(file))
            return results
        index += 8
        '''
        0   16  File identifier                 ASCII
        16  12 	File modification timestamp     Decimal
        28  6   Owner ID                        Decimal
        34  6   Group ID                        Decimal
        40  8   File mode                       Octal
        48  10  File size in bytes              Decimal
        58  2   Ending characters               0x60 0x0A 
        '''
        header_fmt = '<16s 12s 6s 6s 8s 10s 2s'
        header_sz = struct.calcsize(header_fmt)
        while (index + header_sz) < fsize:
            if index & 1:
                index += 1
            name, modified, owner, group, mode, size, ending = \
                struct.unpack(header_fmt, content[index:index + header_sz])
            try:
                size = int(size)
            except:
                print('ERROR: {} has non-integral size of {}'.format(id, size))
                return results
            name_len = 0  # File data in BSD format archives begin with a name of this length. 
            if name.startswith(b'#1/'):
                type = 'BSD'
                name_len = int(name[3:])
                name, = struct.unpack('<'+str(name_len)+'s', content[index + header_sz:index + header_sz + name_len])
                if b'\x00' in name:
                    name = name[:name.find(b'\x00')]
            elif name.startswith(b'//'):
                type = 'GNU_TABLE'
            elif name.strip() == b'/':
                type = 'GNU_SYMBOLS'
            elif name.startswith(b'/'):
                type = 'GNU'
            else:
                type = 'NORMAL'
            print("index={}, name={}, ending={}, size={}, type={}".format(index, name, ending, size, type))
            index += header_sz + name_len
            if type == 'GNU_SYMBOLS':
                # Reference:
                nsymbols, = struct.unpack('>I', content[index:index+4])
                return [fname.decode('utf-8')
                        for fname in content[index+4+(nsymbols*4):index+size].split(b'\x00')[:nsymbols]]
            elif name.startswith(b'__.SYMDEF'):
                # Reference:
                # http://www.manpagez.com/man/5/ranlib/
                # https://opensource.apple.com/source/cctools/cctools-921/misc/libtool.c.auto.html
                # https://opensource.apple.com/source/cctools/cctools-921/misc/nm.c.auto.html
                # https://developer.apple.com/documentation/kernel/nlist_64/1583944-n_type?language=objc

                size_ranlib_structs, = struct.unpack('<I', content[index:index+4])
                # Each of the ranlib structures consists of a zero based offset into the next
                # section (a string table of symbols) and an offset from the beginning of
                # the archive to the start of the archive file which defines the symbol
                nsymbols = size_ranlib_structs // 8
                size_string_table, = struct.unpack('<I', content[index+4+(nsymbols*8):index+4+4+(nsymbols*8)])
                ranlib_structs = []
                for i in range(nsymbols):
                    ranlib_struct1, ranlib_struct2 = struct.unpack('<II', content[index+4+4+(i*8):index+4+4+((i+1)*8)])
                    ranlib_structs.append((ranlib_struct1, ranlib_struct2))
                syms = [fname.decode('utf-8')
                        for fname in content[index+4+4+(nsymbols*8):index+4+4+(nsymbols*8)+size_string_table].split(b'\x00')[:nsymbols]]
                filtered_syms = []
                filtered_ranlib_structs = []
                for i in range(nsymbols):
                    sym = syms[i]
                    ranlib_struct = ranlib_structs[i]
                    print("{} :: {}, {}".format(syms[i], hex(ranlib_struct[0]), hex(ranlib_struct[1])))
                    ntype = n_type()
                    ntype.asByte = ranlib_struct[0] & 0xff # (maybe & 0xff000000 >> 24?)
                    if ntype.N_STAB:
                        nstab = n_stab()
                        nstab.asUint32 = ranlib_struct[0]
                        # If any of these 3 bits are set, the symbol is a symbolic debugging table (stab) entry. In that case, the
                        # entire n_type field is interpreted as a stab value. See /usr/include/mach-o/stab.h for valid stab values.
                        #
                        print('{} :: N_STAB :: N_TYPE {}, N_SECT {}, N_DESC {}, N_VALUE {}'.format(sym, hex(nstab.N_TYPE), hex(nstab.N_SECT), hex(nstab.N_DESC), hex(nstab.N_VALUE)))
                        if nstab.N_DESC != 0x20 and nstab.N_DESC != 0x22:
                            # I've seen 0x6 here (only once in libperl.a)
                            print("N_DESC = {} (N_TYPE really?) not N_GSYM(0x20) nor N_FNAME(0x22)".format(nstab.N_DESC)) 

                        '''
                        /*
                        * Symbolic debugger symbols.  The comments give the conventional use for
                        *    
                        *      .stabs "n_name", n_type, n_sect, n_desc, n_value
                        * 
                        * where n_type is the defined constant and not listed in the comment.  Other
                        * fields not listed are zero. n_sect is the section ordinal the entry is
                        * refering to.
                        */
                        #define N_GSYM  0x20    /* global symbol: name,,NO_SECT,type,0 */
                        #define N_FNAME 0x22    /* procedure name (f77 kludge): name,,NO_SECT,0,0 */
                        #define N_FUN   0x24    /* procedure: name,,n_sect,linenumber,address */
                        #define N_STSYM 0x26    /* static symbol: name,,n_sect,type,address */
                        #define N_LCSYM 0x28    /* .lcomm symbol: name,,n_sect,type,address */
                        #define N_BNSYM 0x2e    /* begin nsect sym: 0,,n_sect,0,address */
                        #define N_OPT   0x3c    /* emitted with gcc2_compiled and in gcc source */
                        #define N_RSYM  0x40    /* register sym: name,,NO_SECT,type,register */
                        #define N_SLINE 0x44    /* src line: 0,,n_sect,linenumber,address */
                        #define N_ENSYM 0x4e    /* end nsect sym: 0,,n_sect,0,address */
                        #define N_SSYM  0x60    /* structure elt: name,,NO_SECT,type,struct_offset */
                        #define N_SO    0x64    /* source file name: name,,n_sect,0,address */
                        #define N_OSO   0x66    /* object file name: name,,0,0,st_mtime */
                        #define N_LSYM  0x80    /* local sym: name,,NO_SECT,type,offset */
                        #define N_BINCL 0x82    /* include file beginning: name,,NO_SECT,0,sum */
                        #define N_SOL   0x84    /* #included file name: name,,n_sect,0,address */
                        #define N_PARAMS  0x86  /* compiler parameters: name,,NO_SECT,0,0 */
                        #define N_VERSION 0x88  /* compiler version: name,,NO_SECT,0,0 */
                        #define N_OLEVEL  0x8A  /* compiler -O level: name,,NO_SECT,0,0 */
                        #define N_PSYM  0xa0    /* parameter: name,,NO_SECT,type,offset */
                        #define N_EINCL 0xa2    /* include file end: name,,NO_SECT,0,0 */
                        #define N_ENTRY 0xa4    /* alternate entry: name,,n_sect,linenumber,address */
                        #define N_LBRAC 0xc0    /* left bracket: 0,,NO_SECT,nesting level,address */
                        #define N_EXCL  0xc2    /* deleted include file: name,,NO_SECT,0,sum */
                        #define N_RBRAC 0xe0    /* right bracket: 0,,NO_SECT,nesting level,address */
                        #define N_BCOMM 0xe2    /* begin common: name,,NO_SECT,0,0 */
                        #define N_ECOMM 0xe4    /* end common: name,,n_sect,0,0 */
                        #define N_ECOML 0xe8    /* end common (local name): 0,,n_sect,0,address */
                        #define N_LENG  0xfe    /* second stab entry with length information *
                        '''
                        if nstab.N_DESC != 0x20:
                            filtered_syms.append(sym)
                            filtered_ranlib_structs.append(ranlib_structs[i])
                        else:
                            print("Skipped {}, it is a global symbol I think".format(sym))
                else:
                    print('{} : N_STAB {}, N_PEXT {}, N_TYPE {}, N_EXT {}'.format(syms[i], ntype.N_STAB, ntype.N_PEXT, ntype.N_TYPE, ntype.N_EXT))
                    # if not ranlib_structs[i][0] & 0x8000:
                    # Symbols that should not appear:
                    # _PL_mod_latin1_uc :: 0x0bd800, 0x1abd
                    # _PerlIO_byte      :: 0x239000, 0x7de2
                    # 0x200000 seems to be "FUNCTION"?!
#                    if ((not ranlib_structs[i][0] & 0x200000) or (ranlib_structs[i][0] & 0x8000):
#                        and ranlib_structs[i][0] != 0x239000):
                    UNDEFINED = 0x5
                    if ntype.N_TYPE != UNDEFINED:
                        filtered_syms.append(syms[i])
                        filtered_ranlib_structs.append(ranlib_structs[i])
                if not '_PL_simple' in syms:
                    print('wtf')
                if not '_PL_simple' in filtered_syms:
                    print('wtf 2')
                return filtered_syms, filtered_ranlib_structs, syms, ranlib_structs
            index += size - name_len
        print("ERROR: Failed to find ranlib symbol defintions or GNU symbols")
        return results


def get_exports(filename, arch='native'):
    result = []
    if isinstance(filename, str):
        if os.path.exists(filename) and filename.endswith('.a') or filename.endswith('.lib'):
            # Sorry!
            import subprocess
            # syms = os.system('nm -g {}'.filename)
            # on macOS at least:
            # -PgUj is:
            # P: posix format
            # g: global (exported) only
            # U: not undefined
            # j is name only
            if sys.platform == 'darwin':
                flags = '-PgUj'
            else:
                flags = '-P'
            try:
                out, _ = subprocess.Popen(['nm', flags, filename], shell=False,
                                    stdout=subprocess.PIPE).communicate()
                results = out.decode('utf-8').splitlines()
                exports = [r.split(' ')[0] for r in results if (' T ') in r]
                result = exports
            except OSError:
                # nm may not be available or have the correct permissions, this
                # should not cause a failure, see gh-3287
                print('WARNING: nm: failed to get_exports({})'.format(file))

            # Now, our own implementation which does not require nm and can
            # handle .lib files.
            exports2, flags2, exports2_all, flags2_all = get_static_lib_exports(filename)
            result = exports2
            if set(exports) != set(exports2):
                diff1 = set(exports).difference(set(exports2))
                diff2 = set(exports2).difference(set(exports))
                error_count = len(diff1) + len(diff2)
                print("errors: {} (-{}, +{})".format(error_count, len(diff1), len(diff2)))
                print("WARNING :: Disagreement regarding static lib exports in {} between nm (nsyms={}) and lielfldd (nsyms={}):"
                      .format(filename, len(exports), len(exports2)))
#                print("nm.diff(liefldd) = \n{}".format('\n'.join(diff1)))
#                print("liefldd.diff(nm) = \n{}".format('\n'.join(diff2)))

                print("\nMissing symbols\n")

                for item in diff1:
                    if item not in exports2_all:
                        print('wtf 3 {}'.format(item))
                    idx = exports2_all.index(item)
                    print("{:>64} : {:08x}".format(item, flags2_all[idx][0]))
#                    print("{:>64} : {:032b}".format(item, flags2_all[idx][0]))

                print("\nUnwanted symbols\n")

                for item in diff2:
                    if item not in exports2_all:
                        print('wtf 4 {}'.format(item))
                    idx = exports2.index(item)
                    ntype = n_type()
                    ntype.asByte = flags2[idx][0] & 0xff
                    print('{} : N_STAB {}, N_PEXT {}, N_TYPE {}, N_EXT {}'.format(item, ntype.N_STAB, ntype.N_PEXT, ntype.N_TYPE, ntype.N_EXT))
#                    print("{:>64} : {:08x}".format(item, flags2[idx][0]))
#                    print("{:>64} : {:032b}".format(item, flags2[idx][0]))

                print("\nAll symbols\n")

                for item in exports2_all:
                    idx = exports2_all.index(item)
                    print("{:>64} : {:08x} {:08x}".format(item, flags2_all[idx][0], flags2_all[idx][1]))
#                    print("{:>64} : {:032b}".format(item, flags2_all[idx][0]))

    if not result:
        binary = ensure_binary(filename)
        if binary:
            result = [str(e) for e in binary.exported_functions]
    return result


def get_relocations(filename, arch='native'):
    if not os.path.exists(filename):
        return []
    try:
        binary = lief.parse(filename)
        res = []
        if len(binary.relocations):
            for r in binary.relocations:
                if r.has_symbol:
                    if r.symbol and r.symbol.name:
                        res.append(r.symbol.name)
            return res
    except:
        print('WARNING: liefldd: failed get_relocations({})'.format(filename))

    return []


def get_symbols(file, defined=True, undefined=True, arch='native'):
    binary = ensure_binary(file)
    try:
        if binary.__class__ == lief.MachO.Binary and binary.has_dyld_info:
            dyscmd = binary.dynamic_symbol_command
            first_undefined_symbol = dyscmd.idx_undefined_symbol
            last_undefined_symbol = first_undefined_symbol + dyscmd.nb_undefined_symbols - 1
        else:
            first_undefined_symbol = 0
            last_undefined_symbol = -1
        res = []
        if len(binary.exported_functions):
            syms = binary.exported_functions
        elif len(binary.symbols):
            syms = binary.symbols
        elif len(binary.static_symbols):
            syms = binary.static_symbols
        for index, s in enumerate(syms):
            is_undefined = index >= first_undefined_symbol and index <= last_undefined_symbol
            if binary.__class__ != lief.MachO.Binary:
                if isinstance(s, str):
                    res.append(s)
                else:
                    if s.exported and s.imported:
                        print("Weird, symbol {} is both imported and exported".format(s.name))
                    if s.exported:
                        is_undefined = True
                    elif s.imported:
                        is_undefined = False

                    if is_undefined and undefined:
                        res.append(s.name)
                    elif not is_undefined and defined:
                        res.append(s.name)
            # else:
            #     print("Skipping {}, is_undefined {}, defined {}, undefined {}".format(s.name, is_undefined, defined, undefined))
        return res
    except:
        print('WARNING: liefldd: failed get_symbols({})'.format(file))

    return []


class memoized_by_arg0_inode(object):
    """Decorator. Caches a function's return value each time it is called.
    If called later with the same arguments, the cached value is returned
    (not reevaluated).

    The first argument is required to be an existing filename and it is
    always converted to an inode number.
    """
    def __init__(self, func):
        self.func = func
        self.cache = {}
        self.lock = threading.Lock()

    def __call__(self, *args, **kw):
        newargs = []
        for arg in args:
            if arg is args[0]:
                s = os.stat(arg)
                arg = s.st_ino
            if isinstance(arg, list):
                newargs.append(tuple(arg))
            elif not isinstance(arg, Hashable):
                # uncacheable. a list, for instance.
                # better to not cache than blow up.
                return self.func(*args, **kw)
            else:
                newargs.append(arg)
        newargs = tuple(newargs)
        key = (newargs, frozenset(sorted(kw.items())))
        with self.lock:
            if key in self.cache:
                return self.cache[key]
            else:
                value = self.func(*args, **kw)
                self.cache[key] = value
                return value


class memoized_by_arg0_filehash(object):
    """Decorator. Caches a function's return value each time it is called.
    If called later with the same arguments, the cached value is returned
    (not reevaluated).

    The first argument is required to be an existing filename and it is
    always converted to an inode number.
    """
    def __init__(self, func):
        self.func = func
        self.cache = {}
        self.lock = threading.Lock()

    def __call__(self, *args, **kw):
        newargs = []
        for arg in args:
            if arg is args[0]:
                sha1 = hashlib.sha1()
                with open(arg, 'rb') as f:
                    while True:
                        data = f.read(65536)
                        if not data:
                            break
                        sha1.update(data)
                arg = sha1.hexdigest()
            if isinstance(arg, list):
                newargs.append(tuple(arg))
            elif not isinstance(arg, Hashable):
                # uncacheable. a list, for instance.
                # better to not cache than blow up.
                return self.func(*args, **kw)
            else:
                newargs.append(arg)
        newargs = tuple(newargs)
        key = (newargs, frozenset(sorted(kw.items())))
        with self.lock:
            if key in self.cache:
                return self.cache[key]
            else:
                value = self.func(*args, **kw)
                self.cache[key] = value
                return value


@memoized_by_arg0_inode
def get_exports_memoized(filename, arch='native'):
    return get_exports(filename, arch=arch)


@memoized_by_arg0_filehash
def get_imports_memoized(filename, arch='native'):
    return get_imports(filename, arch=arch)


@memoized_by_arg0_filehash
def get_relocations_memoized(filename, arch='native'):
    return get_relocations(filename, arch=arch)


@memoized_by_arg0_filehash
def get_symbols_memoized(filename, defined, undefined, arch):
    return get_symbols(filename, defined=defined, undefined=undefined, arch=arch)


@memoized_by_arg0_filehash
def get_linkages_memoized(filename, resolve_filenames, recurse,
                          sysroot='', envroot='', arch='native'):
    return get_linkages(filename, resolve_filenames=resolve_filenames,
                        recurse=recurse, sysroot=sysroot, envroot=envroot, arch=arch)


#d = get_static_lib_exports('/Users/rdonnelly/conda/bzip2_static.lib')
#print(d)
#c = get_static_lib_exports('/Users/rdonnelly/conda/bzip2.lib')
#print(c)
#a = get_static_lib_exports('/Users/rdonnelly/conda/libbz2_linux.a')
#print(a)
#b = get_static_lib_exports('/Users/rdonnelly/conda/libbz2_macos.a')
#print(b)
#print(len(b))
import glob
# for static_lib in glob.iglob('/opt/conda/pkgs/**/*.a', recursive=True):
# for static_lib in ['/opt/conda/pkgs/flex-2.6.4-hb56bbfa_1/lib/libfl.a']:
# for static_lib in ['/opt/conda/pkgs/libtool-2.4.6-h7b6447c_5/lib/libltdl.a']:
for static_lib in ['/opt/conda/pkgs/perl-5.22.2.1-0/lib/perl5/5.22.2/darwin-thread-multi-2level/CORE/libperl.a']:
    get_exports(static_lib)

# Test a symbol on macOS:
# nm -PgUj /opt/conda/pkgs/perl-5.22.2.1-0/lib/perl5/5.22.2/darwin-thread-multi-2level/CORE/libperl.a | grep _Perl_allocmy
