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
"""Builder instances for various targets."""

from pathlib import Path
from typing import cast, Dict, Iterator, List, Optional, Set
import contextlib
import os
import re
import shutil
import textwrap
import timer

import base_builders
import configs
import constants
import hosts
import mapfile
import paths
import utils

class AsanMapFileBuilder(base_builders.Builder):
    name: str = 'asan-mapfile'
    config_list: List[configs.Config] = configs.android_configs()

    def _build_config(self) -> None:
        arch = self._config.target_arch
        # We can not build asan_test using current CMake building system. Since
        # those files are not used to build AOSP, we just simply touch them so that
        # we can pass the build checks.
        asan_test_path = self.output_toolchain.path / 'test' / arch.llvm_arch / 'bin'
        asan_test_path.mkdir(parents=True, exist_ok=True)
        asan_test_bin_path = asan_test_path / 'asan_test'
        asan_test_bin_path.touch(exist_ok=True)

        lib_dir = self.output_toolchain.resource_dir
        self._build_sanitizer_map_file('asan', arch, lib_dir)
        self._build_sanitizer_map_file('ubsan_standalone', arch, lib_dir)

        if arch == hosts.Arch.AARCH64:
            self._build_sanitizer_map_file('hwasan', arch, lib_dir)

    @staticmethod
    def _build_sanitizer_map_file(san: str, arch: hosts.Arch, lib_dir: Path) -> None:
        lib_file = lib_dir / f'libclang_rt.{san}-{arch.llvm_arch}-android.so'
        map_file = lib_dir / f'libclang_rt.{san}-{arch.llvm_arch}-android.map.txt'
        mapfile.create_map_file(lib_file, map_file)


class Stage1Builder(base_builders.LLVMBuilder):
    name: str = 'stage1'
    install_dir: Path = paths.OUT_DIR / 'stage1-install'
    build_android_targets: bool = False
    build_extra_tools: bool = False
    config_list: List[configs.Config] = [configs.host_config()]

    @property
    def llvm_targets(self) -> Set[str]:
        if self.build_android_targets:
            return constants.HOST_TARGETS | constants.ANDROID_TARGETS
        else:
            return constants.HOST_TARGETS

    @property
    def llvm_projects(self) -> Set[str]:
        proj = {'clang', 'lld', 'libcxxabi', 'libcxx', 'compiler-rt'}
        if self.build_extra_tools:
            proj.add('clang-tools-extra')
        if self.build_lldb:
            proj.add('lldb')
        return proj

    @property
    def ldflags(self) -> List[str]:
        ldflags = super().ldflags
        # Use -static-libstdc++ to statically link the c++ runtime [1].  This
        # avoids specifying self.toolchain.lib_dir in rpath to find libc++ at
        # runtime.
        # [1] libc++ in our case, despite the flag saying -static-libstdc++.
        ldflags.append('-static-libstdc++')
        return ldflags

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        defines['CLANG_ENABLE_ARCMT'] = 'OFF'
        if not self.build_extra_tools:
            defines['CLANG_ENABLE_STATIC_ANALYZER'] = 'OFF'

        defines['LLVM_BUILD_TOOLS'] = 'ON'

        # Make libc++.so a symlink to libc++.so.x instead of a linker script that
        # also adds -lc++abi.  Statically link libc++abi to libc++ so it is not
        # necessary to pass -lc++abi explicitly.  This is needed only for Linux.
        if self._config.target_os.is_linux:
            defines['LIBCXX_ENABLE_ABI_LINKER_SCRIPT'] = 'OFF'
            defines['LIBCXX_ENABLE_STATIC_ABI_LIBRARY'] = 'ON'

        # Do not build compiler-rt for Darwin.  We don't ship host (or any
        # prebuilt) runtimes for Darwin anyway.  Attempting to build these will
        # fail compilation of lib/builtins/atomic_*.c that only get built for
        # Darwin and fail compilation due to us using the bionic version of
        # stdatomic.h.
        if self._config.target_os.is_darwin:
            defines['LLVM_BUILD_EXTERNAL_COMPILER_RT'] = 'ON'

        # Don't build libfuzzer as part of the first stage build.
        defines['COMPILER_RT_BUILD_LIBFUZZER'] = 'OFF'

        return defines

    def test(self) -> None:
        with timer.Timer(f'stage1_test'):
            self._ninja(['check-clang', 'check-llvm', 'check-clang-tools'])
        # stage1 cannot run check-cxx yet


