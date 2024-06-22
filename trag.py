#!/usr/bin/env python3

import click

from pathlib import Path
from pprint import pprint
import contextlib
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import functools
import gzip
try:
    # significantly faster!
    import ujson as json
except ImportError:
    import json
import os.path
import re
import shutil
import sqlite3
import subprocess
import sys
import time


@click.group()
def app():
    pass


TESTCASES = [
    line.strip()
    for line in Path(__file__).parent.joinpath('test-cases.txt').open('r')
]


@app.command(
    short_help='Initialize a test data file',
    help='Initialize a test data file and gather data about test cases.')
@click.option('--test262', 'test262_path', required=True, type=Path, help='Path to a clone of https://github.com/tc39/test262')
@click.option('--force/--no-force', help='Re-initialize an existing file, deleting all data (default: fail if file exists already)')
@click.option('-f', '--file', 'data_file', type=Path, default='trag.data', help='Data file to write.')
def init(test262_path, data_file, force):
    if not force and data_file.exists():
        raise RuntimeError(f'data file already exists: {data_file} (use --force to overwrite)')

    db = sqlite3.connect(data_file, autocommit=False)

    db.executescript('''
    create table if not exists strings
      ( string_id integer primary key autoincrement
      , string varchar not null unique
      );
    create table if not exists groups
      ( path_sid unique references strings (string_id)
      , group_sid references strings (string_id)
      );
    create table if not exists runs
      ( testcase_sid not null references strings (string_id)
      , error_category varchar
      , error_message_sid references strings (string_id)
      , use_strict tinyint not null
      , version char(40) not null
      , time real
      );
    create table if not exists testcases
      ( testcase_sid not null references strings (string_id)
      , metadata varchar
      , unique (testcase_sid)
      );
    ''')

    import yaml

    for rel_path in TESTCASES:
        testcase_sid = insert_string(db, rel_path)

        full_text = (test262_path / rel_path).open().read()
        metadata_yaml = cut_metadata(full_text)
        metadata = yaml.safe_load(metadata_yaml) or {}
        metadata_json = json.dumps(metadata)

        db.execute(
            'insert or replace into testcases (testcase_sid, metadata) values (?, ?)',
            (testcase_sid, metadata_json),
        )

    db.commit()

@functools.cache
def insert_string(db, s):
    # there must be a better way...
    db.execute('insert or ignore into strings (string) values (?)', [s])
    res = db.execute('select string_id from strings where string = ?', [s])
    return res.fetchone()[0]


def cut_metadata(full_text):
    start_delim = '/*---'
    end_delim = '---*/'

    try:
        start_ofs = full_text.index(start_delim) + len(start_delim)
    except ValueError:
        # can't find start delimiter => just return the default
        return ''

    # once we get here, an exception is an exception
    end_ofs = full_text.index(end_delim, start_ofs)
    return full_text[start_ofs : end_ofs]
    


