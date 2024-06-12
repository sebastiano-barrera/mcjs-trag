#!/usr/bin/env python3

import click

from pathlib import Path
from pprint import pprint
import asyncio
import contextlib
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

@click.group()
def app():
    pass

# TODO
#  [x] init instead of scan
#  [ ] threads instead of async
#  [ ] no data files outside of the SQLite database
#  [ ] test cases list in a resource file, sibling to the script


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
      );
    create table testcases
      ( testcase_sid not null references strings (string_id)
      , metadata varchar
      );
    ''')

    for rel_path in TESTCASES:
        testcase_sid = insert_string(db, rel_path)

        full_text = (test262_path / rel_path).open().read()
        metadata_raw = cut_metadata(full_text)

        db.execute(
            'insert into testcases (testcase_sid, metadata) values (?, ?)',
            (testcase_sid, metadata_raw),
        )

        raise RuntimeError()
        
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
    


@app.command(help='Run a set of test cases')
@click.option('--mcjs', type=Path, required=True, help='Path to mcjs repo')
@click.option('-o', '--out', required=True, help='Results directory')
@click.option('-f', '--filter', 'testcase_filter', help='Only run test cases whose path contains this substring')
@click.option('-n', '--dry-run', is_flag=True, help='Only print the selected test cases; don\'t run anything')
@click.option('--force/--no-force', help='Overwrite results file if it exists (default: skip)')
@click.option('-j', '--max-jobs', default=10, type=int, help='Limit the max number of concurrent tests running at any given time')
@click.option('--commits', 'commits_filename', help='Checkout and test the commits listed in the given file.')
@click.argument('testrun_filename', metavar='testrun.json')
def run(testrun_filename, mcjs, out, force, max_jobs, commits_filename, testcase_filter, dry_run):
    with open(testrun_filename) as testrun_file:
        testrun = json.load(testrun_file)
        test262_path = Path(testrun['test262_path'])
        testcases = testrun['testcases']

    if commits_filename:
        commits = [
            line.strip()
            for line in open(commits_filename)
        ]

        if '%v' not in out:
            raise RuntimeError('The output file (passed with --out) must include "%v" so that a new file per version is created.')

    else:
        vm_version = get_version_of_repo(mcjs)
        commits = [vm_version]

    for commit in commits:
        if not re.match(r'^[a-f0-9]+$', commit):
            raise RuntimeError('Invalid commit hash in commits file:', commit)

    original_out = out

    for vm_version in commits:
        print('---')
        print('Testing VM version:', vm_version)

        out = original_out
        out = out.replace('%v', vm_version)
        if out.endswith('/'):
            out += 'out'
        if not out.endswith('.jsonl'):
            out = out + '.jsonl'
        out = Path(out)
        out_compressed = Path(str(out) + '.gz')

        print('out file:', out)

        if out_compressed.exists() and not force:
            print('file already exists, skipping task')
            continue

        if commits_filename and not dry_run:
            try:
                switch_to_version(
                    src_dir=mcjs,
                    vm_version=vm_version,
                )
            except VersionSwitchError:
                with contextlib.redirect_stdout(out.open('w')):
                    print('# ' + json.dumps({
                        'error': 'vm build error',
                        'version': vm_version,
                    }))
                continue

        out.parent.mkdir(exist_ok=True)

        testcase_semaphore = asyncio.Semaphore(max_jobs)
        async def call_limited(func, *args, **kwargs):
            async with testcase_semaphore:
                return await func(*args, **kwargs)

        runner = Runner(
            test262_path=test262_path,
            vm_version=vm_version,
            mcjs=mcjs,
        )
        tasks = []
        for rel_path, testcase in testcases.items():
            metadata = testcase['metadata'] or {}

            if testcase_filter is not None and testcase_filter not in rel_path:
                continue

            def submit_task(use_strict):
                if dry_run:
                    use_strict = 'strict' if use_strict else 'sloppy'
                    print(f'would run: {rel_path} ({use_strict})')
                else:
                    tasks.append(call_limited(
                        runner.run_test,
                        rel_path=rel_path,
                        use_strict=use_strict,
                        expected_negative='negative' in metadata,
                    ))

            flags = metadata.get('flags', []) or []
            if 'onlyStrict' not in flags:
                submit_task(use_strict=False)
            if 'noStrict' not in flags:
                submit_task(use_strict=True)

        async def collect_results():
            if dry_run:
                return

            with out.open('w') as out_file:
                for get_result in asyncio.as_completed(tasks):
                    result = await get_result
                    json_line = json.dumps(result)
                    # MUST be all on a single line
                    assert '\n' not in json_line
                    print(json_line, file=out_file)

            with out.open('rb') as out_file:
                with gzip.open(str(out_compressed), 'wb') as compressed_file:
                    shutil.copyfileobj(out_file, compressed_file)

            out.unlink()

        if not dry_run:
            asyncio.run(collect_results())
        print(f'Finished. {len(tasks)} results written to {out}.gz')


def get_version_of_repo(root):
    with contextlib.chdir(root):
        return subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            check=True,
            capture_output=True,
            encoding='utf8'
        ).stdout.strip()


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


class Runner:
    def __init__(self, **kwargs):
        for name in 'test262_path mcjs vm_version'.split():
            setattr(self, name, kwargs[name])

    async def run_test(self, rel_path, use_strict, expected_negative=False):
        files = [
            self.test262_path / 'harness/sta.js',
            self.test262_path / 'harness/assert.js',
            self.test262_path / rel_path,
        ]
        cmd = mk_cmd(files=files, use_strict=use_strict)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self.mcjs,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        output = None
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=10.0,
            )
        except TimeoutError:
            process.kill()
            output = {
                'error': {
                    'category': 'timeout',
                    'message': 'runner timed out',
                },
            }
        else:
            if process.returncode != 0:
                # runner failure
                try:
                    error_message = stdout.decode('utf8')
                except UnicodeDecodeError:
                    error_message = '<# encoding error #>'
                output = {
                    'error': {
                        'category': 'runner failure',
                        'message': error_message,
                    }
                }
            else:
                # use the last line of stdout (a handful of versions emit some garbage on stdout)
                stdout_lines = stdout.splitlines()
                output = json.loads(stdout_lines[-1])

        if expected_negative:
            # TODO handle the different categories of expected errors
            if output['error'] is None:
                output['error'] = {
                    'category': 'unexpected success',
                    'message': 'error expected, but test run fine',
                }
            else:
                output['error'] = None

        output['testcase'] = rel_path
        output['version'] = self.vm_version
        output['use_strict'] = use_strict
        return output


@app.command(help='Ingest .jsonl data into a SQLite database (.db)')
@click.option('--db', 'db_filename', required=True, help='Database file.  Will be created if it doesn''t exist.')
@click.option('--clear/--no-clear', help='Clear previous runs from the database.')
@click.argument('data_filenames', nargs=-1)
def ingest(db_filename, data_filenames, clear):
    db = sqlite3.connect(db_filename, autocommit=False)

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
      );
    ''')

    if clear:
        print('clear: deleting previous records (will be un-deleted if anything fails)')
        db.executescript('''
            delete from groups;
            delete from runs;
        ''')

    def insert_run(record):
        group = os.path.dirname(record['testcase'])
        group_sid = insert_string(group)
        testcase_sid = insert_string(record['testcase'])

        error = record.get('error')
        if error is None:
            error_message_sid = None
            error_category = None
        else:
            error_message_sid = insert_string(record['error']['message'])
            error_category = record['error']['category']

        db.execute(
            'insert or ignore into groups (path_sid, group_sid) values (?, ?)',
            (testcase_sid, group_sid)
        )
        db.execute(
            'insert into runs (testcase_sid, error_category, error_message_sid, use_strict, version) '
            + 'values (?, ?, ?, ?, ?)',
            ( testcase_sid
            , error_category
            , error_message_sid
            , record['use_strict']
            , record['version']
            )
        )

    with db:
        for data_filename in data_filenames:
            print(data_filename)

            input = open(data_filename, 'rb')
            if data_filename.endswith('.gz'):
                input = gzip.open(input)

            with input:
                for line in input:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        print('(invalid JSON; skipping line)')
                        continue

                    insert_run(record)

    print('transaction committed.')


