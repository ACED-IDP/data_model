"""Converts datadictionary yaml files to json schema"""


from pathlib import Path

import click
from dictionaryutils import dump_schemas_from_dir
import json
import os


@click.command()
@click.argument('schema')
@click.option('--out', default="aced.json", help="Output path")
def convert(schema, out):
    """Converts yaml files to json schema"""
    if os.path.isdir(out):
        out = os.path.join(out, "aced.json")
    schema = Path(schema)
    assert schema.is_dir(), f"{schema} should be a path"
    click.echo(f"Writing schema into {out}...")
    with open(out, "w") as f:
        json.dump(dump_schemas_from_dir(schema), f)


if __name__ == '__main__':
    convert()
