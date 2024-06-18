#!/usr/bin/env python3

import sqlite3
import os
from pathlib import Path
import shutil
import json
import contextlib
import subprocess

import click
from mako.template import Template


@click.command()
@click.option('--file', 'data_file', type=Path, default='trag.data', help='Data file to read')
@click.option('--mcjs', 'mcjs_root', help='gather version from this directory where the mcjs repository is located')
@click.option('-o', '--output', 'output_dir', type=Path, required=True, help='Directory where to place output files in')
def main(data_file, mcjs_root, output_dir):
    if not os.path.exists(data_file):
        print(f'error: {data_file}: No such file or directory')
        os.exit(1)

    db = sqlite3.connect(data_file, autocommit=False)

    commit_ids = resolve_commits(repo=mcjs_root, rev_range='HEAD~100..')

    rows = db.execute('''
        select version
        , sum(error_message_sid is null) as n_success
        , count(*) as n_total
        from runs
        group by version
    ''')
    commits = {
        commit_id: dict(
            commit_id=commit_id,
            n_success=n_success,
            n_total=n_total,
        )
        for (commit_id, n_success, n_total) in rows
    }
    # reorder the records read from SQLite based on the commits file
    commits = [commits[cid] for cid in commit_ids if cid in commits]

    output_dir.mkdir(exist_ok=True)

    with (output_dir / 'commits.json').open('w') as out:
        json.dump({'commits': commits}, out)

    here = Path(__file__).parent
    for src in (here / 'templates' / 'dashboard').glob('*'):
        dst = output_dir / src.name
        print('copy', src, dst)
        shutil.copy(src=src, dst=dst)

    for ndx, commit_id in enumerate(commit_ids):
        output_path = output_dir / f'{commit_id}.json'
        if output_path.exists():
            continue

        print(f'{ndx}/{len(commit_ids)}', end='\r')
        res = db.execute('''
            with q as (
                select g.group_sid, iif(error_message_sid is null, 1, 0) as success
                from runs r, groups g
                where r.version = ?
                and r.testcase_sid = g.path_sid
            )
            select sg.string as grp
            , sum(q.success) as ok
            , count(*) as total
            from q, strings sg
            where sg.string_id = q.group_sid
            group by q.group_sid
            order by grp
        ''', (commit_id, ))

        groups = [
            dict(
                path=test_group,
                n_ok=n_ok,
                n_fail=n_total - n_ok,
            )
            for test_group, n_ok, n_total in res.fetchall()
        ]

        with (output_dir / f'{commit_id}.json').open('w') as out:
            json.dump({'groups': groups}, out)

            
def resolve_commits(repo, rev_range):
    with contextlib.chdir(repo):
        if '..' in rev_range:
            cmd = ['git', 'log', '--first-parent', '--format=%H', rev_range]
        else:
            cmd = ['git', 'rev-parse', rev_range]
        return subprocess.check_output(cmd, encoding='ascii').splitlines()




if __name__ == '__main__':
    main()