@app.command(help='Overview of test results')
@click.option('--db', 'db_filename', required=True, help='Database file')
@click.option('--version', help='mcjs version for which to summarize test results')
@click.option('--mcjs', 'mcjs_root', help='gather version from this directory where the mcjs repository is located')
def status(db_filename, version, mcjs_root):
    from tabulate import tabulate

    if not os.path.exists(db_filename):
        print(f'error: {db_filename}: No such file or directory')
        sys.exit(1)

    if version is None:
        if mcjs_root is None:
            print('pass either --version or --mcjs.')
            sys.exit(1)

        version = get_version_of_repo(mcjs_root)

    db = sqlite3.connect(db_filename)
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
@click.option('--db', 'db_filename', required=True, help='Database file')
@click.option('--version', help='mcjs version for which to summarize test results')
@click.option('--mcjs', 'mcjs_root', help='gather version from this directory where the mcjs repository is located')
@click.option('--outcome', help='Only show test cases with the given outcome (passed, failed)')
@click.option('--filter', default='', help='Only show test cases whose path contains this string')
@click.option('--errors/--no-errors', 'show_errors', help='Show error messages')
def list(db_filename, version, mcjs_root, outcome, filter, show_errors):
    from tabulate import tabulate

    if not os.path.exists(db_filename):
        print(f'error: {db_filename}: No such file or directory')
        return

    if version is None:
        if mcjs_root is None:
            print('error: pass either --version or --mcjs.')
            return

        version = get_version_of_repo(mcjs_root)

    db = sqlite3.connect(db_filename)
    query = '''
        select (error_message_sid is null) as success
        , use_strict
        , st.string as testcase
        , se.string as error_msg
        from runs, strings st, strings se
        where st.string_id = testcase_sid
        and se.string_id = error_message_sid
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
@click.option('--db', 'db_filename', required=True, help='Database file')
@click.argument('version_a')
@click.argument('version_b')
def diff(db_filename, version_a, version_b):
    if not os.path.exists(db_filename):
        print(f'error: {db_filename}: No such file or directory')
        sys.exit(1)

    db = sqlite3.connect(db_filename)

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