class Stage2Builder(base_builders.LLVMBuilder):
    name: str = 'stage2'
    install_dir: Path = paths.OUT_DIR / 'stage2-install'
    config_list: List[configs.Config] = [configs.host_config()]
    remove_install_dir: bool = True
    debug_build: bool = False
    build_instrumented: bool = False
    profdata_file: Optional[Path] = None
    lto: bool = True

    @property
    def llvm_targets(self) -> Set[str]:
        return constants.ANDROID_TARGETS

    @property
    def llvm_projects(self) -> Set[str]:
        proj = {'clang', 'lld', 'libcxxabi', 'libcxx', 'compiler-rt',
                'clang-tools-extra', 'polly'}
        if self.build_lldb:
            proj.add('lldb')
        return proj

    @property
    def env(self) -> Dict[str, str]:
        env = super().env
        # Point CMake to the libc++ from stage1.  It is possible that once built,
        # the newly-built libc++ may override this because of the rpath pointing to
        # $ORIGIN/../lib.  That'd be fine because both libraries are built from
        # the same sources.
        env['LD_LIBRARY_PATH'] = str(self.toolchain.lib_dir)
        return env

    @property
    def ldflags(self) -> List[str]:
        ldflags = super().ldflags
        if self.build_instrumented:
            # Building libcxx, libcxxabi with instrumentation causes linker errors
            # because these are built with -nodefaultlibs and prevent libc symbols
            # needed by libclang_rt.profile from being resolved.  Manually adding
            # the libclang_rt.profile to linker flags fixes the issue.
            resource_dir = self.toolchain.resource_dir
            ldflags.append(str(resource_dir / 'libclang_rt.profile-x86_64.a'))
        return ldflags

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        if self.profdata_file:
            cflags.append('-Wno-profile-instr-out-of-date')
            cflags.append('-Wno-profile-instr-unprofiled')
        return cflags

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        defines['SANITIZER_ALLOW_CXXABI'] = 'OFF'
        defines['CLANG_PYTHON_BINDINGS_VERSIONS'] = '3'

        if (self.lto and
                not self._config.target_os.is_darwin and
                not self.build_instrumented and
                not self.debug_build):
            defines['LLVM_ENABLE_LTO'] = 'Thin'

        # Build libFuzzer here to be exported for the host fuzzer builds. libFuzzer
        # is not currently supported on Darwin.
        if self._config.target_os.is_darwin:
            defines['COMPILER_RT_BUILD_LIBFUZZER'] = 'OFF'
        else:
            defines['COMPILER_RT_BUILD_LIBFUZZER'] = 'ON'

        if self.debug_build:
            defines['CMAKE_BUILD_TYPE'] = 'Debug'

        if self.build_instrumented:
            defines['LLVM_BUILD_INSTRUMENTED'] = 'ON'

            # llvm-profdata is only needed to finish CMake configuration
            # (tools/clang/utils/perf-training/CMakeLists.txt) and not needed for
            # build
            llvm_profdata = self.toolchain.path / 'bin' / 'llvm-profdata'
            defines['LLVM_PROFDATA'] = str(llvm_profdata)
        elif self.profdata_file:
            defines['LLVM_PROFDATA_FILE'] = str(self.profdata_file)

        # Make libc++.so a symlink to libc++.so.x instead of a linker script that
        # also adds -lc++abi.  Statically link libc++abi to libc++ so it is not
        # necessary to pass -lc++abi explicitly.  This is needed only for Linux.
        if self._config.target_os.is_linux:
            defines['LIBCXX_ENABLE_STATIC_ABI_LIBRARY'] = 'ON'
            defines['LIBCXX_ENABLE_ABI_LINKER_SCRIPT'] = 'OFF'
            defines['LIBCXX_TEST_COMPILER_FLAGS'] = defines['CMAKE_CXX_FLAGS']
            defines['LIBCXX_TEST_LINKER_FLAGS'] = defines['CMAKE_EXE_LINKER_FLAGS']

        # Do not build compiler-rt for Darwin.  We don't ship host (or any
        # prebuilt) runtimes for Darwin anyway.  Attempting to build these will
        # fail compilation of lib/builtins/atomic_*.c that only get built for
        # Darwin and fail compilation due to us using the bionic version of
        # stdatomic.h.
        if self._config.target_os.is_darwin:
            defines['LLVM_BUILD_EXTERNAL_COMPILER_RT'] = 'ON'

        return defines

    def install_config(self) -> None:
        super().install_config()
        lldb_wrapper_path = self.install_dir / 'bin' / 'lldb.sh'
        lib_path_env = 'LD_LIBRARY_PATH' if self._config.target_os.is_linux else 'DYLD_LIBRARY_PATH'
        lldb_wrapper_path.write_text(textwrap.dedent(f"""\
            #!/bin/bash
            CURDIR=$(cd $(dirname $0) && pwd)
            export PYTHONHOME="$CURDIR/../python3"
            export {lib_path_env}="$CURDIR/../python3/lib:${lib_path_env}"
            "$CURDIR/lldb" "$@"
        """))
        lldb_wrapper_path.chmod(0o755)


class BuiltinsBuilder(base_builders.LLVMRuntimeBuilder):
    name: str = 'builtins'
    src_dir: Path = paths.LLVM_PATH / 'compiler-rt' / 'lib' / 'builtins'

    # Only target the NDK, not the platform. The NDK copy is sufficient for the
    # platform builders, and both NDK+platform builders use the same toolchain,
    # which can only have a single copy installed into its resource directory.
    @property
    def config_list(self) -> List[configs.Config]:
        result = configs.android_configs(platform=False, extra_config={'is_exported': False})
        # For arm32 and x86, build a special version of the builtins library
        # where the symbols are exported, not hidden. This version is needed
        # to continue exporting builtins from libc.so and libm.so.
        for arch in [configs.AndroidARMConfig(), configs.AndroidI386Config()]:
            arch.platform = False
            arch.extra_config = {'is_exported': True}
            result.append(arch)
        return result

    @property
    def is_exported(self) -> bool:
        return cast(Dict[str, bool], self._config.extra_config)['is_exported']

    @property
    def output_dir(self) -> Path:
        old_path = super().output_dir
        suffix = '-exported' if self.is_exported else ''
        return old_path.parent / (old_path.name + suffix)

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        arch = self._config.target_arch
        defines['COMPILER_RT_BUILTINS_HIDE_SYMBOLS'] = \
            'TRUE' if not self.is_exported else 'FALSE'
        defines['COMPILER_RT_DEFAULT_TARGET_TRIPLE'] = self._config.llvm_triple
        # For CMake feature testing, create an archive instead of an executable,
        # because we can't link an executable until builtins have been built.
        defines['CMAKE_TRY_COMPILE_TARGET_TYPE'] = 'STATIC_LIBRARY'
        defines['COMPILER_RT_EXCLUDE_ATOMIC_BUILTIN'] = 'OFF'
        return defines

    def install_config(self) -> None:
        # Copy the library into the toolchain resource directory (lib/linux) and
        # runtimes_ndk_cxx.
        arch = self._config.target_arch
        sarch = 'i686' if arch == hosts.Arch.I386 else arch.value
        filename = 'libclang_rt.builtins-' + sarch + '-android.a'
        filename_exported = 'libclang_rt.builtins-' + sarch + '-android-exported.a'

        src_path = self.output_dir / 'lib' / 'linux' / filename
        print(src_path)
        print(self.output_toolchain.resource_dir / filename)
        if self.is_exported:
            # This special copy exports its symbols and is only intended for use
            # in Bionic's libc.so.
            shutil.copy2(src_path, self.output_toolchain.resource_dir / filename_exported)
        else:
            shutil.copy2(src_path, self.output_toolchain.resource_dir / filename)

            # Also install to self.toolchain.resource_dir, if it's different,
            # for use when building target libraries.
            if self.toolchain.resource_dir != self.output_toolchain.resource_dir:
                shutil.copy2(src_path, self.toolchain.resource_dir / filename)

            # Make a copy for the NDK.
            dst_dir = self.output_toolchain.path / 'runtimes_ndk_cxx'
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst_dir / filename)


