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
"""APIs for build configurations."""

from pathlib import Path
from typing import Dict, List, Optional
import functools
import json

import hosts
import paths
import toolchains
import win_sdk

class Config:
    """Base configuration."""

    name: str
    target_os: hosts.Host
    target_arch: hosts.Arch = hosts.Arch.X86_64
    sysroot: Optional[Path] = None

    """Additional config data that a builder can specify."""
    extra_config = None

    @property
    def llvm_triple(self) -> str:
        raise NotImplementedError()

    def get_c_compiler(self, toolchain: toolchains.Toolchain) -> Path:
        """Returns path to c compiler."""
        return toolchain.cc

    def get_cxx_compiler(self, toolchain: toolchains.Toolchain) -> Path:
        """Returns path to c++ compiler."""
        return toolchain.cxx

    def get_linker(self, toolchain: toolchains.Toolchain) -> Optional[Path]:
        """Returns the path to linker."""
        return None

    @property
    def cflags(self) -> List[str]:
        """Returns a list of flags for c compiler."""
        return []

    @property
    def cxxflags(self) -> List[str]:
        """Returns a list of flags used for cxx compiler."""
        return self.cflags

    @property
    def ldflags(self) -> List[str]:
        """Returns a list of flags for static linker."""
        return []

    @property
    def env(self) -> Dict[str, str]:
        return {}

    def __str__(self) -> str:
        return self.target_os.name

    @property
    def output_suffix(self) -> str:
        """The suffix of output directory name."""
        return f'-{self.target_os.value}'

    @property
    def cmake_defines(self) -> Dict[str, str]:
        """Additional defines for cmake."""
        return dict()


class _BaseConfig(Config):  # pylint: disable=abstract-method
    """Base configuration."""

    use_lld: bool = True
    target_os: hosts.Host

    @property
    def cflags(self) -> List[str]:
        cflags: List[str] = [f'-ffile-prefix-map={paths.ANDROID_DIR}/=']
        cflags.extend(f'-B{d}' for d in self.bin_dirs)
        return cflags

    @property
    def ldflags(self) -> List[str]:
        ldflags: List[str] = []
        for lib_dir in self.lib_dirs:
            ldflags.append(f'-B{lib_dir}')
            ldflags.append(f'-L{lib_dir}')
        if self.use_lld:
            ldflags.append('-fuse-ld=lld')
        return ldflags

    @property
    def bin_dirs(self) -> List[Path]:
        """Paths to binaries used in cflags."""
        return []

    @property
    def lib_dirs(self) -> List[Path]:
        """Paths to libraries used in ldflags."""
        return []

    def get_linker(self, toolchain: toolchains.Toolchain) -> Optional[Path]:
        if self.use_lld:
            return toolchain.lld
        return None


class DarwinConfig(_BaseConfig):
    """Configuration for Darwin targets."""

    target_os: hosts.Host = hosts.Host.Darwin
    use_lld: bool = False

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        # Fails if an API used is newer than what specified in -mmacosx-version-min.
        cflags.append('-Werror=unguarded-availability')
        return cflags


class _GccConfig(_BaseConfig):  # pylint: disable=abstract-method
    """Base config to use gcc libs."""

    gcc_root: Path
    gcc_triple: str
    gcc_ver: str

    def __init__(self, is_32_bit: bool = False):
        self.is_32_bit = is_32_bit

    @property
    def bin_dirs(self) -> List[Path]:
        return [self.gcc_root / self.gcc_triple / 'bin']

    @property
    def lib_dirs(self) -> List[Path]:
        gcc_lib_dir = self.gcc_root / 'lib' / 'gcc' / self.gcc_triple / self.gcc_ver
        if self.is_32_bit:
            gcc_lib_dir = gcc_lib_dir / '32'
            gcc_builtin_dir = self.gcc_root/'lib' / self.gcc_triple
        else:
            gcc_builtin_dir = self.gcc_root/'lib' / self.gcc_triple
        return [gcc_lib_dir, gcc_builtin_dir]


class LinuxConfig(_GccConfig):
    """Configuration for Linux targets."""

    target_os: hosts.Host = hosts.Host.Linux
    sysroot: Optional[Path] = Path('/')
    gcc_root: Path = Path('/usr')
    gcc_triple: str = 'aarch64-linux-gnu'
    gcc_ver: str = '10'

    @property
    def ldflags(self) -> List[str]:
        return super().ldflags + [
            '-Wl,--hash-style=both',
        ]


