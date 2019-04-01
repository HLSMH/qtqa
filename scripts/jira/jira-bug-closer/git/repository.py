#!/usr/bin/env python3
#############################################################################
##
## Copyright (C) 2019 The Qt Company Ltd.
## Contact: https://www.qt.io/licensing/
##
## This file is part of the Quality Assurance module of the Qt Toolkit.
##
## $QT_BEGIN_LICENSE:GPL-EXCEPT$
## Commercial License Usage
## Licensees holding valid commercial Qt licenses may use this file in
## accordance with the commercial license agreement provided with the
## Software or, alternatively, in accordance with the terms contained in
## a written agreement between you and The Qt Company. For licensing terms
## and conditions see https://www.qt.io/terms-conditions. For further
## information use the contact form at https://www.qt.io/contact-us.
##
## GNU General Public License Usage
## Alternatively, this file may be used under the terms of the GNU
## General Public License version 3 as published by the Free Software
## Foundation with exceptions as appearing in the file LICENSE.GPL3-EXCEPT
## included in the packaging of this file. Please review the following
## information to ensure the GNU General Public License requirements will
## be met: https://www.gnu.org/licenses/gpl-3.0.html.
##
## $QT_END_LICENSE$
##
#############################################################################

import asyncio
from distutils.version import StrictVersion
import fcntl
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple

from logger import logger
log = logger('repository')


repo_base = 'ssh://codereview.qt-project.org:29418/'
file_path = os.path.dirname(os.path.abspath(__file__))
working_dir = os.path.abspath(os.path.join(file_path, '..', 'git_repos'))
Path(working_dir).mkdir(parents=True, exist_ok=True)


class Version(StrictVersion):
    def __init__(self, version_string: str) -> None:
        super().__init__(version_string)
        self.original_version_string = version_string

    def __lt__(self, other: Any) -> Any:
        """ Compare versions taking the original_version_string into account.

            There are some cases where the default comparison is not good enough: we want 5.12.0 > 5.12,
            otherwise changes going into 5.12 while 5.12.0 exists will end up in 5.12.0 instead of 5.12.1. """
        if super().__eq__(other):
            return self.original_version_string < other.original_version_string
        return super().__lt__(other)

    def __eq__(self, other: Any) -> Any:
        return self.original_version_string == other.original_version_string

    def __gt__(self, other: Any) -> Any:
        if super().__eq__(other):
            return self.original_version_string > other.original_version_string
        return super().__gt__(other)

    def __repr__(self) -> str:
        return self.original_version_string + " - " + super().__repr__()


class Change:
    def __init__(self, repository: str, branch: str, before: Optional[str], after: str, since: Optional[str] = None) -> None:
        self.repository = repository
        self.branch = branch
        self.before = before
        self.after = after
        self.since = since

    def __repr__(self) -> str:
        return "<Change(repository='%s', branch='%s', before='%s', after='%s', since='%s')>" % (self.repository, self.branch, self.before, self.after, self.since)


class FixedByTag:
    def __init__(self, repository: str, branch: str, sha1: str, author: str, subject: str, version: Optional[str], task_numbers: List[str], fixes: List[str]) -> None:
        self.repository = repository
        self.branch = branch
        self.sha1 = sha1
        self.author = author
        self.subject = subject
        self.version = version  # Can be None in case we failed to guess it. E.g. wip/foobar does not result in anything.
        self.task_numbers = task_numbers
        self.fixes = fixes

    def __eq__(self, other: object) -> bool:
        return self.__dict__ == other.__dict__

    def __repr__(self) -> str:
        return "<FixedByTag(repository='%s', branch='%s', version='%s', sha1='%s', author='%s', fixes=%s, task_numbers=%s, subject='%s')>" % (self.repository, self.branch, self.version, self.sha1, self.author, self.fixes, self.task_numbers, self.subject)

    def __hash__(self) -> int:
        return hash(self.__dict__.values())


