#!/usr/bin/env python3
#
# Copyright (C) 2016 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# pylint: disable=not-callable, line-too-long, no-else-return

import argparse
import logging
from pathlib import Path
import os
import shutil
import sys
import textwrap
from typing import List, Optional, Set, Tuple

import android_version
from base_builders import Builder, LLVMBuilder
import builders
from builder_registry import BuilderRegistry
import configs
import hosts
import paths
import source_manager
import timer
import toolchains
import utils
from version import Version
import win_sdk

def logger():
    """Returns the module level logger."""
    return logging.getLogger(__name__)


def set_default_toolchain(toolchain: toolchains.Toolchain) -> None:
    """Sets the toolchain to use for builders who don't specify a toolchain in constructor."""
    Builder.toolchain = toolchain


def extract_profdata() -> Optional[Path]:
    tar = paths.pgo_profdata_tar()
    if not tar:
        return None
    profdata_file = paths.OUT_DIR / paths.pgo_profdata_filename()
    if profdata_file.exists():
        return profdata_file
    utils.check_call(['tar', '-jxC', str(paths.OUT_DIR), '-f', str(tar)])
    profdata_file = paths.OUT_DIR / paths.pgo_profdata_filename()
    if not profdata_file.exists():
        raise RuntimeError(
            f'Failed to extract profdata from {tar} to {paths.OUT_DIR}')
    return profdata_file


def build_llvm_for_windows(enable_assertions: bool,
                           build_name: str,
                           build_lldb: bool,
                           swig_builder: Optional[builders.SwigBuilder]):
    config_list: List[configs.Config]
    if win_sdk.is_enabled():
        config_list = [configs.MSVCConfig()]
    else:
        config_list = [configs.MinGWConfig()]

    win_builder = builders.WindowsToolchainBuilder(config_list)
    if win_builder.install_dir.exists():
        shutil.rmtree(win_builder.install_dir)

    if not win_sdk.is_enabled():
        # Build and install libcxxabi and libcxx and use them to build Clang.
        libcxx_builder = builders.LibCxxBuilder(config_list)
        libcxxabi_builder = builders.LibCxxAbiBuilder(config_list)
        libcxxabi_builder.enable_assertions = enable_assertions
        libcxxabi_builder.build()

        libcxx_builder.libcxx_abi_path = libcxxabi_builder.install_dir
        libcxx_builder.enable_assertions = enable_assertions
        libcxx_builder.build()
        win_builder.libcxx_path = libcxx_builder.install_dir

    lldb_bins: Set[str] = set()
    libxml2_builder = builders.LibXml2Builder(config_list)
    libxml2_builder.build()
    win_builder.libxml2 = libxml2_builder
    for lib in libxml2_builder.install_libraries:
        lldb_bins.add(lib.name)

    win_builder.build_lldb = build_lldb
    if build_lldb:
        assert swig_builder is not None
        win_builder.libedit = None
        win_builder.swig_executable = swig_builder.install_dir / 'bin' / 'swig'

        xz_builder = builders.XzBuilder(config_list)
        xz_builder.build()
        win_builder.liblzma = xz_builder

        lldb_bins.add('liblldb.dll')

    win_builder.build_name = build_name
    win_builder.svn_revision = android_version.get_svn_revision()
    win_builder.enable_assertions = enable_assertions
    win_builder.build()

    return (win_builder, lldb_bins)


def build_runtimes(build_lldb_server: bool):
    builders.SysrootsBuilder().build()
    builders.BuiltinsBuilder().build()
    builders.LibUnwindBuilder().build()
    builders.PlatformLibcxxAbiBuilder().build()
    builders.CompilerRTBuilder().build()
    # 32-bit host crts are not needed for Darwin
    if hosts.build_host().is_linux:
        builders.CompilerRTHostAarch64Builder().build()
    builders.LibOMPBuilder().build()
    if build_lldb_server:
        builders.LldbServerBuilder().build()
    # Bug: http://b/64037266. `strtod_l` is missing in NDK r15. This will break
    # libcxx build.
    # build_libcxx(toolchain, version)
    builders.AsanMapFileBuilder().build()