@app.command(help='Run test cases for a specific version')
@click.option('--mcjs', 'mcjs_path', type=Path, required=True, help='Path to mcjs repo')
@click.option('--test262', 'test262_path', type=Path, required=True, help='Path to test262 repo')
@click.option('-v', '--versions', default='HEAD', help='Test these commits. (Git\'s revision-range syntax is allowed)')
@click.option('-f', '--file', 'data_file', default='trag.data', help='Data file to manipulate.')
@click.option('--filter', 'testcase_filter', default='', help='Only run test cases whose path contains this substring')
@click.option('-n', '--dry-run', is_flag=True, help='Only print the selected test cases; don\'t run anything')
@click.option('-j', '--max-jobs', default=10, type=int, help='Limit the max number of concurrent tests running at any given time')
@click.option('--force/--no-force', help='Always perform the test, overwrite data if necessary')
def run(mcjs_path, test262_path, versions, data_file, testcase_filter, dry_run, max_jobs, force):
    commits = resolve_commits(repo=mcjs_path, rev_range=versions)
    for commit in commits:
        if not re.match(r'^[a-f0-9]+$', commit):
            raise RuntimeError('Invalid commit hash in commits file:', commit)

    db = sqlite3.connect(data_file, autocommit=False)

    if force:
        already_tested = set()
    else:
        already_tested = set(
            version
            for (version, ) in db.execute('select distinct version from runs')
        )

    for commit in commits:
        action = 'skip' if commit in already_tested else 'TEST'
        print('will', action, commit)

    commits_to_test = [c for c in commits if c not in already_tested]
    if not commits_to_test:
        print('nothing to do.')
        return

    print('loading testcases')
    cur = db.execute('''
        select s.string as relpath, metadata
        from testcases, strings s
        where testcase_sid = s.string_id
        and relpath like '%' || ? || '%'
    ''', (testcase_filter, ))
    testcases = {}
    for (relpath, metadata_json) in cur:
        testcases[relpath] = json.loads(metadata_json) or {}

    warnings = []
    def warn(message):
        warnings.append(message)
        print('WARNING:', message)

    with (
        restore_repo_status(mcjs_path),
        ThreadPoolExecutor(max_workers=max_jobs) as tpool,
    ):
        for vm_version in commits:
            print('---')
            if vm_version in already_tested:
                print('Skipping, already tested:', vm_version)
                continue

            print('Testing VM version:', vm_version)
            db.execute('delete from runs where version = ?', (vm_version, ))

            if not dry_run:
                try:
                    switch_to_version(
                        src_dir=mcjs_path,
                        vm_version=vm_version,
                    )
                except VersionSwitchError:
                    warn('# ERROR while switching to version {}'.format(vm_version))
                    continue

            futures = []
            for relpath, metadata in testcases.items():
                def submit_task(use_strict):
                    futures.append(tpool.submit(
                        run_test,
                        test262_path=test262_path,
                        vm_version=vm_version,
                        mcjs=mcjs_path,
                        rel_path=relpath,
                        use_strict=use_strict,
                        expected_negative='negative' in metadata,
                        dry_run=dry_run,
                    ))

                flags = metadata.get('flags', []) or []
                if 'onlyStrict' not in flags:
                    submit_task(use_strict=False)
                if 'noStrict' not in flags:
                    submit_task(use_strict=True)

            from concurrent.futures import as_completed
            for i, future in enumerate(as_completed(futures)):
                result = future.result()
                print(
                    '{:5}/{:5} {} {} {:60}'.format(
                        i + 1,
                        len(futures),
                        result['version'][:8],
                        'strict' if result['use_strict'] else 'sloppy',
                        os.path.basename(result['testcase']),
                    ),
                    end='\r',
                )
                store_result(db, result)


            print(f'Finished. {len(futures)} results')
            if not dry_run:
                db.commit()

    if warnings:
        print('Finished with warnings:')
        for w in warnings:
            print('-', w)



@contextmanager
def restore_repo_status(path):
    original_head = subprocess.check_output(
        ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
        cwd=path,
        encoding='ascii',
    ).strip()
    print('will restore repo to {} ({})'.format(original_head, path))

    try:
        yield
    finally:
        print('restoring repo to {} ({})'.format(original_head, path))
        subprocess.check_call(
            ['git', 'checkout', original_head],
            cwd=path,
        )


def resolve_commits(repo, rev_range):
    with contextlib.chdir(repo):
        if '..' in rev_range:
            cmd = ['git', 'log', '--first-parent', '--format=%H', rev_range]
        else:
            cmd = ['git', 'rev-parse', rev_range]
        return subprocess.check_output(cmd, encoding='ascii').splitlines()

def store_result(db, result):
    testcase_sid = insert_string(db, result['testcase'])
    if result.get('error'):
        err_msg_sid = insert_string(db, result['error']['message'])
        err_cat = result['error']['category']
    else:
        err_msg_sid = None
        err_cat = None

    db.execute('''
        insert into runs (testcase_sid, error_category, error_message_sid, use_strict, version, time)
        values (?, ?, ?, ?, ?, ?);
        ''',
        (testcase_sid, err_cat, err_msg_sid, result['use_strict'], result['version'], result.get('time'))
    )