class Repository:
    def __init__(self, name: str) -> None:
        self.name = name
        self._issue_key_regexp = re.compile(r'^[A-Z]+-\d+$')
        # self._fd: int = -1  # lock file descriptor
        Path(os.path.dirname(self.repo_path)).mkdir(parents=True, exist_ok=True)

    async def __aenter__(self) -> "Repository":
        lock_path = self.repo_path + '_lock'
        self._fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: fcntl.flock(self._fd, fcntl.LOCK_EX))  # type: ignore
        return self

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)

    @property
    def repo_path(self) -> str:
        return os.path.join(working_dir, self.name)

    def git_command(self, command: str) -> str:
        return "git --git-dir=%s %s" % (self.repo_path, command)

    async def _check_repo(self) -> None:
        if os.path.exists(self.repo_path):
            return
        Path(os.path.dirname(self.repo_path)).mkdir(parents=True, exist_ok=True)
        log.info("Cloning '%s", self.name)
        command = "git clone --bare %s %s" % (repo_base + self.name, self.repo_path)
        process = await asyncio.create_subprocess_exec(*command.split())
        # wait for the process to finish
        await asyncio.wait_for(process.communicate(), 360)

    async def _git_fetch_heads(self) -> None:
        log.info("git fetch '%s'", self.name)
        command = self.git_command("fetch origin +refs/heads/*:refs/heads/* --prune")
        process = await asyncio.create_subprocess_exec(*command.split(), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        # wait for the process to finish
        await asyncio.wait_for(process.communicate(), 180)

    async def _git_show_ref(self, tags: bool = False) -> str:
        refType = '--tags' if tags else '--heads'
        command = self.git_command("show-ref %s" % refType)
        process = await asyncio.create_subprocess_exec(*command.split(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        # wait for the process to finish
        stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
        if stderr:
            log.warning("Error when running git show-ref: '%s'", stderr.decode('utf-8'))
        return stdout.decode('utf-8').strip()

    def _show_ref_output_to_dict(self, output: str) -> Dict[str, str]:
        d = {}
        for line in output.splitlines():
            sha1_ref = line.split()
            d[sha1_ref[1]] = sha1_ref[0]
        return d

    async def new_changes(self, since: Optional[str] = None) -> List[Change]:
        # git show-ref --heads
        # git fetch origin +refs/heads/*:refs/heads/* --prune
        # git show-ref --heads

        await self._check_repo()
        before = self._show_ref_output_to_dict(await self._git_show_ref())
        log.debug('before %s', before)
        await self._git_fetch_heads()
        after = self._show_ref_output_to_dict(await self._git_show_ref())
        log.debug('after %s', after)

        changes: List[Change] = []
        for branch, sha1 in after.items():
            if since:
                # We ignore recent changes and only take since into account
                changes.append(Change(repository=self.name, branch=branch, before=None, after=sha1, since=since))
            elif before.get(branch) != sha1:
                changes.append(Change(repository=self.name, branch=branch, before=before.get(branch), after=sha1, since=None))
        return changes

    def get_task_number_and_fixes(self, body: str) -> Tuple[List[str], List[str]]:
        task_numbers = []
        fixes = []
        for line in body.splitlines():
            if line.startswith('Task-number:'):
                issue_key = line[12:].strip()
                if self._issue_key_regexp.fullmatch(issue_key):
                    task_numbers.append(issue_key)
            if line.startswith('Fixes:'):
                issue_key = line[6:].strip()
                if self._issue_key_regexp.fullmatch(issue_key):
                    fixes.append(issue_key)
        return task_numbers, fixes

    @staticmethod
    def _clean_branch_name(ref: str) -> str:
        refs_heads = 'refs/heads/'
        if ref.startswith(refs_heads):
            return ref[len(refs_heads):]
        return ref

    @staticmethod
    def _clean_tag_name(ref: str) -> str:
        refs_tags = 'refs/tags/'
        if ref.startswith(refs_tags):
            ref = ref[len(refs_tags):]
        if ref.startswith('v'):
            return ref[1:]
        return ref

    @staticmethod
    def _find_first_comparable_minor_version(ref: Version, sorted_versions: List[Version]) -> Optional[Version]:
        for v in sorted_versions:
            if v.version[0] == ref.version[0] and v.version[1] == ref.version[1]:
                return v
        return None

    @staticmethod
    async def _guess_version(ref: str, branches: List[str], tags: List[str]) -> Optional[str]:
        ref = Repository._clean_branch_name(ref)
        if ref.count('.') == 2:
            return ref

        branch_list: List[Version] = []
        for b in branches:
            try:
                branch_list.append(Version(Repository._clean_branch_name(b)))
            except ValueError:
                # skip versions that are not x.y.z
                pass
        branch_list = sorted(branch_list, reverse=True)

        tag_list: List[Version] = []
        for t in tags:
            try:
                tag_list.append(Version(Repository._clean_tag_name(t)))
            except ValueError:
                # skip versions that are not x.y.z
                pass
        tag_list = sorted(tag_list, reverse=True)

        if ref in ['dev', 'master'] and len(branch_list) > 0:
            # take the last version found and increase minor by one
            previous = branch_list[0].version
            return '%s.%s.0' % (previous[0], str(int(previous[1] + 1)))

        # x.y - find the hightest tag or branch of the same version
        if ref.count('.') == 1:
            try:
                ref_version = Version(ref)
                log.warning("found highest version: %s", sorted(branch_list + tag_list, reverse=True))
                highest = Repository._find_first_comparable_minor_version(ref_version, sorted(branch_list + tag_list, reverse=True))
                log.warning("found highest version: %s", highest)
                if highest and highest.original_version_string.count('.') > 1:
                    # assume that 5.12 will be 5.12.7 if we find 5.12.6 in tags or branches
                    # the only exception is that if we got '5.12' as original version, we must assume 5.12.0, so end up in else
                    return '%s.%s.%s' % (highest.version[0], highest.version[1], str(int(highest.version[2]) + 1))
                else:
                    return '%s.%s.0' % (ref_version.version[0], ref_version.version[1])
            except ValueError:
                log.debug("Invalid version number: '%s'", ref)
                return None
        log.error("Could not determine version for ref: '%s' (branches: %s, tags: %s)", ref, branches, tags)
        return None

    async def parse_commit_messages(self, change: Change) -> List[FixedByTag]:
        format_options = {
            "id": "%H",
            "author_name": "%an",
            "author_email": "%ae",
            "date": "%ad",
            "subject": "%s",
            "body": "%b"}

        git_log_fields = "%x1f".join((format_options['id'], format_options['author_name'], format_options['subject'], format_options['body']))
        # use '1e' as start and '1f' as field separator
        format_string = "%x1e" + git_log_fields + "%x1f"

        # if a new branch is created, before will be None
        commit_range = "%s..%s" % (change.before, change.after) if change.before else change.after
        since = ''
        if change.since:
            since = '--since %s' % change.since
        command = self.git_command("log %s --format=%s %s" % (commit_range, format_string, since))
        process = await asyncio.create_subprocess_exec(*command.split(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 60)

        if stderr:
            log.warning("Error when running git command '%s': '%s'", command, stderr.decode('utf-8'))
        commits = stdout.decode('utf-8', errors='replace').strip('\x1e').split('\x1e')

        result: List[FixedByTag] = []
        if commits == ['']:  # ### FIXME: see test_gitlog, qt/qtlocation-mapboxgl comes up empty here
            return result

        for commit in commits:
            # -2 to remove \x1f\n
            sha1, author, subject, body = commit[:-2].split('\x1f')
            task_numbers, fixes = self.get_task_number_and_fixes(body)
            if task_numbers or fixes:
                version = await self._guess_version(
                    change.branch,
                    branches=list(self._show_ref_output_to_dict(await self._git_show_ref(tags=False)).keys()),
                    tags=list(self._show_ref_output_to_dict(await self._git_show_ref(tags=True)).keys()))
                result.append(FixedByTag(repository=self.name, branch=self._clean_branch_name(change.branch),
                                         version=version,
                                         sha1=sha1, author=author, subject=subject,
                                         task_numbers=task_numbers, fixes=fixes))
        return result
