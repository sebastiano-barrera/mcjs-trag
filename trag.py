#!/usr/bin/env python3

import click

from pathlib import Path
from pprint import pprint
import asyncio
import contextlib
import gzip
import json
import re
import shutil
import subprocess
import sqlite3
import os.path

@click.group()
def app():
    pass


@app.command(
    short_help='Scan a test262 repo directory.',
    help='Scan a test262 repo directory (a clone of https://github.com/tc39/test262) and gather data about test cases.')
@click.option('--cases', 'cases_filename', default='test-cases.txt', help='File listing test262 cases to run/scan/etc.')
@click.option('-o', '--out', required=True, type=Path, help='Test run file written')
@click.option('--test262', 'test262_path', type=Path)
def scan(cases_filename, test262_path, out):
    import yaml
    import json

    testcases = {}

    for rel_path in open(cases_filename):
        rel_path = rel_path.strip()

        path = test262_path / rel_path
        print(path)

        lines = iter(open(path))

        for line in lines:
            if line.strip() == '/*---':
                break

        yml_metadata_lines = []

        for line in lines:
            if line.strip() == '---*/':
                break
            yml_metadata_lines.append(line)

        metadata = yaml.safe_load('\n'.join(yml_metadata_lines))
        if metadata is not None and 'es6id' in metadata:
            metadata['es6id'] = str(metadata['es6id'])
        testcases[rel_path] = dict(metadata=metadata)

    root = dict(
        test262_path=str(test262_path),
        testcases=testcases,
    )

    with open(out, 'w') as f:
        json.dump(root, fp=f, indent=2)


@app.command(help='Run a set of test cases')
@click.option('--mcjs', type=Path, required=True, help='Path to mcjs repo')
@click.option('-o', '--out', required=True, help='Results directory')
@click.option('--force/--no-force', help='Overwrite results file if it exists (default: skip)')
@click.option('-j', '--max-jobs', default=10, type=int, help='Limit the max number of concurrent tests running at any given time')
@click.option('--commits', 'commits_filename', help='Checkout and test the commits listed in the given file.')
@click.argument('testrun_filename', metavar='testrun.json')
def run(testrun_filename, mcjs, out, force, max_jobs, commits_filename):
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
        with contextlib.chdir(mcjs):
            vm_version = subprocess.run(
                ['git', 'rev-parse', 'HEAD'],
                check=True,
                capture_output=True,
                encoding='utf8'
            ).stdout.strip()
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

        if commits_filename:
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

            def submit_task(use_strict):
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

        asyncio.run(collect_results())
        print(f'Finished. {len(tasks)} results written to {out}.gz')


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
                timeout=5.0,
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


@app.command()
@click.option('--db', 'db_filename', required=True, help='Database file.  Will be created if it doesn''t exist.')
@click.argument('data_filenames', nargs=-1)
def ingest(db_filename, data_filenames):
    db = sqlite3.connect(db_filename, autocommit=False)
    db.isolation_level = 'EXCLUSIVE'

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

    create index if not exists runs__version on runs (version);

    delete from groups;
    delete from runs;
    ''')

    def insert_string(s):
        db.execute('insert or ignore into strings (string) values (?)', [s])
        res = db.execute('select string_id from strings where string = ?', [s])
        return res.fetchone()[0]

    for data_filename in data_filenames:
        print(data_filename)

        input = open(data_filename, 'rb')
        if data_filename.endswith('.gz'):
            input = gzip.open(input)

        try:
            line_count = 0
            for line in input:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    print('(invalid JSON; skipping)')
                    continue

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

                line_count += 1

        finally:
            input.close()

    db.commit()
    print('transaction committed.')


if __name__ == '__main__':
    app()
