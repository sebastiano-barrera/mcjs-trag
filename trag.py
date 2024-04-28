#!/usr/bin/env python3

import click
from pathlib import Path


class Context:
    def __init__(self):
        pass

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
@click.option('-o', '--out', type=Path, required=True, help='Results directory')
@click.option('--force/--no-force', help='Overwrite results file if it exists')
@click.argument('testrun_filename', metavar='testrun.json')
def run(testrun_filename, mcjs, out, force):
    import json
    import contextlib
    import subprocess
    from pprint import pprint
    
    testrun = json.load(open(testrun_filename))

    test262_path = Path(testrun['test262_path'])
    testcases = testrun['testcases']

    with contextlib.chdir(mcjs):
        vm_version = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            check=True,
            capture_output=True,
            encoding='utf8'
        ).stdout.strip()

    print('Testing VM version:', vm_version)

    out.mkdir(exist_ok=True)
    out_filename = out / f'{vm_version}.jsonl'
    if out_filename.exists() and not force:
        raise RuntimeError('Results file already exists: ' + str(out_filename))
    out_file = out_filename.open('w')

    results_count = 0
    for rel_path, testcase in testcases.items():
        metadata = testcase['metadata']
        files = [
            test262_path / 'harness/sta.js',
            test262_path / 'harness/assert.js',
            test262_path / rel_path,
        ]
 
        def mk_cmd(use_strict):
            cmd = [
                'cargo',
                'run',
                '-p',
                'mcjs_test262',
                '--',
            ]
            if use_strict:
                cmd.append('--force-last-strict')
            cmd += [str(p) for p in files]
            return cmd

        def run_test_inner(use_strict):
            result = subprocess.run(
                mk_cmd(use_strict=use_strict),
                cwd=mcjs,
                capture_output=True,
            )

            if result.returncode != 0:
                # runner failure
                try:
                    error_message = result.stdout.decode('utf8')
                except UnicodeDecodeError:
                    error_message = '<# encoding error #>'
                    
                return {
                    'error': {
                        'category': 'runner failure',
                        'message': error_message,
                    }
                }

            output = json.loads(result.stdout)
            return output

        def run_test(use_strict):
            output = run_test_inner(use_strict)
            if 'negative' in metadata:
                # TODO handle the different categories of expected errors
                if output['error'] is None:
                    output['error'] = {
                        'category': 'unexpected success',
                        'message': 'error expected, but test run fine',
                    }
                else:
                    output['error'] = None
            output['testcase'] = rel_path
            output['version'] = vm_version
            output['use_strict'] = use_strict
            return output

        def emit_result(result):
            nonlocal results_count
            results_count += 1
            json_line = json.dumps(result)
            assert '\n' not in json_line
            print(json_line, file=out_file)
        
        flags = metadata.get('flags', [])

        if 'onlyStrict' not in flags:
            emit_result(run_test(use_strict=False))

        if 'noStrict' not in flags:
            emit_result(run_test(use_strict=True))

        if results_count >= 10:
            break

    print(f'Finished. {results_count} results written to {out_filename}')
    

        
if __name__ == '__main__':
    app()