def mk_cmd(files, use_strict):
    cmd = ['./target/debug/mcjs_test262']
    if use_strict:
        cmd.append('--force-last-strict')
    cmd += [str(p) for p in files]
    return cmd


class VersionSwitchError(RuntimeError):
    pass

def switch_to_version(src_dir, vm_version):
    with contextlib.chdir(src_dir):
        print('Checking out commit...')
        subprocess.run(
            ['git', 'checkout', vm_version],
            check=True,
        )
        print('Rebuilding...')
        try:
            subprocess.run(
                ['cargo', 'build', '-p', 'mcjs_test262'],
                check=True,
            )
        except subprocess.CalledProcessError as err:
            raise VersionSwitchError() from err


def run_test(test262_path, mcjs, vm_version, rel_path, use_strict, expected_negative=False, dry_run=False):
    files = [
        test262_path / 'harness/sta.js',
        test262_path / 'harness/assert.js',
        test262_path / rel_path,
    ]
    cmd = mk_cmd(files=files, use_strict=use_strict)

    output = {
        'testcase': rel_path,
        'version': vm_version,
        'use_strict': use_strict,
    }

    if dry_run:
        return output
    
    try:
        start_time = time.time()
        completed_process = subprocess.run(
            cmd,
            cwd=mcjs,
            capture_output=True,
            timeout=10.0,
        )

        # use the last line of stdout (a handful of versions emit some garbage on stdout)
        stdout_lines = completed_process.stdout.splitlines()
        outcome = json.loads(stdout_lines[-1])
        output.update(outcome)

        end_time = time.time()
        output['time'] = end_time - start_time

        if expected_negative:
            # TODO handle the different categories of expected errors
            if output['error'] is None:
                output['error'] = {
                    'category': 'unexpected success',
                    'message': 'error expected, but test run fine',
                }
            else:
                output['error'] = None

    except subprocess.TimeoutExpired:
        output['error'] = {
            'category': 'timeout',
            'message': 'runner timed out',
        }

    except subprocess.CalledProcessError as err:
        end_time = time.time()

        # runner failure
        try:
            error_message = err.output.decode('utf8')
        except UnicodeDecodeError:
            error_message = '<# encoding error #>'

        output['error'] = {
            'category': 'runner failure',
            'message': error_message,
            'time': end_time - start_time,
        }

    return output


def assert_exists(path):
    if not Path(path).is_file():
        raise RuntimeError(f'error: {data_file}: not a file')
   

@app.command(help='Overview of test results')
@click.option('--file', 'data_file', type=Path, default='trag.data', help='Data file to read')
@click.option('--version', help='mcjs version for which to summarize test results')
@click.option('--mcjs', 'mcjs_root', help='gather version from this directory where the mcjs repository is located')
def status(data_file, version, mcjs_root):
    from tabulate import tabulate

    assert_exists(data_file)

    if version is None:
        if mcjs_root is None:
            print('pass either --version or --mcjs.')
            sys.exit(1)
        version = resolve_commits(mcjs_root, 'HEAD^..')[0]

    db = sqlite3.connect(data_file)
    res = db.execute('''
        with q as (
            select g.group_sid, iif(error_message_sid is null, 1, 0) as success
            from runs r, groups g
            where r.version = ?
            and r.testcase_sid = g.path_sid
        )
        , q2 as (
            select sg.string as grp
            , sum(q.success) as ok
            , count(*) as total
            from q, strings sg
            where sg.string_id = q.group_sid
            group by q.group_sid
            order by grp
        )
        select q2.grp, cast(ok as real) / total * 100 as perc
        from q2
    ''', (version, ))

    rows = res.fetchall()
    print(tabulate(rows, headers=['Group', '% Passing'], floatfmt='.1f'))