class CompilerRTBuilder(base_builders.LLVMRuntimeBuilder):
    name: str = 'compiler-rt'
    src_dir: Path = paths.LLVM_PATH / 'compiler-rt'
    config_list: List[configs.Config] = (
        configs.android_configs(platform=True) +
        configs.android_configs(platform=False)
    )

    @property
    def install_dir(self) -> Path:
        if self._config.platform:
            return self.output_toolchain.clang_lib_dir
        # Installs to a temporary dir and copies to runtimes_ndk_cxx manually.
        output_dir = self.output_dir
        return output_dir.parent / (output_dir.name + '-install')

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        arch = self._config.target_arch
        defines['COMPILER_RT_BUILD_BUILTINS'] = 'OFF'
        defines['COMPILER_RT_USE_BUILTINS_LIBRARY'] = 'ON'
        # FIXME: Disable WError build until upstream fixed the compiler-rt
        # personality routine warnings caused by r309226.
        # defines['COMPILER_RT_ENABLE_WERROR'] = 'ON'
        defines['COMPILER_RT_TEST_COMPILER_CFLAGS'] = defines['CMAKE_C_FLAGS']
        defines['COMPILER_RT_DEFAULT_TARGET_TRIPLE'] = self._config.llvm_triple
        defines['COMPILER_RT_INCLUDE_TESTS'] = 'OFF'
        defines['SANITIZER_CXX_ABI'] = 'libcxxabi'
        # With CMAKE_SYSTEM_NAME='Android', compiler-rt will be installed to
        # lib/android instead of lib/linux.
        del defines['CMAKE_SYSTEM_NAME']
        libs: List[str] = []
        if self._config.api_level < 21:
            libs += ['-landroid_support']
        # Currently, -rtlib=compiler-rt (even with -unwindlib=libunwind) does
        # not automatically link libunwind.a on Android.
        libs += ['-lunwind']
        defines['SANITIZER_COMMON_LINK_LIBS'] = ' '.join(libs)
        # compiler-rt's CMakeLists.txt file deletes -Wl,-z,defs from
        # CMAKE_SHARED_LINKER_FLAGS when COMPILER_RT_USE_BUILTINS_LIBRARY is
        # set. We want this flag on instead to catch unresolved references
        # early.
        defines['SANITIZER_COMMON_LINK_FLAGS'] = '-Wl,-z,defs'
        if self._config.platform:
            defines['COMPILER_RT_HWASAN_WITH_INTERCEPTORS'] = 'OFF'
        return defines

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        cflags.append('-funwind-tables')
        return cflags

    def install_config(self) -> None:
        # Still run `ninja install`.
        super().install_config()

        # Install the fuzzer library to the old {arch}/libFuzzer.a path for
        # backwards compatibility.
        arch = self._config.target_arch
        sarch = 'i686' if arch == hosts.Arch.I386 else arch.value
        static_lib_filename = 'libclang_rt.fuzzer-' + sarch + '-android.a'

        lib_dir = self.install_dir / 'lib' / 'linux'
        arch_dir = lib_dir / arch.value
        arch_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(lib_dir / static_lib_filename, arch_dir / 'libFuzzer.a')

        if not self._config.platform:
            dst_dir = self.output_toolchain.path / 'runtimes_ndk_cxx'
            shutil.copytree(lib_dir, dst_dir, dirs_exist_ok=True)

    def install(self) -> None:
        # Install libfuzzer headers once for all configs.
        header_src = self.src_dir / 'lib' / 'fuzzer'
        header_dst = self.output_toolchain.path / 'prebuilt_include' / 'llvm' / 'lib' / 'Fuzzer'
        header_dst.mkdir(parents=True, exist_ok=True)
        for f in header_src.iterdir():
            if f.suffix in ('.h', '.def'):
                shutil.copy2(f, header_dst)

        symlink_path = self.output_toolchain.resource_dir / 'libclang_rt.hwasan_static-aarch64-android.a'
        symlink_path.unlink(missing_ok=True)
        os.symlink('libclang_rt.hwasan-aarch64-android.a', symlink_path)