def install_wrappers(llvm_install_path: Path, llvm_next=False) -> None:
    wrapper_path = paths.OUT_DIR / 'llvm_android_wrapper'
    wrapper_build_script = paths.TOOLCHAIN_UTILS_DIR / 'compiler_wrapper' / 'build.py'
    # Note: The build script automatically determines the architecture
    # based on the host.
    go_env = dict(os.environ)
    go_env['PATH'] = str(paths.GO_BIN_PATH) + os.pathsep + go_env['PATH']
    utils.check_call([sys.executable, wrapper_build_script,
                      '--config=android',
                      '--use_ccache=false',
                      '--use_llvm_next=' + str(llvm_next).lower(),
                      f'--output_file={wrapper_path}'], env=go_env)

    bisect_path = paths.SCRIPTS_DIR / 'bisect_driver.py'
    bin_path = llvm_install_path / 'bin'
    clang_path = bin_path / 'clang'
    clang_real_path = bin_path / 'clang.real'
    clangxx_path = bin_path / 'clang++'
    clangxx_real_path = bin_path / 'clang++.real'
    clang_tidy_path = bin_path / 'clang-tidy'
    clang_tidy_real_path = bin_path / 'clang-tidy.real'

    # Rename clang and clang++ to clang.real and clang++.real.
    # clang and clang-tidy may already be moved by this script if we use a
    # prebuilt clang. So we only move them if clang.real and clang-tidy.real
    # doesn't exist.
    if not clang_real_path.exists():
        clang_path.rename(clang_real_path)
    clang_tidy_real_path = clang_tidy_path.parent / (clang_tidy_path.name + '.real')
    if not clang_tidy_real_path.exists():
        clang_tidy_path.rename(clang_tidy_real_path)
    clang_path.unlink(missing_ok=True)
    clangxx_path.unlink(missing_ok=True)
    clang_tidy_path.unlink(missing_ok=True)
    clangxx_real_path.unlink(missing_ok=True)
    clangxx_real_path.symlink_to('clang.real')

    shutil.copy2(wrapper_path, clang_path)
    shutil.copy2(wrapper_path, clangxx_path)
    shutil.copy2(wrapper_path, clang_tidy_path)
    shutil.copy2(bisect_path, bin_path)

    # point clang-cl to clang.real instead of clang (which is the wrapper)
    clangcl_path = bin_path / 'clang-cl'
    clangcl_path.unlink()
    clangcl_path.symlink_to('clang.real')


# Normalize host libraries (libLLVM, libclang, libc++, libc++abi) so that there
# is just one library, whose SONAME entry matches the actual name.
def normalize_llvm_host_libs(install_dir: Path, host: hosts.Host, version: Version) -> None:
    if host.is_linux:
        libs = {'libLLVM': 'libLLVM-{version}git.so',
                'libclang': 'libclang.so.{version}git',
                'libclang-cpp': 'libclang-cpp.so.{version}git',
                'libclang_cxx': 'libclang_cxx.so.{version}git',
                'libc++': 'libc++.so.{version}',
                'libc++abi': 'libc++abi.so.{version}'
               }
    else:
        libs = {'libc++': 'libc++.{version}.dylib',
                'libc++abi': 'libc++abi.{version}.dylib'
               }

    def getVersions(libname: str) -> Tuple[str, str]:
        if libname == 'libclang_cxx' or libname == 'libclang-cpp':
            return version.major, version.major
        if not libname.startswith('libc++'):
            return version.long_version(), version.major
        else:
            return '1.0', '1'

    libdir = os.path.join(install_dir, 'lib')
    for libname, libformat in libs.items():
        short_version, major = getVersions(libname)

        soname_version = '13' if libname == 'libclang' else major
        soname_lib = os.path.join(libdir, libformat.format(version=soname_version))
        if libname.startswith('libclang') and libname != 'libclang-cpp':
            soname_lib = soname_lib[:-3]
        real_lib = os.path.join(libdir, libformat.format(version=short_version))

        preserved_libnames = ('libLLVM', 'libclang_cxx', 'libclang-cpp')
        if libname not in preserved_libnames:
            # Rename the library to match its SONAME
            if not os.path.isfile(real_lib):
                raise RuntimeError(real_lib + ' must be a regular file')
            if not os.path.islink(soname_lib):
                raise RuntimeError(soname_lib + ' must be a symlink')

            shutil.move(real_lib, soname_lib)

        # Retain only soname_lib and delete other files for this library.  We
        # still need libc++.so or libc++.dylib symlinks for a subsequent stage1
        # build using these prebuilts (where CMake tries to find C++ atomics
        # support) to succeed.  We also need a few checks to ensure libclang-cpp
        # is not deleted when cleaning up libclang.so* and libc++abi is not
        # deleted when cleaning up libc++.so*.
        libcxx_name = 'libc++.so' if host.is_linux else 'libc++.dylib'
        all_libs = [lib for lib in os.listdir(libdir) if
                    lib != libcxx_name and
                    not lib.startswith('libclang-cpp') and # retain libclang-cpp
                    not lib.endswith('.a') and # skip static host libraries
                    (lib.startswith(libname + '.') or # so libc++abi is ignored
                     lib.startswith(libname + '-'))]

        for lib in all_libs:
            lib = os.path.join(libdir, lib)
            if lib != soname_lib:
                os.remove(lib)


