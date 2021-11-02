#
# Copyright (C) 2020 The Android Open Source Project
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
"""Builders for various build tools and build systems."""

import functools
from pathlib import Path
import logging
import multiprocessing
import os
import shutil
import subprocess
from typing import cast, Dict, List, Optional, Set, Sequence

import android_version
from builder_registry import BuilderRegistry
import configs
import constants
import hosts
import paths
import timer
import toolchains
import utils
import win_sdk


def logger():
    """Returns the module level logger."""
    return logging.getLogger(__name__)


class LibInfo:
    """An interface to get information of a library."""

    name: str
    _config: configs.Config

    lib_version: str
    static_lib: bool = False

    @property
    def install_dir(self) -> Path:
        raise NotImplementedError()

    @property
    def _lib_names(self) -> List[str]:
        return [self.name]

    @property
    def include_dir(self) -> Path:
        """Path to headers."""
        return self.install_dir / 'include'

    @property
    def _lib_suffix(self) -> str:
        if self._config.target_os.is_windows and win_sdk.is_enabled():
            return '.lib'
        if self.static_lib:
            return '.a'
        return {
            hosts.Host.Linux: f'.so.{self.lib_version}',
            hosts.Host.Darwin: f'.{self.lib_version}.dylib',
            hosts.Host.Windows: '.dll.a',
        }[self._config.target_os]

    @property
    def link_libraries(self) -> List[Path]:
        """Path to the libraries used when linking."""
        suffix = self._lib_suffix
        return list(self.install_dir / 'lib' / f'{name}{suffix}' for name in self._lib_names)

    @property
    def install_libraries(self) -> List[Path]:
        """Path to the libraries to install."""
        if self.static_lib:
            return []
        if self._config.target_os.is_windows:
            return [self.install_dir / 'bin' / f'{name}.dll' for name in self._lib_names]
        return self.link_libraries

    @property
    def symlinks(self) -> List[Path]:
        """List of symlinks to the library that may need to be installed."""
        return []

    def update_lib_id(self) -> None:
        """Util function to update lib paths on mac."""
        if self.static_lib:
            return
        if not self._config.target_os.is_darwin:
            return
        for lib in self.link_libraries:
            # Update LC_ID_DYLIB, so that users of the library won't link with absolute path.
            utils.check_call(['install_name_tool', '-id', f'@rpath/{lib.name}', str(lib)])
            # The lib may already reference other libs.
            for other_lib in self.link_libraries:
                utils.check_call(['install_name_tool', '-change', str(other_lib),
                                  f'@rpath/{other_lib.name}', str(lib)])


class Builder:  # pylint: disable=too-few-public-methods
    """Base builder type."""
    name: str = ""
    config_list: List[configs.Config]

    """Use prebuilt toolchain by default. This value will be updated if a new toolchain is built."""
    toolchain: toolchains.Toolchain = toolchains.get_prebuilt_toolchain()

    """The toolchain to install artifacts from this LLVMRuntimeBuilder."""
    output_toolchain: toolchains.Toolchain

    def __init__(self,
                 config_list: Optional[Sequence[configs.Config]] = None,
                 toolchain: Optional[toolchains.Toolchain] = None) -> None:
        if toolchain:
            self.toolchain = toolchain
        if config_list:
            self.config_list = list(config_list)
        self._config: configs.Config = self.config_list[0]

    @BuilderRegistry.register_and_build
    def build(self) -> None:
        """Builds all configs."""
        for config in self.config_list:
            self._config = config

            logger().info('Building %s for %s', self.name, self._config)
            with timer.Timer(f'{self.name}_{self._config}'):
                self._build_config()
        self.install()

    def _build_config(self) -> None:
        raise NotImplementedError()

    def _is_cross_compiling(self) -> bool:
        return self._config.target_os != hosts.build_host()

    @property
    def _cc(self) -> Path:
        return self._config.get_c_compiler(self.toolchain)

    @property
    def _cxx(self) -> Path:
        return self._config.get_cxx_compiler(self.toolchain)

    @property
    def cflags(self) -> List[str]:
        """Additional cflags to use."""
        return []

    @property
    def cxxflags(self) -> List[str]:
        """Additional cxxflags to use."""
        return self.cflags

    @property
    def ldflags(self) -> List[str]:
        """Additional ldflags to use."""
        ldflags = []
        # When cross compiling, toolchain libs won't work on target arch.
        if not self._is_cross_compiling():
            ldflags.append(f'-L{self.toolchain.lib_dir}')
        return ldflags

    @property
    def env(self) -> Dict[str, str]:
        """Environment variables used when building."""
        env = dict(utils.ORIG_ENV)
        env.update(self._config.env)
        paths = [self._config.env.get('PATH'), utils.ORIG_ENV.get('PATH')]
        env['PATH'] = os.pathsep.join(p for p in paths if p)
        return env

    def install(self) -> None:
        """Installs built artifacts."""