class CompilerRTHostI386Builder(base_builders.LLVMRuntimeBuilder):
    name: str = 'compiler-rt-i386-host'
    src_dir: Path = paths.LLVM_PATH / 'compiler-rt'
    config_list: List[configs.Config] = [configs.LinuxConfig(is_32_bit=True)]

    @property
    def install_dir(self) -> Path:
        return self.output_toolchain.clang_lib_dir

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        # Due to CMake and Clang oddities, we need to explicitly set
        # CMAKE_C_COMPILER_TARGET and use march=i686 in cflags below instead of
        # relying on auto-detection from the Compiler-rt CMake files.
        defines['CMAKE_C_COMPILER_TARGET'] = 'i386-linux-gnu'
        defines['COMPILER_RT_INCLUDE_TESTS'] = 'ON'
        defines['COMPILER_RT_ENABLE_WERROR'] = 'ON'
        defines['SANITIZER_CXX_ABI'] = 'libstdc++'
        return defines

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        # compiler-rt/lib/gwp_asan uses PRIu64 and similar format-specifier macros.
        # Add __STDC_FORMAT_MACROS so their definition gets included from
        # inttypes.h.  This explicit flag is only needed here.  64-bit host runtimes
        # are built in stage1/stage2 and get it from the LLVM CMake configuration.
        # These are defined unconditionaly in bionic and newer glibc
        # (https://sourceware.org/git/gitweb.cgi?p=glibc.git;h=1ef74943ce2f114c78b215af57c2ccc72ccdb0b7)
        cflags.append('-D__STDC_FORMAT_MACROS')
        cflags.append('--target=i386-linux-gnu')
        cflags.append('-march=i686')
        return cflags

    def _build_config(self) -> None:
        # Also remove the "stamps" created for the libcxx included in libfuzzer so
        # CMake runs the configure again (after the cmake caches are deleted).
        stamp_path = self.output_dir / 'lib' / 'fuzzer' / 'libcxx_fuzzer_i386-stamps'
        if stamp_path.exists():
            shutil.rmtree(stamp_path)
        super()._build_config()


class CompilerRTHostAarch64Builder(base_builders.LLVMRuntimeBuilder):
    name: str = 'compiler-rt-aarch64-host'
    src_dir: Path = paths.LLVM_PATH / 'compiler-rt'
    config_list: List[configs.Config] = [configs.LinuxConfig(is_32_bit=False)]

    @property
    def install_dir(self) -> Path:
        return self.output_toolchain.clang_lib_dir

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        # Due to CMake and Clang oddities, we need to explicitly set
        # CMAKE_C_COMPILER_TARGET and use march=i686 in cflags below instead of
        # relying on auto-detection from the Compiler-rt CMake files.
        defines['CMAKE_C_COMPILER_TARGET'] = 'aarch64-linux-gnu'
        defines['COMPILER_RT_INCLUDE_TESTS'] = 'ON'
        defines['COMPILER_RT_ENABLE_WERROR'] = 'ON'
        defines['SANITIZER_CXX_ABI'] = 'libstdc++'
        return defines

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        # compiler-rt/lib/gwp_asan uses PRIu64 and similar format-specifier macros.
        # Add __STDC_FORMAT_MACROS so their definition gets included from
        # inttypes.h.  This explicit flag is only needed here.  64-bit host runtimes
        # are built in stage1/stage2 and get it from the LLVM CMake configuration.
        # These are defined unconditionaly in bionic and newer glibc
        # (https://sourceware.org/git/gitweb.cgi?p=glibc.git;h=1ef74943ce2f114c78b215af57c2ccc72ccdb0b7)
        cflags.append('-D__STDC_FORMAT_MACROS')
        cflags.append('--target=aarch64-linux-gnu')
        cflags.append('-march=armv8-a')
        return cflags

    def _build_config(self) -> None:
        # Also remove the "stamps" created for the libcxx included in libfuzzer so
        # CMake runs the configure again (after the cmake caches are deleted).
        stamp_path = self.output_dir / 'lib' / 'fuzzer' / 'libcxx_fuzzer_aarch64-stamps'
        if stamp_path.exists():
            shutil.rmtree(stamp_path)
        super()._build_config()

class LibUnwindBuilder(base_builders.LLVMRuntimeBuilder):
    name: str = 'libunwind'
    src_dir: Path = paths.LLVM_PATH / 'libunwind'

    # Build two copies of the builtins library:
    #  - A copy targeting the NDK with hidden symbols.
    #  - A copy targeting the platform with exported symbols.
    # Bionic's libc.so exports the unwinder, so it needs a copy with exported
    # symbols. Everything else uses the NDK copy.
    config_list: List[configs.Config] = (
        configs.android_configs(platform=True) +
        configs.android_configs(platform=False)
    )

    @property
    def is_exported(self) -> bool:
        return self._config.platform

    @property
    def output_dir(self) -> Path:
        old_path = super().output_dir
        suffix = '-exported' if self.is_exported else '-hermetic'
        return old_path.parent / (old_path.name + suffix)

    @property
    def cflags(self) -> List[str]:
        return super().cflags + ['-D_LIBUNWIND_USE_DLADDR=0']

    @property
    def ldflags(self) -> List[str]:
        # Override the default -unwindlib=libunwind. libunwind.a doesn't exist
        # when libunwind is built, and libunwind can't use
        # CMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY because
        # LIBUNWIND_HAS_PTHREAD_LIB must be set to false.
        return super().ldflags + ['-unwindlib=none']

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        defines['LIBUNWIND_HIDE_SYMBOLS'] = 'TRUE' if not self.is_exported else 'FALSE'
        defines['LIBUNWIND_ENABLE_SHARED'] = 'FALSE'
        if self.enable_assertions:
            defines['LIBUNWIND_ENABLE_ASSERTIONS'] = 'TRUE'
        else:
            defines['LIBUNWIND_ENABLE_ASSERTIONS'] = 'FALSE'
        # Enable the FrameHeaderCache for the libc.so unwinder only. It can't be
        # enabled generally for Android because it needs the
        # dlpi_adds/dlpi_subs fields, which were only added to Bionic in
        # Android R. See llvm.org/pr46743.
        defines['LIBUNWIND_USE_FRAME_HEADER_CACHE'] = 'TRUE' if self.is_exported else 'FALSE'
        defines['LIBUNWIND_TARGET_TRIPLE'] = self._config.llvm_triple
        return defines

    def install_config(self) -> None:
        # We need to install libunwind manually.
        src_path = self.output_dir / 'lib64' / 'libunwind.a'
        arch = self._config.target_arch
        out_res_dir = self.output_toolchain.resource_dir / arch.value
        out_res_dir.mkdir(parents=True, exist_ok=True)

        if self.is_exported:
            # This special copy exports its symbols and is only intended for use
            # in Bionic's libc.so.
            shutil.copy2(src_path, out_res_dir / 'libunwind-exported.a')
        else:
            shutil.copy2(src_path, out_res_dir / 'libunwind.a')

            # Also install to self.toolchain.resource_dir, if it's different, for
            # use when building runtimes.
            if self.toolchain.resource_dir != self.output_toolchain.resource_dir:
                res_dir = self.toolchain.resource_dir / arch.value
                res_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, res_dir / 'libunwind.a')

            # Make a copy for the NDK.
            ndk_dir = self.output_toolchain.path / 'runtimes_ndk_cxx' / arch.value
            ndk_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, ndk_dir / 'libunwind.a')


