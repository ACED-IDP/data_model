from collections import defaultdict
from typing import Dict

import click
from fastavro import reader
from pathlib import Path
import logging

logging.basicConfig(format='%(levelname)s:%(message)s', level=logging.WARNING)
logger = logging.getLogger("transform")
logger.setLevel(logging.INFO)


def recursive_default_dict() -> Dict:
    """Recursive default dict."""
    return defaultdict(recursive_default_dict)


@click.command()
@click.option('--path', default='.', help='Search this path for pattern [*.pfb]', show_default=True)
@click.option('--pattern', default='**/*.pfb.avro', help='Search pattern', show_default=True)
def cli(path, pattern):
    """Aggregate avro pfb files into a cytoscape friendly tsv."""
    aggregated_node_counts = defaultdict(set)
    aggregated_edge_counts = recursive_default_dict()
    path = Path(path)
    for file_path in path.glob(pattern):
        with open(file_path, 'rb') as fo:
            logger.info(f"Reading {file_path}")
            avro_reader = reader(fo)
            for record in avro_reader:
                # skip metadata
                if record['name'] == 'Metadata':
                    continue
                aggregated_node_counts[record['name']].add(str(file_path))
                for relation in record['relations']:
                    if relation['dst_name'] not in aggregated_edge_counts[record['name']]:
                        aggregated_edge_counts[record['name']][relation['dst_name']] = set()
                    aggregated_edge_counts[record['name']][relation['dst_name']].add(str(file_path))

    from pprint import pprint
    pprint(aggregated_node_counts)
    # pprint(aggregated_edge_counts)

    edge_table_path = "/tmp/network_table.tsv"
    with open(edge_table_path, "w") as fp:
        print("source\ttarget\tsource_count\tedge_count", file=fp)
        for source in aggregated_edge_counts:
            for target in aggregated_edge_counts[source]:
                print(f"{source}\t{target}\t{len(aggregated_edge_counts[source][target])}\t{len(aggregated_node_counts[source])}", file=fp)
    logger.info(f"Wrote {edge_table_path}")


if __name__ == '__main__':
    cli()