def install_license_files(install_dir: Path) -> None:
    projects = (
        'llvm',
        'compiler-rt',
        'libcxx',
        'libcxxabi',
        'openmp',
        'clang',
        'clang-tools-extra',
        'lld',
    )

    # Get generic MODULE_LICENSE_* files from our android subdirectory.
    for license_file in paths.SCRIPTS_DIR.glob('MODULE_LICENSE_*'):
        shutil.copy2(license_file, install_dir)

    # Fetch all the LICENSE.* files under our projects and append them into a
    # single NOTICE file for the resulting prebuilts.
    notices = []
    for project in projects:
        for license_file in (paths.LLVM_PATH / project).glob('LICENSE.*'):
            with license_file.open() as notice_file:
                notices.append(notice_file.read())
    with (install_dir / 'NOTICE').open('w') as notice_file:
        notice_file.write('\n'.join(notices))


def install_winpthreads(bin_dir: Path, lib_dir: Path) -> None:
    """Installs the winpthreads runtime to the Windows bin and lib directory."""
    lib_name = 'libwinpthread-1.dll'
    lib_path = paths.MINGW_ROOT / 'bin' / lib_name

    shutil.copy2(lib_path, lib_dir / lib_name)
    shutil.copy2(lib_path, bin_dir / lib_name)


def remove_static_libraries(static_lib_dir, necessary_libs=None):
    if not necessary_libs:
        necessary_libs = {}
    if os.path.isdir(static_lib_dir):
        lib_files = os.listdir(static_lib_dir)
        for lib_file in lib_files:
            if lib_file.endswith('.a') and lib_file not in necessary_libs:
                static_library = os.path.join(static_lib_dir, lib_file)
                os.remove(static_library)


def darwin_codesign(binary):
      utils.check_call(['codesign', '--force', '-s', '-', binary])


