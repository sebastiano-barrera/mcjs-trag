#!/usr/bin/env python3

import click
import sqlite3
import os
from pathlib import Path
import shutil
import json

from mako.template import Template


@click.command()
@click.option('--db', 'db_filename', required=True, help='Path to the database')
@click.option('--commits', 'commits_filename', required=True, help='Filename of the commit list')
@click.option('-o', '--output', 'output_dir', type=Path, required=True, help='Directory where to place output files in')
def main(db_filename, commits_filename, output_dir):
    if not os.path.exists(db_filename):
        print(f'error: {db_filename}: No such file or directory')
        os.exit(1)

    db = sqlite3.connect(db_filename, autocommit=False)

    commit_ids = [line.strip() for line in open(commits_filename)]

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



if __name__ == '__main__':
    main()


