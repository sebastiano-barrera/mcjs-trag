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
@click.option('--force/--no-force', help='Overwrite results file if it exists')
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
        print('Testing VM version:', vm_version)

        out = original_out
        out = out.replace('%v', vm_version)
        if out.endswith('/'):
            out += 'out'
        if not out.endswith('.jsonl'):
            out = out + '.jsonl'
        out = Path(out)

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
        if out.exists() and not force:
            raise RuntimeError('Results file already exists: ' + str(out))

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
                gz_filename = str(out) + '.gz'
                with gzip.open(gz_filename, 'wb') as compressed_file:
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

        print('starting ({}): {}'.format(
            'strict' if use_strict else 'sloppy',
            rel_path,
        ))
        stdout, stderr = await process.communicate()
        try:
            error_message = stdout.decode('utf8')
        except UnicodeDecodeError:
            error_message = '<# encoding error #>'

        if process.returncode != 0:
            # runner failure
            output = {
                'error': {
                    'category': 'runner failure',
                    'message': error_message,
                }
            }
        else:
            output = json.loads(stdout)

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



if __name__ == '__main__':
    app()
