import json
from dictionaryutils import dump_schemas_from_dir


def to_json(json_file_path='dump.json', schema_dir='output/gen3/'):
    with open(json_file_path, 'w') as f:
        json.dump(dump_schemas_from_dir(schema_dir), f)
    print(f"Wrote {json_file_path}")


if __name__ == '__main__':
    to_json()