def package_toolchain(toolchain_builder: LLVMBuilder,
                      necessary_bin_files: Optional[Set[str]]=None,
                      strip=True, create_tar=True, llvm_next=False):
    dist_dir = Path(utils.ORIG_ENV.get('DIST_DIR', paths.OUT_DIR))
    build_dir = toolchain_builder.install_dir
    host = toolchain_builder.config_list[0].target_os
    build_name = toolchain_builder.build_name
    version = toolchain_builder.installed_toolchain.version

    package_name = 'clang-' + build_name

    install_dir = paths.get_package_install_path(host, package_name)
    install_host_dir = install_dir.parent

    # Remove any previously installed toolchain so it doesn't pollute the
    # build.
    if install_host_dir.exists():
        shutil.rmtree(install_host_dir)

    # First copy over the entire set of output objects.
    shutil.copytree(build_dir, install_dir, symlinks=True)

    ext = '.exe' if host.is_windows else ''
    script_ext = '.cmd' if host.is_windows else '.sh'
    shlib_ext = '.dll' if host.is_windows else '.so' if host.is_linux else '.dylib'

    if not necessary_bin_files:
        necessary_bin_files = set()

    # Next, we remove unnecessary binaries.
    necessary_bin_files |= {
        'clang' + ext,
        'clang++' + ext,
        'clang-' + version.major_version() + ext,
        'clang-check' + ext,
        'clang-cl' + ext,
        'clang-format' + ext,
        'clang-tidy' + ext,
        'clangd' + ext,
        'dsymutil' + ext,
        'git-clang-format',  # No extension here
        'ld.lld' + ext,
        'ld64.lld' + ext,
        'lld' + ext,
        'lld-link' + ext,
        'llvm-addr2line' + ext,
        'llvm-ar' + ext,
        'llvm-as' + ext,
        'llvm-cfi-verify' + ext,
        'llvm-config' + ext,
        'llvm-cov' + ext,
        'llvm-cxxfilt' + ext,
        'llvm-dis' + ext,
        'llvm-dwarfdump' + ext,
        'llvm-dwp' + ext,
        'llvm-lib' + ext,
        'llvm-link' + ext,
        'llvm-lipo' + ext,
        'llvm-modextract' + ext,
        'llvm-nm' + ext,
        'llvm-objcopy' + ext,
        'llvm-objdump' + ext,
        'llvm-profdata' + ext,
        'llvm-ranlib' + ext,
        'llvm-rc' + ext,
        'llvm-readelf' + ext,
        'llvm-readobj' + ext,
        'llvm-size' + ext,
        'llvm-strings' + ext,
        'llvm-strip' + ext,
        'llvm-symbolizer' + ext,
        'llvm-windres' + ext,
        'sancov' + ext,
        'sanstats' + ext,
        'scan-build' + ext,
        'scan-view' + ext,
    }

    if toolchain_builder.build_lldb:
        necessary_bin_files.update({
            'lldb-argdumper' + ext,
            'lldb' + ext,
            'lldb' + script_ext,
        })

    if host.is_windows:
        windows_exclude_bin_files = {
            'clang-' + version.major_version() + ext,
            'clangd' + ext,
            'scan-build' + ext,
            'scan-view' + ext,
        }
        necessary_bin_files -= windows_exclude_bin_files

    # scripts that should not be stripped
    script_bins = {
        'git-clang-format',
        'scan-build',
        'scan-view',
        'lldb' + script_ext,
    }

    bin_dir = install_dir / 'bin'
    lib_dir = install_dir / 'lib'
    strip_cmd = Builder.toolchain.strip

    for binary in bin_dir.iterdir():
        if binary.is_file():
            if binary.name not in necessary_bin_files:
                binary.unlink()
            elif binary.is_symlink():
                continue
            elif strip and binary.name not in script_bins:
                # Strip all non-global symbols and debug info.
                # These specific flags prevent Darwin executables from being
                # stripped of additional global symbols that might be used
                # by plugins.
                utils.check_call([strip_cmd, '-S', '-x', binary])
                # Strip mutates binary, need to codesign the binary again.
                if host.is_darwin:
                    darwin_codesign(binary)

    # FIXME: check that all libs under lib/clang/<version>/ are created.
    for necessary_bin_file in necessary_bin_files:
        if not (bin_dir / necessary_bin_file).is_file():
            raise RuntimeError(f'Did not find {necessary_bin_file} in {bin_dir}')

    necessary_lib_files = set()
    if not (host.is_windows and win_sdk.is_enabled()):
        necessary_lib_files |= {
            'libc++.a',
            'libc++abi.a',
        }

    if host.is_windows and not win_sdk.is_enabled():
        necessary_lib_files.add('libwinpthread-1' + shlib_ext)
        # For Windows, add other relevant libraries.
        install_winpthreads(bin_dir, lib_dir)

    # Remove unnecessary static libraries.
    remove_static_libraries(lib_dir, necessary_lib_files)

    if host.is_linux:
        install_wrappers(install_dir, llvm_next)

    if not host.is_windows:
        normalize_llvm_host_libs(install_dir, host, version)

    # Check necessary lib files exist.
    for necessary_lib_file in necessary_lib_files:
        if not (lib_dir / necessary_lib_file).is_file():
            raise RuntimeError(f'Did not find {necessary_lib_file} in {lib_dir}')

    # Next, we copy over stdatomic.h and bits/stdatomic.h from bionic.
    libc_include_path = paths.ANDROID_DIR / 'bionic' / 'libc' / 'include'
    header_path = lib_dir / 'clang' / version.long_version() / 'include'

    shutil.copy2(libc_include_path / 'stdatomic.h', header_path)

    bits_install_path = header_path / 'bits'
    bits_install_path.mkdir(parents=True, exist_ok=True)
    bits_stdatomic_path = libc_include_path / 'bits' / 'stdatomic.h'
    shutil.copy2(bits_stdatomic_path, bits_install_path)

    # Install license files as NOTICE in the toolchain install dir.
    install_license_files(install_dir)

    # Add an AndroidVersion.txt file.
    version_file_path = install_dir / 'AndroidVersion.txt'
    with version_file_path.open('w') as version_file:
        version_file.write(f'{version.long_version()}\n')
        svn_revision = android_version.get_svn_revision()
        version_file.write(f'based on {svn_revision}\n')
        version_file.write('for additional information on LLVM revision and '
                           'cherry-picks, see clang_source_info.md')
    shutil.copy2(paths.OUT_DIR / 'clang_source_info.md', install_dir)

    if host.is_linux:

        # Add BUILD.bazel file.
        with (install_dir / 'BUILD.bazel').open('w') as bazel_file:
            bazel_file.write(
                textwrap.dedent("""\
                    package(default_visibility = ["//visibility:public"])

                    filegroup(
                        name = "binaries",
                        srcs = glob([
                            "bin/*",
                            "lib/*",
                        ]),
                    )"""))

        # Create RBE input files.
        with (install_dir / 'bin' / 'remote_toolchain_inputs').open('w') as inputs_file:
            dependencies = ('clang\n'
                            'clang++\n'
                            'clang.real\n'
                            'clang++.real\n'
                            'clang-tidy\n'
                            'clang-tidy.real\n'
                            '../lib/libc++.so.1\n'
                            'lld\n'
                            'ld64.lld\n'
                            'ld.lld\n'
                            f'../lib/clang/{version.long_version()}/share\n'
                            f'../lib/clang/{version.long_version()}/lib/linux\n'
                            f'../lib/clang/{version.long_version()}/include\n'
                            f'../lib/libxml2.so.{builders.LibXml2Builder.lib_version}\n'
                           )
            inputs_file.write(dependencies)

    # Package up the resulting trimmed install/ directory.
    if create_tar:
        tarball_name = package_name + '-' + host.os_tag + '.tar.bz2'
        package_path = dist_dir / tarball_name
        logger().info(f'Packaging {package_path}')
        args = ['tar', '-cjC', install_host_dir, '-f', package_path, package_name]
        utils.check_call(args)