class MinGWConfig(_GccConfig):
    """Configuration for MinGW targets."""

    target_os: hosts.Host = hosts.Host.Windows
    gcc_root: Path = (paths.GCC_ROOT / 'host' / 'x86_64-w64-mingw32-4.8')
    sysroot: Optional[Path] = gcc_root / 'x86_64-w64-mingw32'
    gcc_triple: str = 'x86_64-w64-mingw32'
    gcc_ver: str = '4.8.3'

    @property
    def llvm_triple(self) -> str:
        return 'x86_64-pc-windows-gnu'

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        cflags.append(f'--target={self.llvm_triple}')
        cflags.append('-D_LARGEFILE_SOURCE')
        cflags.append('-D_FILE_OFFSET_BITS=64')
        cflags.append('-D_WIN32_WINNT=0x0600')
        cflags.append('-DWINVER=0x0600')
        cflags.append('-D__MSVCRT_VERSION__=0x1400')
        return cflags


class MSVCConfig(Config):
    """Configuration for MSVC toolchain."""
    target_os: hosts.Host = hosts.Host.Windows

    # We still use lld but don't want -fuse-ld=lld in linker flags.
    use_lld: bool = False

    def get_c_compiler(self, toolchain: toolchains.Toolchain) -> Path:
        return toolchain.cl

    def get_cxx_compiler(self, toolchain: toolchains.Toolchain) -> Path:
        return toolchain.cl

    def get_linker(self, toolchain: toolchains.Toolchain) -> Optional[Path]:
        return toolchain.lld_link

    @functools.cached_property
    def _read_env_setting(self) -> Dict[str, str]:
        sdk_path = win_sdk.get_path()
        assert sdk_path is not None
        base_path = sdk_path / 'bin'
        with (base_path / 'SetEnv.x64.json').open('r') as env_file:
            env_setting = json.load(env_file)
        return {key: ';'.join(str(base_path.joinpath(*v)) for v in value)
                for key, value in env_setting['env'].items()}

    @property
    def llvm_triple(self) -> str:
        return 'x86_64-pc-windows-msvc',

    @property
    def cflags(self) -> List[str]:
        return super().cflags + [
            '-w',
            '-fuse-ld=lld',
            '--target={self.llvm_triple}',
            '-fms-compatibility-version=19.10',
            '-D_HAS_EXCEPTIONS=1',
            '-D_CRT_STDIO_ISO_WIDE_SPECIFIERS'
        ]

    @property
    def ldflags(self) -> List[str]:
        return super().ldflags + [
            '/MANIFEST:NO',
        ]

    @property
    def env(self) -> Dict[str, str]:
        return self._read_env_setting

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        defines['CMAKE_POLICY_DEFAULT_CMP0091'] = 'NEW'
        defines['CMAKE_MSVC_RUNTIME_LIBRARY'] = 'MultiThreaded'
        return defines