class LibOMPBuilder(base_builders.LLVMRuntimeBuilder):
    name: str = 'libomp'
    src_dir: Path = paths.LLVM_PATH / 'openmp'

    config_list: List[configs.Config] = (
        configs.android_configs(platform=True, extra_config={'is_shared': False}) +
        configs.android_configs(platform=False, extra_config={'is_shared': False}) +
        configs.android_configs(platform=False, extra_config={'is_shared': True})
    )

    @property
    def is_shared(self) -> bool:
        return cast(Dict[str, bool], self._config.extra_config)['is_shared']

    @property
    def output_dir(self) -> Path:
        old_path = super().output_dir
        suffix = '-shared' if self.is_shared else '-static'
        return old_path.parent / (old_path.name + suffix)

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        defines['OPENMP_ENABLE_LIBOMPTARGET'] = 'FALSE'
        defines['OPENMP_ENABLE_OMPT_TOOLS'] = 'FALSE'
        defines['LIBOMP_ENABLE_SHARED'] = 'TRUE' if self.is_shared else 'FALSE'
        return defines

    def install_config(self) -> None:
        # We need to install libomp manually.
        libname = 'libomp.' + ('so' if self.is_shared else 'a')
        src_lib = self.output_dir / 'runtime' / 'src' / libname
        dst_dir = self.install_dir
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_lib, dst_dir / libname)

        # install omp.h, omp-tools.h (it's enough to do for just one config).
        if self._config.target_arch == hosts.Arch.AARCH64:
            for header in ['omp.h', 'omp-tools.h']:
                shutil.copy2(self.output_dir / 'runtime' / 'src' / header,
                             self.output_toolchain.clang_builtin_header_dir)


class LibNcursesBuilder(base_builders.AutoconfBuilder, base_builders.LibInfo):
    name: str = 'libncurses'
    src_dir: Path = paths.LIBNCURSES_SRC_DIR
    config_list: List[configs.Config] = [configs.host_config()]
    lib_version: str = '6'

    @property
    def config_flags(self) -> List[str]:
        return super().config_flags + [
            '--with-shared',
        ]

    @property
    def _lib_names(self) -> List[str]:
        return ['libncurses', 'libform', 'libpanel']


class LibEditBuilder(base_builders.AutoconfBuilder, base_builders.LibInfo):
    name: str = 'libedit'
    src_dir: Path = paths.LIBEDIT_SRC_DIR
    config_list: List[configs.Config] = [configs.host_config()]
    libncurses: base_builders.LibInfo
    lib_version: str = '0'

    @property
    def ldflags(self) -> List[str]:
        return [
            f'-L{self.libncurses.link_libraries[0].parent}',
        ] + super().ldflags

    @property
    def cflags(self) -> List[str]:
        flags = []
        flags.append('-I' + str(self.libncurses.include_dir))
        flags.append('-I' + str(self.libncurses.include_dir / 'ncurses'))
        return flags + super().cflags


    def build(self) -> None:
        files: List[Path] = []
        super().build()


class SwigBuilder(base_builders.AutoconfBuilder):
    name: str = 'swig'
    src_dir: Path = paths.SWIG_SRC_DIR
    config_list: List[configs.Config] = [configs.host_config()]

    @property
    def config_flags(self) -> List[str]:
        flags = super().config_flags
        flags.append('--without-pcre')
        return flags

    @property
    def ldflags(self) -> List[str]:
        ldflags = super().ldflags
        # Point to the libc++.so from the toolchain.
        ldflags.append(f'-Wl,-rpath,{self.toolchain.lib_dir}')
        return ldflags


class XzBuilder(base_builders.CMakeBuilder, base_builders.LibInfo):
    name: str = 'liblzma'
    src_dir: Path = paths.XZ_SRC_DIR
    config_list: List[configs.Config] = [configs.host_config()]
    static_lib: bool = True

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        # CMake actually generates a malformed archive command. llvm-ranlib does
        # not accept it, but the Apple ranlib accepts this. Workaround to use
        # the system ranlib until either CMake fixes this or llvm-ranlib also
        # supports this common malformed input.
        # See LIBTOOL(1).
        if self._config.target_os.is_darwin:
            defines.pop("CMAKE_RANLIB")
        return defines

class LibXml2Builder(base_builders.CMakeBuilder, base_builders.LibInfo):
    name: str = 'libxml2'
    src_dir: Path = paths.LIBXML2_SRC_DIR
    config_list: List[configs.Config] = [configs.host_config()]
    lib_version: str = '2.9.12'

    @contextlib.contextmanager
    def _backup_file(self, file_to_backup: Path) -> Iterator[None]:
        backup_file = file_to_backup.parent / (file_to_backup.name + '.bak')
        if file_to_backup.exists():
            file_to_backup.rename(backup_file)
        try:
            yield
        finally:
            if backup_file.exists():
                backup_file.rename(file_to_backup)

    def build(self) -> None:
        # The src dir contains configure files for Android platform. Rename them
        # so that they will not be used during our build.
        # We don't delete them here because the same libxml2 may be used to build
        # Android platform later.
        with self._backup_file(self.src_dir / 'include' / 'libxml' / 'xmlversion.h'):
            with self._backup_file(self.src_dir / 'config.h'):
                super().build()

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        defines['LIBXML2_WITH_PYTHON'] = 'OFF'
        defines['LIBXML2_WITH_PROGRAMS'] = 'OFF'
        defines['LIBXML2_WITH_LZMA'] = 'OFF'
        defines['LIBXML2_WITH_ICONV'] = 'OFF'
        defines['LIBXML2_WITH_ZLIB'] = 'OFF'
        return defines

    @property
    def include_dir(self) -> Path:
        return self.install_dir / 'include' / 'libxml2'

    @property
    def symlinks(self) -> List[Path]:
        if self._config.target_os.is_windows:
            return []
        ext = 'so' if self._config.target_os.is_linux else 'dylib'
        return [self.install_dir / 'lib' / f'libxml2.{ext}']