class AutoconfBuilder(Builder):
    """Builder for autoconf targets."""
    src_dir: Path
    remove_install_dir: bool = True

    @property
    def output_dir(self) -> Path:
        """The path for intermediate results."""
        return paths.OUT_DIR / 'lib' / (f'{self.name}{self._config.output_suffix}')

    @property
    def install_dir(self) -> Path:
        """Returns the path this target will be installed to."""
        output_dir = self.output_dir
        return output_dir.parent / (output_dir.name + '-install')

    @staticmethod
    def _get_mac_sdk_path() -> Path:
        out = subprocess.check_output(['xcrun', '--show-sdk-path'], text=True)
        return Path(out.strip())

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        cflags.append('-fPIC')
        cflags.append('-Wno-unused-command-line-argument')
        #if self._config.sysroot:
        #    cflags.append(f'--sysroot={self._config.sysroot}')
        if self._config.target_os.is_darwin:
            sdk_path = self._get_mac_sdk_path()
            cflags.append(f'-mmacosx-version-min={constants.MAC_MIN_VERSION}')
            cflags.append(f'-DMACOSX_DEPLOYMENT_TARGET={constants.MAC_MIN_VERSION}')
            cflags.append(f'-isysroot{sdk_path}')
            cflags.append(f'-Wl,-syslibroot,{sdk_path}')
        return cflags

    @property
    def cxxflags(self) -> List[str]:
        cxxflags = super().cxxflags
        cxxflags.append('-stdlib=libc++')
        return cxxflags

    @property
    def ldflags(self) -> List[str]:
        ldflags = super().ldflags
        if self._config.target_os.is_darwin:
            ldflags.append('-Wl,-rpath,@loader_path/../lib')
        if self._config.target_os.is_linux:
            ldflags.append('-Wl,-rpath,$ORIGIN/../lib')
        return ldflags

    @property
    def config_flags(self) -> List[str]:
        """Parameters to configure."""
        return []

    def _touch_src_dir(self, files) -> None:
        for file in files:
            file_path = self.src_dir / file
            if file_path.is_file():
                file_path.touch(exist_ok=True)

    def _touch_autoconfig_files(self) -> None:
        """Touches configure files to prevent autoreconf."""
        files_to_touch = ["aclocal.m4", "configure", "Makefile.am"]
        self._touch_src_dir(files_to_touch)
        self._touch_src_dir(self.src_dir.glob('**/*.in'))

    def _build_config(self) -> None:
        logger().info('Building %s for %s', self.name, self._config)

        if self.remove_install_dir and self.install_dir.exists():
            shutil.rmtree(self.install_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._touch_autoconfig_files()

        # Write flags to files, to avoid various escaping issues.
        cflags = self._config.cflags + self.cflags
        cxxflags = self._config.cxxflags + self.cxxflags
        ldflags = self._config.ldflags + self.ldflags
        cflags_file = self.output_dir / 'cflags'
        cxxflags_file = self.output_dir / 'cxxflags'
        with cflags_file.open('w') as argfile:
            argfile.write(' '.join(cflags + ldflags))
        with cxxflags_file.open('w') as argfile:
            argfile.write(' '.join(cxxflags + ldflags))

        env = self.env
        # Append CFLAGS after CC since autoconf pre-checks does not use CFLAGS, and we can't pass
        # it without providing -isystem flags.
        env['CC'] = f'{self._cc} @{cflags_file}'
        env['CXX'] = f'{self._cxx} @{cxxflags_file}'

        # Build universal binaries.
        # Cannot add them to cflags_file since autoconf prechecks invokes clang -E and that doesn't
        # work when -arch flags also present.
        if self._config.target_os.is_darwin:
            universal_cflags = f'-arch arm64 -arch x86_64'
            env['CFLAGS'] = universal_cflags
            env['CXXFLAGS'] = universal_cflags

        config_cmd = [str(self.src_dir / 'configure'), f'--prefix={self.install_dir}']
        config_cmd.extend(self.config_flags)
        utils.create_script(self.output_dir / 'config_invocation.sh', config_cmd, env)
        utils.check_call(config_cmd, cwd=self.output_dir, env=env)

        make_cmd = [str(paths.MAKE_BIN_PATH), f'-j{multiprocessing.cpu_count()}']
        utils.check_call(make_cmd, cwd=self.output_dir)

        self.install_config()

    def install_config(self) -> None:
        """Installs built artifacts for current config."""
        install_cmd = [str(paths.MAKE_BIN_PATH), 'install']
        utils.check_call(install_cmd, cwd=self.output_dir)
        if isinstance(self, LibInfo):
            cast(LibInfo, self).update_lib_id()


class CMakeBuilder(Builder):
    """Builder for cmake targets."""
    config: configs.Config
    src_dir: Path
    remove_cmake_cache: bool = False
    remove_install_dir: bool = False
    ninja_targets: List[str] = []

    @property
    def output_dir(self) -> Path:
        """The path for intermediate results."""
        return paths.OUT_DIR / 'lib' / (f'{self.name}{self._config.output_suffix}')

    @property
    def install_dir(self) -> Path:
        """Returns the path this target will be installed to."""
        output_dir = self.output_dir
        return output_dir.parent / (output_dir.name + '-install')

    @property
    def cmake_defines(self) -> Dict[str, str]:
        """CMake defines."""
        cflags = self._config.cflags + self.cflags
        cxxflags = self._config.cxxflags + self.cxxflags
        ldflags = self._config.ldflags + self.ldflags
        cflags_str = ' '.join(cflags)
        cxxflags_str = ' '.join(cxxflags)
        ldflags_str = ' '.join(ldflags)
        defines: Dict[str, str] = {
            'CMAKE_C_COMPILER': str(self._cc),
            'CMAKE_CXX_COMPILER': str(self._cxx),

            'CMAKE_ADDR2LINE': str(self.toolchain.addr2line),
            'CMAKE_AR': str(self.toolchain.ar),
            'CMAKE_LIPO': str(self.toolchain.lipo),
            'CMAKE_NM': str(self.toolchain.nm),
            'CMAKE_OBJCOPY': str(self.toolchain.objcopy),
            'CMAKE_OBJDUMP': str(self.toolchain.objdump),
            'CMAKE_RANLIB': str(self.toolchain.ranlib),
            'CMAKE_RC_COMPILER': str(self.toolchain.rc),
            'CMAKE_RC_FLAGS': f'-I {paths.MINGW_ROOT}/include/',
            'CMAKE_READELF': str(self.toolchain.readelf),
            'CMAKE_STRIP': str(self.toolchain.strip),
            'CMAKE_MT': str(self.toolchain.mt),

            'CMAKE_ASM_FLAGS':  cflags_str,
            'CMAKE_C_FLAGS': cflags_str,
            'CMAKE_CXX_FLAGS': cxxflags_str,

            'CMAKE_EXE_LINKER_FLAGS': ldflags_str,
            'CMAKE_SHARED_LINKER_FLAGS': ldflags_str,
            'CMAKE_MODULE_LINKER_FLAGS': ldflags_str,

            'CMAKE_BUILD_TYPE': 'Release',
            'CMAKE_INSTALL_PREFIX': str(self.install_dir),

            'CMAKE_MAKE_PROGRAM': str(paths.MAKE_BIN_PATH),

            'CMAKE_FIND_ROOT_PATH_MODE_INCLUDE': 'ONLY',
            'CMAKE_FIND_ROOT_PATH_MODE_LIBRARY': 'ONLY',
            'CMAKE_FIND_ROOT_PATH_MODE_PACKAGE': 'ONLY',
            'CMAKE_FIND_ROOT_PATH_MODE_PROGRAM': 'NEVER',

            'CMAKE_POSITION_INDEPENDENT_CODE': 'ON',
        }
        linker = self._config.get_linker(self.toolchain)
        if linker:
            defines['CMAKE_LINKER'] = str(linker)
        if self._config.sysroot:
            defines['CMAKE_SYSROOT'] = str(self._config.sysroot)
        if self._config.target_os == hosts.Host.Android:
            defines['ANDROID'] = '1'
            # Inhibit all of CMake's own NDK handling code.
            defines['CMAKE_SYSTEM_VERSION'] = '1'
        if self._config.target_os.is_darwin:
            # Build universal binaries.
            defines['CMAKE_OSX_ARCHITECTURES'] = 'arm64;x86_64'
        if self._is_cross_compiling():
            # Cross compiling
            defines['CMAKE_SYSTEM_NAME'] = self._get_cmake_system_name()
            defines['CMAKE_SYSTEM_PROCESSOR'] = self._get_cmake_system_arch()
        defines.update(self._config.cmake_defines)
        return defines

    def _get_cmake_system_name(self) -> str:
        return self._config.target_os.value.capitalize()

    def _get_cmake_system_arch(self) -> str:
        return self._config.target_arch.value

    @staticmethod
    def _rm_cmake_cache(cache_dir: Path):
        for dirpath, dirs, files in os.walk(cache_dir):
            if 'CMakeCache.txt' in files:
                os.remove(os.path.join(dirpath, 'CMakeCache.txt'))
            if 'CMakeFiles' in dirs:
                shutil.rmtree(os.path.join(dirpath, 'CMakeFiles'))

    def _ninja(self, args: list[str]) -> None:
        ninja_cmd = [str(paths.MAKE_BIN_PATH)] + args
        utils.check_call(ninja_cmd, cwd=self.output_dir, env=self.env)

    def _build_config(self) -> None:
        if self.remove_cmake_cache:
            self._rm_cmake_cache(self.output_dir)

        if self.remove_install_dir and self.install_dir.exists():
            shutil.rmtree(self.install_dir)

        cmake_cmd: List[str] = [str(paths.CMAKE_BIN_PATH), '-G', 'Unix Makefiles']

        cmake_cmd.extend(f'-D{key}={val}' for key, val in self.cmake_defines.items())
        cmake_cmd.append(str(self.src_dir))

        self.output_dir.mkdir(parents=True, exist_ok=True)

        env = self.env
        utils.create_script(self.output_dir / 'cmake_invocation.sh', cmake_cmd, env)
        utils.check_call(cmake_cmd, cwd=self.output_dir, env=env)
        self.ninja_targets.append("-j8")
        self._ninja(self.ninja_targets)
        self.install_config()

    def install_config(self) -> None:
        """Installs built artifacts for current config."""
        utils.check_call([paths.MAKE_BIN_PATH, 'install'],
                         cwd=self.output_dir, env=self.env)


class LLVMBaseBuilder(CMakeBuilder):  # pylint: disable=abstract-method
    """Base builder for both llvm and individual runtime lib."""

    enable_assertions: bool = False

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines

        if self.enable_assertions:
            defines['LLVM_ENABLE_ASSERTIONS'] = 'ON'
        else:
            defines['LLVM_ENABLE_ASSERTIONS'] = 'OFF'

        # https://github.com/android-ndk/ndk/issues/574 - Don't depend on libtinfo.
        defines['LLVM_ENABLE_TERMINFO'] = 'OFF'
        defines['LLVM_ENABLE_THREADS'] = 'ON'
        defines['LLVM_USE_NEWPM'] = 'ON'
        defines['LLVM_LIBDIR_SUFFIX'] = '64'
        if patch_level := android_version.get_patch_level():
            defines['LLVM_VERSION_PATCH'] = patch_level
        defines['CLANG_REPOSITORY_STRING'] = (
            'https://android.googlesource.com/toolchain/llvm-project')
        defines['BUG_REPORT_URL'] = 'https://github.com/android-ndk/ndk/issues'

        if self._config.target_os.is_darwin:
            # This will be used to set -mmacosx-version-min. And helps to choose SDK.
            # To specify a SDK, set CMAKE_OSX_SYSROOT or SDKROOT environment variable.
            defines['CMAKE_OSX_DEPLOYMENT_TARGET'] = constants.MAC_MIN_VERSION

        # http://b/111885871 - Disable building xray because of MacOS issues.
        defines['COMPILER_RT_BUILD_XRAY'] = 'OFF'

        # To prevent cmake from checking libstdcxx version.
        defines['LLVM_ENABLE_LIBCXX'] = 'ON'

        # Don't depend on the host libatomic library.
        defines['LIBCXX_HAS_ATOMIC_LIB'] = 'NO'

        if not self._config.target_os.is_darwin:
            defines['LLVM_ENABLE_LLD'] = 'ON'

        # Building llvm with tests needs python >= 3.6, which may not be available on build server.
        # So always use prebuilts python.
        target = self._config.target_os
        if target != hosts.Host.Android:
            defines['Python3_LIBRARY'] = str(paths.get_python_lib(target))
            defines['Python3_LIBRARIES'] = str(paths.get_python_lib(target))
            defines['Python3_INCLUDE_DIR'] = str(paths.get_python_include_dir(target))
            defines['Python3_INCLUDE_DIRS'] = str(paths.get_python_include_dir(target))
        defines['Python3_EXECUTABLE'] = str(paths.get_python_executable(hosts.build_host()))

        return defines


class LLVMRuntimeBuilder(LLVMBaseBuilder):  # pylint: disable=abstract-method
    """Base builder for llvm runtime libs."""

    _config: configs.AndroidConfig

    @property
    def install_dir(self) -> Path:
        arch = self._config.target_arch
        if self._config.platform:
            return self.output_toolchain.resource_dir / arch.value
        return self.output_toolchain.path / 'runtimes_ndk_cxx' / arch.value

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines: Dict[str, str] = super().cmake_defines
        defines['LLVM_CONFIG_PATH'] = str(self.toolchain.path /
                                          'bin' / 'llvm-config')
        return defines


class LLVMBuilder(LLVMBaseBuilder):
    """Builder for LLVM project."""

    src_dir: Path = paths.LLVM_PATH / 'llvm'
    config_list: List[configs.Config]
    build_name: str
    build_tags: Optional[List[str]] = None
    svn_revision: str
    enable_assertions: bool = False
    toolchain_name: str

    # lldb options.
    build_lldb: bool = True
    swig_executable: Optional[Path] = None
    libxml2: Optional[LibInfo] = None
    liblzma: Optional[LibInfo] = None
    libedit: Optional[LibInfo] = None
    libncurses: Optional[LibInfo] = None

    @property
    def install_dir(self) -> Path:
        return paths.OUT_DIR / f'{self.name}-install'

    @property
    def output_dir(self) -> Path:
        return paths.OUT_DIR / self.name

    @property
    def llvm_projects(self) -> Set[str]:
        """Returns enabled llvm projects."""
        raise NotImplementedError()

    @property
    def llvm_targets(self) -> Set[str]:
        """Returns llvm target archtects to build."""
        raise NotImplementedError()

    def _set_lldb_flags(self, target: hosts.Host, defines: Dict[str, str]) -> None:
        """Sets cmake defines for lldb."""
        if target.is_darwin:
            # Avoids the build of debug server. It is only used in testing.
            defines['LLDB_USE_SYSTEM_DEBUGSERVER'] = 'ON'

        defines['LLDB_ENABLE_LUA'] = 'OFF'

        if self.swig_executable:
            defines['SWIG_EXECUTABLE'] = str(self.swig_executable)
            defines['LLDB_ENABLE_PYTHON'] = 'ON'
            defines['LLDB_EMBED_PYTHON_HOME'] = 'OFF'
        else:
            defines['LLDB_ENABLE_PYTHON'] = 'OFF'

        if self.liblzma:
            defines['LLDB_ENABLE_LZMA'] = 'ON'
            defines['LIBLZMA_INCLUDE_DIR'] = str(self.liblzma.include_dir)
            defines['LIBLZMA_LIBRARY'] = str(self.liblzma.link_libraries[0])
        else:
            defines['LLDB_ENABLE_LZMA'] = 'OFF'

        if self.libedit:
            defines['LLDB_ENABLE_LIBEDIT'] = 'ON'
            defines['LibEdit_INCLUDE_DIRS'] = str(self.libedit.include_dir)
            defines['LibEdit_LIBRARIES'] = str(self.libedit.link_libraries[0])
        else:
            defines['LLDB_ENABLE_LIBEDIT'] = 'OFF'

        if self.libxml2:
            defines['LLDB_ENABLE_LIBXML2'] = 'ON'
        else:
            defines['LLDB_ENABLE_LIBXML2'] = 'OFF'

        if self.libncurses:
            defines['LLDB_ENABLE_CURSES'] = 'ON'
            defines['CURSES_INCLUDE_DIRS'] = str(self.libncurses.include_dir)
            curses_libs = ';'.join(str(lib) for lib in self.libncurses.link_libraries)
            defines['CURSES_LIBRARIES'] = curses_libs
            defines['PANEL_LIBRARIES'] = curses_libs
        else:
            defines['LLDB_ENABLE_CURSES'] = 'OFF'

    def _install_lldb_deps(self) -> None:
        lib_dir = self.install_dir / ('bin' if self._config.target_os.is_windows else 'lib')
        lib_dir.mkdir(exist_ok=True, parents=True)

        if self.swig_executable:
            python_prebuilt_dir = paths.get_python_dir(self._config.target_os)
            python_dest_dir = self.install_dir / 'python3'
            shutil.copytree(python_prebuilt_dir, python_dest_dir, symlinks=True, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns('*.pyc', '__pycache__', 'Android.bp',
                                                          '.git', '.gitignore'))

        for lib in (self.liblzma, self.libedit, self.libxml2, self.libncurses):
            if lib:
                for lib_file in lib.install_libraries:
                    shutil.copy2(lib_file, lib_dir)
                for link in lib.symlinks:
                    shutil.copy2(link, lib_dir, follow_symlinks=False)

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines

        defines['LLVM_ENABLE_PROJECTS'] = ';'.join(sorted(self.llvm_projects))

        defines['LLVM_TARGETS_TO_BUILD'] = ';'.join(sorted(self.llvm_targets))
        if self._config.target_os.is_darwin or self._config.target_os.is_linux:
            defines['LLVM_BUILD_LLVM_DYLIB'] = 'ON'
        elif isinstance(self._config, configs.MSVCConfig):
            defines['LLVM_BUILD_LLVM_C_DYLIB'] = 'OFF'

        if self.build_tags:
            tags_str = ''.join(tag + ', ' for tag in self.build_tags)
        else:
            tags_str = ''

        defines['CLANG_VENDOR'] = 'Android ({}, {}based on {})'.format(
            self.build_name, tags_str, self.svn_revision)

        defines['LLVM_BUILD_RUNTIME'] = 'ON'

        defines['LLVM_INCLUDE_GO_TESTS'] = 'OFF'

        if self._config.target_os.is_darwin:
            defines['HAVE_LIBCOMPRESSION'] = '1'
            defines['HAVE_FUTIMENS'] = '1'
            # Codesign binaries with an ad-hoc certificate, mandatory for arm64 Darwin.
            defines['LLVM_CODESIGNING_IDENTITY'] = '-'

        # libxml2 is used by lld and lldb.
        if self.libxml2:
            defines['LIBXML2_INCLUDE_DIR'] = str(self.libxml2.include_dir)
            defines['LIBXML2_LIBRARY'] = str(self.libxml2.link_libraries[0])

        if self.build_lldb:
            self._set_lldb_flags(self._config.target_os, defines)

        return defines

    def install_config(self) -> None:
        super().install_config()
        self._install_lldb_deps()

    @functools.cached_property
    def installed_toolchain(self) -> toolchains.Toolchain:
        """Gets the built Toolchain."""
        return toolchains.Toolchain(self.install_dir, self.output_dir)

    def test(self) -> None:
        with timer.Timer(f'stage2_test'):
            self._ninja(
                ['check-clang', 'check-llvm', 'check-clang-tools', 'check-cxx'])
        # Known failed tests:
        #   Clang :: CodeGenCXX/builtins.cpp
        #   Clang :: CodeGenCXX/unknown-anytype.cpp
        #   Clang :: Sema/builtin-setjmp.c
        #   LLVM :: Bindings/Go/go.test (disabled by LLVM_INCLUDE_GO_TESTS=OFF)
        #   LLVM :: CodeGen/X86/extractelement-fp.ll
        #   LLVM :: CodeGen/X86/fp-round.ll