class AndroidConfig(_BaseConfig):
    """Config for Android targets."""

    target_os: hosts.Host = hosts.Host.Android

    target_arch: hosts.Arch
    _toolchain_path: Path

    static: bool = False
    platform: bool = False
    suppress_libcxx_headers: bool = False

    @property
    def base_llvm_triple(self) -> str:
        """Get base LLVM triple (without API level)."""
        return f'{self.target_arch.llvm_arch}-linux-android'

    @property
    def llvm_triple(self) -> str:
        """Get LLVM triple (with API level)."""
        return f'{self.base_llvm_triple}{self.api_level}'

    @property
    def ndk_arch(self) -> str:
        """Converts to ndk arch."""
        return {
            hosts.Arch.ARM: 'arm',
            hosts.Arch.AARCH64: 'arm64',
            hosts.Arch.I386: 'x86',
            hosts.Arch.X86_64: 'x86_64',
        }[self.target_arch]

    @property
    def ndk_sysroot_triple(self) -> str:
        """Triple used to identify NDK sysroot."""
        if self.target_arch == hosts.Arch.ARM:
            return 'arm-linux-androideabi'
        return self.base_llvm_triple

    @property
    def sysroot(self) -> Path:  # type: ignore
        """Returns sysroot path."""
        platform_or_ndk = 'platform' if self.platform else 'ndk'
        return paths.SYSROOTS / platform_or_ndk / self.ndk_arch

    @property
    def ldflags(self) -> List[str]:
        ldflags = super().ldflags
        ldflags.append('-rtlib=compiler-rt')
        ldflags.append('-Wl,-z,defs')
        ldflags.append('-Wl,--gc-sections')
        ldflags.append('-Wl,--build-id=sha1')
        ldflags.append('-pie')
        if self.static:
            ldflags.append('-static')
        return ldflags

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        toolchain_bin = paths.GCC_ROOT / self._toolchain_path / 'bin'
        cflags.append(f'--target={self.llvm_triple}')
        cflags.append(f'-B{toolchain_bin}')
        cflags.append('-ffunction-sections')
        cflags.append('-fdata-sections')
        return cflags

    @property
    def _libcxx_header_dirs(self) -> List[Path]:
        # For the NDK, the sysroot has the C++ headers.
        assert self.platform
        if self.suppress_libcxx_headers:
            return []
        # <prebuilts>/include/c++/v1 includes the cxxabi headers
        return [
            paths.CLANG_PREBUILT_LIBCXX_HEADERS,
            # The platform sysroot also has Bionic headers from an NDK release,
            # but override them with the current headers.
            paths.BIONIC_HEADERS,
            paths.BIONIC_KERNEL_HEADERS,
        ]

    @property
    def cxxflags(self) -> List[str]:
        cxxflags = super().cxxflags
        if self.platform:
            # For the NDK, the sysroot has the C++ headers, but for the
            # platform, we need to add the headers manually.
            cxxflags.append('-nostdinc++')
            for d in self._libcxx_header_dirs:
                if(d==Path('/usr/include/c++/10')):
                    cxxflags.append('-isystem '+'/root/llvm-toolchain/out/stage2-install/include/c++/10')
                else:
                    cxxflags.append(f'-isystem {d}')
        return cxxflags

    @property
    def api_level(self) -> int:
        if self.static or self.platform:
            # Set API level for platform to to 29 since these runtimes can be
            # used for apexes targeting that API level.
            return 29
        if self.target_arch in [hosts.Arch.ARM, hosts.Arch.I386]:
            return 19
        return 21

    def __str__(self) -> str:
        return (f'{self.target_os.name}-{self.target_arch.name} ' +
                f'(platform={self.platform} static={self.static} {self.extra_config})')

    @property
    def output_suffix(self) -> str:
        suffix = f'-{self.target_arch.value}'
        if not self.platform:
            suffix += '-ndk-cxx'
        return suffix


class AndroidARMConfig(AndroidConfig):
    """Configs for android arm targets."""
    target_arch: hosts.Arch = hosts.Arch.ARM
    _toolchain_path: Path = Path('arm/arm-linux-androideabi-4.9/arm-linux-androideabi')

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        cflags.append('-march=armv7-a')
        return cflags


class AndroidAArch64Config(AndroidConfig):
    """Configs for android arm64 targets."""
    target_arch: hosts.Arch = hosts.Arch.AARCH64
    _toolchain_path: Path = Path('aarch64/aarch64-linux-android-4.9/aarch64-linux-android')

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        cflags.append('-mbranch-protection=standard')
        return cflags


class AndroidX64Config(AndroidConfig):
    """Configs for android x86_64 targets."""
    target_arch: hosts.Arch = hosts.Arch.X86_64
    _toolchain_path: Path = Path('x86/x86_64-linux-android-4.9/x86_64-linux-android')


class AndroidI386Config(AndroidConfig):
    """Configs for android x86 targets."""
    target_arch: hosts.Arch = hosts.Arch.I386
    _toolchain_path: Path = Path('x86/x86_64-linux-android-4.9/x86_64-linux-android')

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        cflags.append('-m32')
        return cflags


def _get_default_host_config() -> Config:
    """Returns the Config matching the current machine."""
    return {
        hosts.Host.Linux: LinuxConfig,
        hosts.Host.Darwin: DarwinConfig,
        hosts.Host.Windows: MinGWConfig
    }[hosts.build_host()]()


_HOST_CONFIG: Config = _get_default_host_config()


def host_config() -> Config:
    """Returns the cached Host matching the current machine."""
    global _HOST_CONFIG  # pylint: disable=global-statement
    return _HOST_CONFIG

def android_configs(platform: bool=True,
                    static: bool=False,
                    suppress_libcxx_headers: bool=False,
                    extra_config=None) -> List[Config]:
    """Returns a list of configs for android builds."""
    configs = [
        AndroidARMConfig(),
        AndroidAArch64Config(),
        AndroidI386Config(),
        AndroidX64Config(),
    ]
    for config in configs:
        config.static = static
        config.platform = platform
        config.suppress_libcxx_headers = suppress_libcxx_headers
        config.extra_config = extra_config
    # List is not covariant. Explicit convert is required to make it List[Config].
    return list(configs)