class LldbServerBuilder(base_builders.LLVMRuntimeBuilder):
    name: str = 'lldb-server'
    src_dir: Path = paths.LLVM_PATH / 'llvm'
    config_list: List[configs.Config] = configs.android_configs(platform=False, static=True)
    ninja_targets: List[str] = ['lldb-server']

    @property
    def cflags(self) -> List[str]:
        cflags: List[str] = super().cflags
        # The build system will add '-stdlib=libc++' automatically. Since we
        # have -nostdinc++ here, -stdlib is useless. Adds a flag to avoid the
        # warnings.
        cflags.append('-Wno-unused-command-line-argument')
        return cflags

    @property
    def ldflags(self) -> List[str]:
        # Currently, -rtlib=compiler-rt (even with -unwindlib=libunwind) does
        # not automatically link libunwind.a on Android.
        return super().ldflags + ['-lunwind']

    @property
    def _llvm_target(self) -> str:
        return {
            hosts.Arch.ARM: 'ARM',
            hosts.Arch.AARCH64: 'AArch64',
            hosts.Arch.I386: 'X86',
            hosts.Arch.X86_64: 'X86',
        }[self._config.target_arch]

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        # lldb depends on support libraries.
        defines['LLVM_ENABLE_PROJECTS'] = 'clang;lldb'
        defines['LLVM_TARGETS_TO_BUILD'] = self._llvm_target
        defines['LLVM_TABLEGEN'] = str(self.toolchain.build_path / 'bin' / 'llvm-tblgen')
        defines['CLANG_TABLEGEN'] = str(self.toolchain.build_path / 'bin' / 'clang-tblgen')
        defines['LLDB_TABLEGEN'] = str(self.toolchain.build_path / 'bin' / 'lldb-tblgen')
        triple = self._config.llvm_triple
        defines['LLVM_HOST_TRIPLE'] = triple.replace('i686', 'i386')
        defines['LLDB_ENABLE_LUA'] = 'OFF'
        return defines

    def install_config(self) -> None:
        src_path = self.output_dir / 'bin' / 'lldb-server'
        install_dir = self.install_dir
        install_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, install_dir)


class LibCxxAbiBuilder(base_builders.LLVMRuntimeBuilder):
    name = 'libcxxabi'
    src_dir: Path = paths.LLVM_PATH / 'libcxxabi'

    @property
    def install_dir(self):
        return paths.OUT_DIR / 'windows-x86-64-install'

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines: Dict[str, str] = super().cmake_defines
        defines['LIBCXXABI_ENABLE_NEW_DELETE_DEFINITIONS'] = 'OFF'
        defines['LIBCXXABI_LIBCXX_INCLUDES'] = self.toolchain.libcxx_headers

        # Build only the static library.
        defines['LIBCXXABI_ENABLE_SHARED'] = 'OFF'

        if self.enable_assertions:
            defines['LIBCXXABI_ENABLE_ASSERTIONS'] = 'ON'
        defines['LIBCXXABI_TARGET_TRIPLE'] = self._config.llvm_triple

        return defines

    @property
    def cflags(self) -> List[str]:
        cflags: List[str] = super().cflags
        # Disable libcxx visibility annotations and enable WIN32 threads.  These
        # are needed because the libcxxabi build happens before libcxx and uses
        # headers from stage1/stage2.
        cflags.append('-D_LIBCPP_DISABLE_VISIBILITY_ANNOTATIONS')
        cflags.append('-D_LIBCPP_HAS_THREAD_API_WIN32')
        return cflags