@app.command(help='List detailed test case results')
@click.option('--file', 'data_file', type=Path, default='trag.data', help='Data file to read from')
@click.option('--version', help='mcjs version for which to summarize test results')
@click.option('--mcjs', 'mcjs_root', help='gather version from this directory where the mcjs repository is located')
@click.option('--outcome', help='Only show test cases with the given outcome (passed, failed)')
@click.option('--filter', default='', help='Only show test cases whose path contains this string')
@click.option('--errors/--no-errors', 'show_errors', help='Show error messages')
def list(data_file, version, mcjs_root, outcome, filter, show_errors):
    from tabulate import tabulate

    if not data_file.is_file():
        print(f'error: {data_file}: not a file')
        sys.exit(1)

    if version is None:
        if mcjs_root is None:
            print('pass either --version or --mcjs.')
            sys.exit(1)

        version = resolve_commits(mcjs_root, 'HEAD^..')[0]

    db = sqlite3.connect(data_file)
    query = '''
        select (error_message_sid is null) as success
        , use_strict
        , st.string as testcase
        , se.string as error_msg
        from runs left join strings se on (se.string_id = error_message_sid)
        , strings st
        where st.string_id = testcase_sid
        and version = ?
        and testcase like '%' || ? || '%'
    '''
    args = [version, filter]
    if outcome in ('passed', 'failed'):
        query += 'and success = ?'
        args += [1 if outcome == 'passed' else 0]
    elif outcome is not None:
        print('invalid value for --outcome:', outcome)
        sys.exit(1)

    res = db.execute(query, args)

    success_s = {
        0: 'failed',
        1: 'passed',
    }
    use_strict_s = {
        0: 'sloppy',
        1: 'strict',
    }

    for (success, use_strict, testcase, error_msg) in res:
        print('{:10} {:10} {}'.format(success_s[success], use_strict_s[use_strict], testcase))
        if show_errors:
            for line in error_msg.splitlines():
                print('    | ' + line)


@app.command(help='Compare test results between versions')
@click.option('--file', 'data_file', type=Path, default='trag.data', help='Data file to read from')
@click.option('--mcjs', 'mcjs_path', type=Path, help='Path to mcjs repo. Allows using git commit syntax for versions')
@click.argument('version_a')
@click.argument('version_b')
def diff(data_file, version_a, version_b, mcjs_path):
    if not os.path.exists(data_file):
        print(f'error: {data_file}: No such file or directory')
        sys.exit(1)

    if mcjs_path:
        version_a = resolve_commits(repo=mcjs_path, rev_range=version_a)[0]
        version_b = resolve_commits(repo=mcjs_path, rev_range=version_b)[0]
    else:
        def check_commit_id(s):
            import string
            if len(s) != 40 or not all(c in string.hexdigits for c in s):
                print(f'invalid commit ID: {s} (git commit syntax only available with --mcjs. See --help)')
                sys.exit(1)

        check_commit_id(version_a)
        check_commit_id(version_b)

    db = sqlite3.connect(data_file)

    res = db.execute('''
        select st.string as testcase, se.string as error_message
        from runs a, runs b, strings st, strings se
        where a.testcase_sid = b.testcase_sid
        and a.use_strict = b.use_strict
        and a.version = ?
        and b.version = ?
        and a.error_message_sid is null
        and b.error_message_sid is not null
        and st.string_id = a.testcase_sid
        and se.string_id = b.error_message_sid
        order by testcase
    ''', (version_a, version_b))
    new_failures = res.fetchall()

    print('Failures introduced:', len(new_failures))
    for (testcase, error_message) in new_failures:
         print(' * ' + testcase)

         for line in error_message.splitlines():
             print('    | ' + line)


    res = db.execute('''
        select st.string as testcase
        from runs a, runs b, strings st
        where a.testcase_sid = b.testcase_sid
        and a.use_strict = b.use_strict
        and a.version = ?
        and b.version = ?
        and a.error_message_sid is not null
        and b.error_message_sid is null
        and st.string_id = a.testcase_sid
        order by testcase
    ''', (version_a, version_b))
    new_successes = res.fetchall()

    print()
    print('Failures fixed:', len(new_successes))
    for (testcase, ) in new_successes:
         print(' * ' + testcase)



if __name__ == '__main__':
    app()