def parse_args():
    known_components = ('linux', 'windows', 'lldb')
    known_components_str = ', '.join(known_components)

    # Simple argparse.Action to allow comma-separated values (e.g.
    # --option=val1,val2)
    class CommaSeparatedListAction(argparse.Action):
        def __call__(self, parser, namespace, values, option_string):
            for value in values.split(','):
                if value not in known_components:
                    error = '\'{}\' invalid.  Choose from {}'.format(
                        value, known_components)
                    raise argparse.ArgumentError(self, error)
            setattr(namespace, self.dest, values.split(','))


    # Parses and returns command line arguments.
    parser = argparse.ArgumentParser()

    parser.add_argument(
        '--build-name', default='dev', help='Release name for the package.')

    parser.add_argument(
        '--enable-assertions',
        action='store_true',
        default=False,
        help='Enable assertions (only affects stage2)')

    lto_group = parser.add_mutually_exclusive_group()
    lto_group.add_argument(
        '--lto',
        action='store_true',
        default=False,
        help='Enable LTO (only affects stage2).  This option increases build time.')
    lto_group.add_argument(
        '--no-lto',
        action='store_false',
        default=False,
        dest='lto',
        help='Disable LTO to speed up build (only affects stage2)')

    parser.add_argument(
        '--no-pgo',
        action='store_true',
        default=False,
        help='Disable PGO (only affects stage2)')

    parser.add_argument(
        '--debug',
        action='store_true',
        default=False,
        help='Build debuggable Clang and LLVM tools (only affects stage2)')

    parser.add_argument(
        '--build-instrumented',
        action='store_true',
        default=False,
        help='Build LLVM tools with PGO instrumentation')

    # Options to skip build or packaging (can't skip both, or the script does
    # nothing).
    build_package_group = parser.add_mutually_exclusive_group()
    build_package_group.add_argument(
        '--skip-build',
        '-sb',
        action='store_true',
        default=False,
        help='Skip the build, and only do the packaging step')
    build_package_group.add_argument(
        '--skip-package',
        '-sp',
        action='store_true',
        default=False,
        help='Skip the packaging, and only do the build step')

    parser.add_argument(
        '--skip-source-setup',
        action='store_true',
        default=False,
        help='Skip setting up source code, which can be slow on rotational disks. Only use this if \
        no code has changed since previous build.')

    parser.add_argument(
        '--create-tar',
        action='store_true',
        default=False,
        help='Create a tar archive of the toolchains')

    parser.add_argument(
        '--no-strip',
        action='store_true',
        default=False,
        help='Don\'t strip binaries/libraries')

    parser.add_argument(
        '--run-tests-stage1',
        action=argparse.BooleanOptionalAction,
        default=False,
        help='Run tests in stage1, with clang-tools-extra.')

    parser.add_argument(
        '--skip-tests',
        action='store_true',
        default=False,
        help='Skip clang/llvm check tests after stage1 and stage2.')

    build_group = parser.add_mutually_exclusive_group()
    build_group.add_argument(
        '--build',
        nargs='+',
        help='A list of builders to build. All builders not listed will be skipped.')
    build_group.add_argument(
        '--skip',
        nargs='+',
        help='A list of builders to skip. All builders not listed will be built.')

    # skip_runtimes is set to skip recompilation of libraries
    parser.add_argument(
        '--skip-runtimes',
        action='store_true',
        default=False,
        help='Skip the runtime libraries')

    parser.add_argument(
        '--no-build',
        action=CommaSeparatedListAction,
        default=list(),
        help='Don\'t build toolchain components or platforms.  Choices: ' + \
            known_components_str)

    parser.add_argument(
        '--build-llvm-next',
        action='store_true',
        default=False,
        help='Build next LLVM revision (android_version.py:svn_revision_next)')

    parser.add_argument(
        '--windows-sdk',
        help='Path to a Windows SDK. If set, it will be used instead of MinGW.'
    )

    return parser.parse_args()