class SysrootsBuilder(base_builders.Builder):
    name: str = 'sysroots'
    config_list: List[configs.Config] = (
        configs.android_configs(platform=True) +
        configs.android_configs(platform=False)
    )

    def _build_config(self) -> None:
        config: configs.AndroidConfig = cast(configs.AndroidConfig, self._config)
        arch = config.target_arch
        platform = config.platform
        sysroot = config.sysroot
        if sysroot.exists():
            shutil.rmtree(sysroot)
        sysroot.mkdir(parents=True, exist_ok=True)

        # Copy the NDK prebuilt's sysroot, but for the platform variant, omit
        # the STL and android_support headers and libraries.
        src_sysroot = paths.NDK_BASE / 'toolchains' / 'llvm' / 'prebuilt' / 'linux-x86_64' / 'sysroot'

        # Copy over usr/include.
        shutil.copytree(src_sysroot / 'usr' / 'include',
                        sysroot / 'usr' / 'include', symlinks=True)

        if platform:
            # Remove the STL headers.
            shutil.rmtree(sysroot / 'usr' / 'include' / 'c++')
        else:
            # Add the android_support headers from usr/local/include.
            shutil.copytree(src_sysroot / 'usr' / 'local' / 'include',
                            sysroot / 'usr' / 'local' / 'include', symlinks=True)

        # Copy over usr/lib/$TRIPLE.
        src_lib = src_sysroot / 'usr' / 'lib' / config.ndk_sysroot_triple
        dest_lib = sysroot / 'usr' / 'lib' / config.ndk_sysroot_triple
        shutil.copytree(src_lib, dest_lib, symlinks=True)

        # Remove the NDK's libcompiler_rt-extras.  For the platform, also remove
        # the NDK libc++.
        (dest_lib / 'libcompiler_rt-extras.a').unlink()
        if platform:
            (dest_lib / 'libc++abi.a').unlink()
            (dest_lib / 'libc++_static.a').unlink()
            (dest_lib / 'libc++_shared.so').unlink()
        # Each per-API-level directory has libc++.so, libc++.a, and libcompiler_rt-extras.a.
        for subdir in dest_lib.iterdir():
            if subdir.is_symlink() or not subdir.is_dir():
                continue
            if not re.match(r'\d+$', subdir.name):
                continue
            (subdir / 'libcompiler_rt-extras.a').unlink()
            if platform:
                (subdir / 'libc++.a').unlink()
                (subdir / 'libc++.so').unlink()
        # Verify that there aren't any extra copies somewhere else in the
        # directory hierarchy.
        verify_gone = ['libcompiler_rt-extras.a', 'libunwind.a']
        if platform:
            verify_gone += [
                'libc++abi.a',
                'libc++_static.a',
                'libc++_shared.so',
                'libc++.a',
                'libc++.so',
            ]
        for (parent, _, files) in os.walk(sysroot):
            for f in files:
                if f in verify_gone:
                    raise RuntimeError('sysroot file should have been ' +
                                       f'removed: {os.path.join(parent, f)}')

        if platform:
            # Create a stub library for the platform's libc++.
            platform_stubs = paths.OUT_DIR / 'platform_stubs' / config.ndk_arch
            platform_stubs.mkdir(parents=True, exist_ok=True)
            libdir = sysroot / 'usr' / ('lib' if arch == hosts.Arch.X86_64 else 'lib')
            libdir.mkdir(parents=True, exist_ok=True)
            with (platform_stubs / 'libc++.c').open('w') as f:
                f.write(textwrap.dedent("""\
                    void __cxa_atexit() {}
                    void __cxa_demangle() {}
                    void __cxa_finalize() {}
                    void __dynamic_cast() {}
                    void _ZTIN10__cxxabiv117__class_type_infoE() {}
                    void _ZTIN10__cxxabiv120__si_class_type_infoE() {}
                    void _ZTIN10__cxxabiv121__vmi_class_type_infoE() {}
                    void _ZTISt9type_info() {}
                """))

            utils.check_call([self.toolchain.cc,
                              f'--target={config.llvm_triple}',
                              '-fuse-ld=lld', '-nostdlib', '-shared',
                              '-Wl,-soname,libc++.so',
                              '-o{}'.format(libdir / 'libc++.so'),
                              str(platform_stubs / 'libc++.c')])


class PlatformLibcxxAbiBuilder(base_builders.LLVMRuntimeBuilder):
    name = 'platform-libcxxabi'
    src_dir: Path = paths.LLVM_PATH / 'libcxxabi'
    config_list: List[configs.Config] = configs.android_configs(
        platform=True, suppress_libcxx_headers=True)

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines: Dict[str, str] = super().cmake_defines
        defines['LIBCXXABI_LIBCXX_INCLUDES'] = self.toolchain.libcxx_headers
        defines['LIBCXXABI_ENABLE_SHARED'] = 'OFF'
        defines['LIBCXXABI_TARGET_TRIPLE'] = self._config.llvm_triple
        return defines

    def _is_64bit(self) -> bool:
        return self._config.target_arch in (hosts.Arch.AARCH64, hosts.Arch.X86_64)

    def _build_config(self) -> None:
        if self._is_64bit():
            # For arm64 and x86_64, build static cxxabi library from
            # toolchain/libcxxabi and use it when building runtimes.  This
            # should affect all compiler-rt runtimes that use libcxxabi
            # (e.g. asan, hwasan, scudo, tsan, ubsan, xray).
            super()._build_config()
        else:
            self.install_config()

    def install_config(self) -> None:
        arch = self._config.target_arch
        lib_name = 'lib' if arch == hosts.Arch.X86_64 else 'lib'
        install_dir = self._config.sysroot / 'usr' / lib_name

        if self._is_64bit():
            src_path = self.output_dir / 'lib64' / 'libc++abi.a'
            shutil.copy2(src_path, install_dir / 'libc++abi.a')
        else:
            with (install_dir / 'libc++abi.so').open('w') as f:
                f.write('INPUT(-lc++)')


class LibCxxBuilder(base_builders.LLVMRuntimeBuilder):
    name = 'libcxx'
    src_dir: Path = paths.LLVM_PATH / 'libcxx'
    libcxx_abi_path: Path

    @property
    def install_dir(self):
        return paths.OUT_DIR / 'windows-x86-64-install'

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines: Dict[str, str] = super().cmake_defines
        defines['LIBCXX_ENABLE_STATIC_ABI_LIBRARY'] = 'ON'
        defines['LIBCXX_ENABLE_NEW_DELETE_DEFINITIONS'] = 'ON'
        defines['LIBCXX_CXX_ABI'] = 'libcxxabi'
        defines['LIBCXX_HAS_WIN32_THREAD_API'] = 'ON'
        defines['LIBCXX_TEST_COMPILER_FLAGS'] = defines['CMAKE_CXX_FLAGS']
        defines['LIBCXX_TEST_LINKER_FLAGS'] = defines['CMAKE_EXE_LINKER_FLAGS']
        defines['LIBCXX_TARGET_TRIPLE'] = self._config.llvm_triple

        # Use cxxabi header from the source directory since it gets installed
        # into install_dir only during libcxx's install step.  But use the
        # library from install_dir.
        defines['LIBCXX_CXX_ABI_INCLUDE_PATHS'] = str(paths.LLVM_PATH / 'libcxxabi' / 'include')
        defines['LIBCXX_CXX_ABI_LIBRARY_PATH'] = str(self.libcxx_abi_path / 'lib')

        # Build only the static library.
        defines['LIBCXX_ENABLE_SHARED'] = 'OFF'
        defines['LIBCXX_ENABLE_EXPERIMENTAL_LIBRARY'] = 'OFF'

        if self.enable_assertions:
            defines['LIBCXX_ENABLE_ASSERTIONS'] = 'ON'

        return defines

    @property
    def cflags(self) -> List[str]:
        cflags: List[str] = super().cflags
        # Disable libcxxabi visibility annotations since we're only building it
        # statically.
        cflags.append('-D_LIBCXXABI_DISABLE_VISIBILITY_ANNOTATIONS')
        return cflags


