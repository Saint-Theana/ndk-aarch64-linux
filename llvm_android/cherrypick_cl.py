#!/usr/bin/env python3
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

from __future__ import annotations
import argparse
import collections
import dataclasses
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Optional

from android_version import get_svn_revision_number, set_llvm_next
from merge_from_upstream import fetch_upstream, sha_to_revision
import paths
import source_manager
from utils import check_call, check_output


def parse_args():
    parser = argparse.ArgumentParser(description="Cherry pick upstream LLVM patches.",
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--sha', nargs='+', help='sha of patches to cherry pick')
    parser.add_argument(
        '--start-version', default='llvm',
        help="""svn revision to start applying patches. 'llvm' and 'llvm-next' can also be used.""")
    parser.add_argument('--verify-merge', action='store_true',
                        help='check if patches can be applied cleanly')
    parser.add_argument('--create-cl', action='store_true', help='create a CL')
    parser.add_argument('--bug', help='bug to reference in CLs created (if any)')
    parser.add_argument('--reason', help='issue/reason to mention in CL subject line', required=True)
    args = parser.parse_args()
    return args


def parse_start_version(start_version: str) -> int:
    if start_version in ['llvm', 'llvm-next']:
        set_llvm_next(start_version == 'llvm-next')
        return int(get_svn_revision_number())
    m = re.match(r'r?(\d+)', start_version)
    assert m, f'invalid start_version: {start_version}'
    return int(m.group(1))


@dataclass
class PatchItem:
    comment: str
    rel_patch_path: str
    bugs_tests: Optional[List(str)]
    start_version: int
    end_version: Optional[int]

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> PatchItem:
        return PatchItem(
            comment=d['comment'],
            rel_patch_path=d['rel_patch_path'],
            bugs_tests=d['bugs_tests'],
            start_version=d['start_version'],
            end_version=d['end_version'])

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self, dict_factory=collections.OrderedDict)

    @property
    def is_local_patch(self) -> bool:
        return not self.rel_patch_path.startswith('cherry/')

    @property
    def sha(self) -> str:
        m = re.match(r'cherry/(.+)\.patch', self.rel_patch_path)
        assert m, self.rel_patch_path
        return m.group(1)

    def __lt__(self, other: PatchItem) -> bool:
        """ Used to sort patches in PatchList:
            1. Sort upstream patches by their end_version in increasing order.
            2. Keep local patches at the end of the list, and don't change the relative order
               between two local patches.
        """
        if self.is_local_patch or other.is_local_patch:
            if not self.is_local_patch:
                return True
            return False
        return self.end_version < other.end_version


class PatchList(list):
    """ a list of PatchItem """

    JSON_FILE_PATH = paths.SCRIPTS_DIR / 'patches' / 'PATCHES.json'

    @classmethod
    def load_from_file(cls) -> PatchList:
        with open(cls.JSON_FILE_PATH, 'r') as fh:
            array = json.load(fh)
        return PatchList(PatchItem.from_dict(d) for d in array)

    def save_to_file(self):
        array = [patch.to_dict() for patch in self]
        with open(self.JSON_FILE_PATH, 'w') as fh:
            json.dump(array, fh, indent=4)


def generate_patch_files(sha_list: List[str], start_version: int) -> PatchList:
    """ generate upstream cherry-pick patch files """
    upstream_dir = paths.TOOLCHAIN_LLVM_PATH
    fetch_upstream()
    result = PatchList()
    for sha in sha_list:
        if len(sha) < 40:
            sha = get_full_sha(upstream_dir, sha)
        file_path = paths.SCRIPTS_DIR / 'patches' / 'cherry' / f'{sha}.patch'
        assert not file_path.exists(), f'{file_path} already exists'
        with open(file_path, 'w') as fh:
            check_call(f'git format-patch -1 {sha} --stdout',
                       stdout=fh, shell=True, cwd=upstream_dir)

        commit_subject = check_output(
            f'git log -n1 --format=%s {sha}', shell=True, cwd=upstream_dir)
        comment = '[UPSTREAM] ' + commit_subject.strip()
        rel_patch_path = f'cherry/{sha}.patch'
        end_version = sha_to_revision(sha)
        bugs_tests = None
        result.append(PatchItem(comment, rel_patch_path, bugs_tests, start_version, end_version))
    return result


def get_full_sha(upstream_dir: Path, short_sha: str) -> str:
    return check_output(['git', 'rev-parse', short_sha], cwd=upstream_dir).strip()


def create_cl(new_patches: PatchList, bug: Optional[str], reason: Optional[str]):
    file_list = [p.rel_patch_path for p in new_patches] + ['PATCHES.json']
    file_list = [str(paths.SCRIPTS_DIR / 'patches' / f) for f in file_list]
    check_call(['git', 'add'] + file_list)

    if reason:
        subject = f'[patches] Cherry pick CLS for: {reason}'
    else:
        subject = '[patches] Cherry pick CLs from upstream'
    commit_lines = [subject, '']
    if bug:
        if bug.isnumeric():
            commit_lines += [f'Bug: http://b/{bug}', '']
        else:
            commit_lines += [f'Bug: {bug}', '']
    for patch in new_patches:
        sha = patch.sha[:11]
        subject = patch.comment
        if subject.startswith('[UPSTREAM] '):
            subject = subject[len('[UPSTREAM] '):]
        commit_lines.append(sha + ' ' + subject)
    commit_lines += ['', 'Test: N/A']
    check_call(['git', 'commit', '-m', '\n'.join(commit_lines)])


def main():
    args = parse_args()
    patch_list = PatchList.load_from_file()
    if args.sha:
        start_version = parse_start_version(args.start_version)
        new_patches = generate_patch_files(args.sha, start_version)
        patch_list.extend(new_patches)
    patch_list.sort()
    patch_list.save_to_file()
    if args.verify_merge:
        print('verify merge...')
        source_manager.setup_sources(source_dir=paths.LLVM_PATH)
    if args.create_cl:
        create_cl(new_patches, args.bug, args.reason)


if __name__ == '__main__':
    main()
