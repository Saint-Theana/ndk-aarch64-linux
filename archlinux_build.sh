#/l!/bin/bash

pacman -S  --noconfirm clang llvm lld libc++ --overwrite '*'
#link toolchains
if ! ls /usr/bin/clang;then
  cd /usr/bin
  for i in $(ls *-12)
  do
    ln -s $i $(echo $i|sed 's/-12//g')
  done
fi

# some tool we need

pacman -S --noconfirm expat bison python git rsync curl wget make tar go python3 cmake --overwrite '*'
curl https://storage.googleapis.com/git-repo-downloads/repo > /bin/repo
chmod a+x /bin/repo
git config --global user.email "Saint-Theana@github.com"
git config --global user.name "Saint-Theana"





cd
mkdir llvm-toolchain
cd llvm-toolchain
#this process might stuck at a configuration choice.
repo init -u https://android.googlesource.com/platform/manifest -b llvm-toolchain
#and there is something we dont need to download,so patch it
origin="$(cat .repo/manifests/default.xml)"
deleteline(){
    origin="$(echo "$origin"|grep -v "$1")"
}
deleteline "prebuilts/clang"
deleteline "prebuilts/python"
deleteline "prebuilts/go"
deleteline "prebuilts/cmake"
deleteline "prebuilts/gcc"
deleteline "toolchain/prebuilts/ndk/r23"
echo "$origin" >.repo/manifests/default.xml
#sync the repo
repo sync -cj4
#download ndk prebuilt files
mkdir -p toolchain/prebuilts/ndk/
cd toolchain/prebuilts/ndk/
wget -c https://github.com/Saint-Theana/ndk-aarch64-linux/releases/download/r23/android-ndk-r23-linux-aarch64-bionic-ubuntu.1.tar.gz
if ! ls r23;then
  tar xvf android-ndk-r23-linux-aarch64-bionic-ubuntu.1.tar.gz
  mv android-ndk-r23 r23
fi
#patch build files
cd
cd llvm-toolchain
cd toolchain
git clone https://github.com/Saint-Theana/llvm_android_aarch64_patch
cp -r llvm_android llvm_android_origin
cd llvm_android
for i in $(ls ../llvm_android_aarch64_patch/archlinux_build)
do
    patch -p1 <../llvm_android_aarch64_patch/archlinux_build/$i
done
cd
cd llvm-toolchain


#build shader-tools
cd 
mkdir shader-tools
cd shader-tools
git clone --depth=1 https://github.com/google/shaderc
cd shaderc/third_party
git clone --depth=1 https://github.com/KhronosGroup/SPIRV-Tools.git   spirv-tools
git clone --depth=1 https://github.com/KhronosGroup/SPIRV-Headers.git spirv-tools/external/spirv-headers
git clone --depth=1 https://github.com/google/googletest.git
git clone --depth=1 https://github.com/google/effcee.git
git clone --depth=1 https://github.com/google/re2.git
git clone --depth=1 https://github.com/KhronosGroup/glslang.git
# start building shaderc...
if ! cmake -DCMAKE_INSTALL_LOCAL_ONLY=1 -P cmake_install.cmake;then
sed -i '1i\include(CheckCXXCompilerFlag)' ./CMakeLists.txt
mkdir build && cd build
# setting android ndk toolchain
cmake -G "Unix Makefiles" \
    -DCMAKE_C_COMPILER=/usr/bin/clang \
    -DCMAKE_CXX_COMPILER=/usr/bin/clang++ \
    -DCMAKE_SYSROOT=/ \
    -DCMAKE_BUILD_TYPE=Release \
    -DEFFCEE_BUILD_TESTING=off \
    -DCMAKE_INSTALL_PREFIX=~/llvm-toolchain/toolchain/prebuilts/ndk/r23/shader-tools/linux-aarch64 \
    ..
make -j8
make install -j8
fi
cd
cd llvm-toolchain
python3 toolchain/llvm_android/build.py --no-build windows

cd
cd llvm-toolchain
rm -r toolchain/prebuilts/ndk/r23/toolchains/llvm/prebuilt/linux-aarch64/lib64
rm -r toolchain/prebuilts/ndk/r23/toolchains/llvm/prebuilt/linux-aarch64/lib
rm -r toolchain/prebuilts/ndk/r23/toolchains/llvm/prebuilt/linux-aarch64/share
rm -r toolchain/prebuilts/ndk/r23/toolchains/llvm/prebuilt/linux-aarch64/test
rm -r toolchain/prebuilts/ndk/r23/toolchains/llvm/prebuilt/linux-aarch64/prebuilt_include
rm -r toolchain/prebuilts/ndk/r23/toolchains/llvm/prebuilt/linux-aarch64/AndroidVersion.txt
for i in $(ls toolchain/prebuilts/ndk/r23/toolchains/llvm/prebuilt/linux-aarch64/bin)
do
  if ! echo $i|grep linux-android;then
      rm toolchain/prebuilts/ndk/r23/toolchains/llvm/prebuilt/linux-aarch64/bin/$i
  fi
done
cp -r out/install/linux-x86/clang-dev/bin/* /opt/android-ndk-r23/toolchains/llvm/prebuilt/linux-aarch64/bin
cp -r out/install/linux-x86/clang-dev/lib /opt/android-ndk-r23/toolchains/llvm/prebuilt/linux-aarch64/
cp -r out/install/linux-x86/clang-dev/lib64 /opt/android-ndk-r23/toolchains/llvm/prebuilt/linux-aarch64/
cp -r out/install/linux-x86/clang-dev/share /opt/android-ndk-r23/toolchains/llvm/prebuilt/linux-aarch64/
cp -r out/install/linux-x86/clang-dev/test /opt/android-ndk-r23/toolchains/llvm/prebuilt/linux-aarch64/
cp -r out/install/linux-x86/clang-dev/prebuilt_include /opt/android-ndk-r23/toolchains/llvm/prebuilt/linux-aarch64/