def main():
    dist_dir = Path(utils.ORIG_ENV.get('DIST_DIR', paths.OUT_DIR))
    timer.Timer.register_atexit(dist_dir / 'build_times.txt')

    args = parse_args()
    if args.skip_build:
        # Skips all builds
        BuilderRegistry.add_filter(lambda name: False)
    elif args.skip:
        BuilderRegistry.add_skips(args.skip)
    elif args.build:
        BuilderRegistry.add_builds(args.build)
    do_runtimes = not args.skip_runtimes
    do_package = not args.skip_package
    do_strip = not args.no_strip
    do_strip_host_package = do_strip and not args.debug and not args.build_llvm_next
    build_lldb = 'lldb' not in args.no_build

    android_version.set_llvm_next(args.build_llvm_next)

    need_host = hosts.build_host().is_darwin or ('linux' not in args.no_build)
    need_windows = hosts.build_host().is_linux and ('windows' not in args.no_build)

    logging.basicConfig(level=logging.DEBUG)

    logger().info('do_build=%r do_stage1=%r do_stage2=%r do_runtimes=%r do_package=%r need_windows=%r lto=%r' %
                  (not args.skip_build, BuilderRegistry.should_build('stage1'), BuilderRegistry.should_build('stage2'),
                  do_runtimes, do_package, need_windows, args.lto))

    # Clone sources to be built and apply patches.
    if not args.skip_source_setup:
        source_manager.setup_sources(source_dir=paths.LLVM_PATH)

    # Build the stage1 Clang for the build host
    instrumented = hosts.build_host().is_linux and args.build_instrumented

    stage1 = builders.Stage1Builder()
    stage1.build_name = 'stage1'
    stage1.svn_revision = android_version.get_svn_revision()
    # Build lldb for lldb-tblgen. It will be used to build lldb-server and windows lldb.
    stage1.build_lldb = build_lldb
    stage1.build_extra_tools = args.run_tests_stage1
    stage1.build_android_targets = args.debug or instrumented
    stage1.build()
    # stage1 test is off by default, turned on by --run-tests-stage1,
    # and suppressed by --skip-tests.
    if not args.skip_tests and args.run_tests_stage1:
         stage1.test()
    set_default_toolchain(stage1.installed_toolchain)

    if build_lldb:
        # Swig is needed for both host and windows lldb.
        swig_builder = builders.SwigBuilder()
        swig_builder.build()
    else:
        swig_builder = None

    if need_host:
        if not args.no_pgo:
            profdata = extract_profdata()
        else:
            profdata = None

        stage2 = builders.Stage2Builder()
        stage2.build_name = args.build_name
        stage2.svn_revision = android_version.get_svn_revision()
        stage2.debug_build = args.debug
        stage2.enable_assertions = args.enable_assertions
        stage2.lto = args.lto
        stage2.build_instrumented = instrumented
        stage2.profdata_file = profdata if profdata else None

        libxml2_builder = builders.LibXml2Builder()
        libxml2_builder.build()
        stage2.libxml2 = libxml2_builder

        stage2.build_lldb = build_lldb
        if build_lldb:
            stage2.swig_executable = swig_builder.install_dir / 'bin' / 'swig'

            xz_builder = builders.XzBuilder()
            xz_builder.build()
            stage2.liblzma = xz_builder

            libncurses = builders.LibNcursesBuilder()
            libncurses.build()
            stage2.libncurses = libncurses

            libedit_builder = builders.LibEditBuilder()
            libedit_builder.libncurses = libncurses
            libedit_builder.build()
            stage2.libedit = libedit_builder

        stage2_tags = []
        # Annotate the version string if there is no profdata.
        if profdata is None:
            stage2_tags.append('NO PGO PROFILE')
        # Annotate the version string if this is an llvm-next build.
        if args.build_llvm_next:
            stage2_tags.append('ANDROID_LLVM_NEXT')
        stage2.build_tags = stage2_tags

        stage2.build()
        if not (stage2.build_instrumented or stage2.debug_build):
            set_default_toolchain(stage2.installed_toolchain)

        Builder.output_toolchain = stage2.installed_toolchain
        if hosts.build_host().is_linux and do_runtimes:
            build_runtimes(build_lldb_server=build_lldb)

    if need_windows:
        if args.windows_sdk:
            win_sdk.set_path(Path(args.windows_sdk))
        win_builder, win_lldb_bins = build_llvm_for_windows(
            enable_assertions=args.enable_assertions,
            build_name=args.build_name,
            build_lldb=build_lldb,
            swig_builder=swig_builder)

    # stage2 test is on when stage2 is enabled unless --skip-tests or
    # on instrumented builds.
    need_tests = not args.skip_tests and need_host and \
            BuilderRegistry.should_build('stage2') and \
            (not args.build_instrumented)
    if need_tests:
        # http://b/197645198 Temporarily skip tests on [Darwin|Debug] builds
        if not (hosts.build_host().is_darwin or args.debug):
            stage2.test()

    if do_package and need_host:
        package_toolchain(
            stage2,
            strip=do_strip_host_package,
            create_tar=args.create_tar,
            llvm_next=args.build_llvm_next)

    if do_package and need_windows:
        package_toolchain(
            win_builder,
            necessary_bin_files=win_lldb_bins,
            strip=do_strip,
            create_tar=args.create_tar)

    return 0


if __name__ == '__main__':
    main()