class WindowsToolchainBuilder(base_builders.LLVMBuilder):
    name: str = 'windows-x86-64'
    toolchain_name: str = 'stage1'
    build_lldb: bool = True
    libcxx_path: Optional[Path] = None

    @property
    def _is_msvc(self) -> bool:
        return isinstance(self._config, configs.MSVCConfig)

    @property
    def install_dir(self) -> Path:
        return paths.OUT_DIR / 'windows-x86-64-install'

    @property
    def llvm_targets(self) -> Set[str]:
        return constants.ANDROID_TARGETS

    @property
    def llvm_projects(self) -> Set[str]:
        proj = {'clang', 'clang-tools-extra', 'lld', 'polly'}
        if self.build_lldb:
            proj.add('lldb')
        return proj

    @property
    def cmake_defines(self) -> Dict[str, str]:
        defines = super().cmake_defines
        # Don't build compiler-rt, libcxx etc. for Windows
        defines['LLVM_BUILD_RUNTIME'] = 'OFF'
        # Build clang-tidy/clang-format for Windows.
        defines['LLVM_TOOL_CLANG_TOOLS_EXTRA_BUILD'] = 'ON'
        defines['LLVM_TOOL_OPENMP_BUILD'] = 'OFF'
        # Don't build tests for Windows.
        defines['LLVM_INCLUDE_TESTS'] = 'OFF'

        defines['LLVM_CONFIG_PATH'] = str(self.toolchain.build_path / 'bin' / 'llvm-config')
        defines['LLVM_TABLEGEN'] = str(self.toolchain.build_path / 'bin' / 'llvm-tblgen')
        defines['CLANG_TABLEGEN'] = str(self.toolchain.build_path / 'bin' / 'clang-tblgen')
        if self.build_lldb:
            defines['LLDB_TABLEGEN'] = str(self.toolchain.build_path / 'bin' / 'lldb-tblgen')
            defines['LLDB_PYTHON_RELATIVE_PATH'] = f'lib/python{paths._PYTHON_VER}/site-packages'
        defines['LLVM_ENABLE_PLUGINS'] = 'ON'

        defines['CMAKE_CXX_STANDARD'] = '17'

        defines['ZLIB_INCLUDE_DIR'] = str(paths.WIN_ZLIB_INCLUDE_PATH)
        defines['ZLIB_LIBRARY_DEBUG'] = str(paths.WIN_ZLIB_LIB_PATH / 'libz.a')
        defines['ZLIB_LIBRARY_RELEASE'] = str(paths.WIN_ZLIB_LIB_PATH / 'libz.a')

        return defines

    @property
    def ldflags(self) -> List[str]:
        ldflags = super().ldflags
        if not self._is_msvc:
            # Use static-libgcc to avoid runtime dependence on libgcc_eh.
            ldflags.append('-static-libgcc')
            # pthread is needed by libgcc_eh.
            ldflags.append('-pthread')

            ldflags.append('-Wl,--dynamicbase')
            ldflags.append('-Wl,--nxcompat')
            ldflags.append('-Wl,--high-entropy-va')
            ldflags.append('-Wl,--Xlink=-Brepro')
            libpath_prefix = '-L'
        else:
            ldflags.append('/dynamicbase')
            ldflags.append('/nxcompat')
            ldflags.append('/highentropyva')
            ldflags.append('/Brepro')
            libpath_prefix = '/LIBPATH:'

        ldflags.append(libpath_prefix + str(paths.WIN_ZLIB_LIB_PATH))
        if self.libcxx_path:
            # Add path to libc++, libc++abi.
            libcxx_lib = self.libcxx_path / 'lib'
            ldflags.append(libpath_prefix + str(libcxx_lib))
        return ldflags

    @property
    def cflags(self) -> List[str]:
        cflags = super().cflags
        cflags.append('-DLZMA_API_STATIC')
        cflags.append('-DMS_WIN64')
        cflags.append(f'-I{paths.WIN_ZLIB_INCLUDE_PATH}')
        return cflags

    @property
    def cxxflags(self) -> List[str]:
        cxxflags = super().cxxflags

        # Use -fuse-cxa-atexit to allow static TLS destructors.  This is needed for
        # clang-tools-extra/clangd/Context.cpp
        cxxflags.append('-fuse-cxa-atexit')

        if self.libcxx_path:
            # Explicitly add the path to libc++ headers.  We don't need to configure
            # options like visibility annotations, win32 threads etc. because the
            # __generated_config header in the patch captures all the options used when
            # building libc++.
            cxx_headers = self.libcxx_path / 'include' / 'c++' / 'v1'
            cxxflags.append(f'-I{cxx_headers}')

        return cxxflags

    def install_config(self) -> None:
        super().install_config()
        lldb_wrapper_path = self.install_dir / 'bin' / 'lldb.cmd'
        lldb_wrapper_path.write_text(textwrap.dedent("""\
            @ECHO OFF
            SET PYTHONHOME=%~dp0..\python3
            SET PATH=%~dp0..\python3;%PATH%
            %~dp0lldb.exe %*
            EXIT /B %ERRORLEVEL%
        """))
